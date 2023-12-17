"""
Old branching heuristics, must be removed very soon (assigned to Kaidi).
"""

import torch
import arguments


@torch.no_grad()
def input_split_branching(net, dom_lb, x_L, x_U, lA, thresholds,
                          branching_method, split_depth=1, num_iter=0,
                          last_split_idx=None):
    """
    Produce input split according to branching methods.
    """
    x_L = x_L.flatten(1)
    x_U = x_U.flatten(1)

    if branching_method == 'naive':
        # we just select the longest edge
        return torch.topk(x_U - x_L, split_depth, -1).indices
    elif branching_method == 'sb':
        return input_split_heuristic_sb(x_L, x_U, num_iter, dom_lb,
                                        thresholds, lA, split_depth)
    elif branching_method == 'brute-force':
        assert split_depth == 1
        return input_split_heuristic_bf(
            net, x_L, x_U, num_iter, dom_lb, thresholds, lA, last_split_idx)
    else:
        raise NameError(f'Unsupported branching method "{branching_method}" for input splits.')


def input_split_heuristic_sb(x_L, x_U, num_iter, dom_lb, thresholds, lA, split_depth=1):
    branching_args = arguments.Config['bab']['branching']
    input_split_args = branching_args['input_split']
    sb_primary_spec = input_split_args['sb_primary_spec']
    sb_primary_spec_iter = input_split_args['sb_primary_spec_iter']
    lA_clamping_thresh = branching_args['sb_coeff_thresh']
    sb_margin_weight = input_split_args['sb_margin_weight']
    sb_sum = input_split_args['sb_sum']

    lA = lA.view(lA.shape[0], lA.shape[1], -1)
    # lA shape: (batch, spec, # inputs)
    perturb = (x_U - x_L).unsqueeze(-2)
    # perturb shape: (batch, 1, # inputs)
    # dom_lb shape: (batch, spec)
    # thresholds shape: (batch, spec)
    assert lA_clamping_thresh >= 0

    if sb_sum:
        score = (lA.abs().clamp(min=lA_clamping_thresh) * perturb / 2
                + (dom_lb.to(lA.device).unsqueeze(-1)
                    - thresholds.unsqueeze(-1)) * sb_margin_weight)
        score = score.sum(dim=-2)
    else:
        if (sb_primary_spec is not None
                and num_iter is not None and num_iter >= sb_primary_spec_iter):
            score = (lA[:, sb_primary_spec].abs().clamp(min=lA_clamping_thresh)
                        * perturb.squeeze(1) / 2
                    + (dom_lb[:, sb_primary_spec].to(lA.device).unsqueeze(-1)
                        - thresholds[:, sb_primary_spec].unsqueeze(-1))
                    * sb_margin_weight)
        else:
            score = (lA.abs().clamp(min=lA_clamping_thresh) * perturb / 2
                    + (dom_lb.to(lA.device).unsqueeze(-1)
                        - thresholds.unsqueeze(-1)) * sb_margin_weight)
        score = score.amax(dim=-2)
    # note: the k (split_depth) in topk <= # inputs, because split_depth is computed as
    # min(max split depth, # inputs).
    # 1) If max split depth <= # inputs, then split_depth <= # inputs.
    # 2) If max split depth > # inputs, then split_depth = # inputs.
    return torch.topk(score, split_depth, -1).indices


def input_split_heuristic_bf(net, x_L, x_U, num_iter, dom_lb, thresholds, lA,
                             last_split_idx):
    branching_args = arguments.Config['bab']['branching']
    input_split_args = branching_args['input_split']
    lA_clamping_thresh = branching_args['sb_coeff_thresh']
    sb_margin_weight = input_split_args['sb_margin_weight']
    bf_backup_thresh = input_split_args['bf_backup_thresh']
    bf_rhs_offset = input_split_args['bf_rhs_offset']
    compare_with_old_bounds = input_split_args["compare_with_old_bounds"]
    zero_crossing_score = input_split_args['bf_zero_crossing_score']

    assert x_L.ndim == 2
    input_dim = x_L.shape[1]
    x_M = (x_L + x_U) / 2
    new_x_L = x_L.expand(2, input_dim, -1, -1).clone()
    new_x_U = x_U.expand(2, input_dim, -1, -1).clone()
    for i in range(input_dim):
        new_x_U[0, i, :, i] = x_M[:, i]
        new_x_L[1, i, :, i] = x_M[:, i]
    new_x_L = new_x_L.view(-1, new_x_L.shape[-1])
    new_x_U = new_x_U.view(-1, new_x_U.shape[-1])
    from auto_LiRPA import BoundedTensor, PerturbationLpNorm
    new_x = BoundedTensor(
        new_x_L,
        ptb=PerturbationLpNorm(x_L=new_x_L, x_U=new_x_U))
    C = net.c.expand(new_x.shape[0], -1, -1)
    lb_ibp = net.net.compute_bounds(
        x=(new_x,), C=C, method='ibp', bound_upper=False)[0]
    reference_interm_bounds = {}
    for node in net.net.nodes():
        if (node.perturbed
                and isinstance(getattr(node, 'lower', None), torch.Tensor)
                and isinstance(getattr(node, 'upper', None), torch.Tensor)):
            reference_interm_bounds[node.name] = (node.lower, node.upper)
    lb_crown = net.net.compute_bounds(
        x=(new_x,), C=C, method='crown', bound_upper=False,
        reference_bounds=reference_interm_bounds
    )[0]
    lb = torch.max(lb_ibp, lb_crown)

    margin = (lb - thresholds[0]).view(2, input_dim, -1, lb.shape[-1])
    lb_base = dom_lb.cuda() - thresholds[0]
    verified = margin.amax(dim=-1) > 0

    assert bf_rhs_offset >= 0
    objective = (
        (margin - lb_base).clamp(min=0)
        / (lb_base - bf_rhs_offset).abs().clamp(min=1e-8)
        * (1 - verified.unsqueeze(-1).int())
    ).clamp(max=2e8).sum(dim=0)

    objective = objective.sum(dim=-1)
    objective = objective + 1e9 * verified.sum(dim=0)
    too_bad = objective.amax(dim=0) < bf_backup_thresh

    # TODO branch at zero rather than midpoint
    if zero_crossing_score:
        cross_zero = torch.logical_and(x_L < 0, x_U > 0)
        objective = objective + (cross_zero * (x_U - x_L) * 100).t()

    lA = lA.view(lA.shape[0], lA.shape[1], -1)
    perturb = (x_U - x_L).unsqueeze(-2)
    sb_score = (lA.abs().clamp(min=lA_clamping_thresh) * perturb / 2
            + (dom_lb.to(lA.device).unsqueeze(-1)
                - thresholds.unsqueeze(-1)) * sb_margin_weight)
    sb_score = sb_score.sum(dim=-2)
    objective[:, too_bad] = sb_score[too_bad].t()

    index = objective.argmax(0).unsqueeze(-1)

    worst_idx = margin.amax(dim=-1).amin(dim=0).amax(dim=0).argmin()
    print('Worst idx:', worst_idx)
    print('Before', lb_base[worst_idx])
    print('Left branch:', margin[0, :, worst_idx])
    print('Right branch:', margin[1, :, worst_idx])
    print('Selected index:', index[worst_idx])
    if last_split_idx is not None:
        print('Last selected index:', last_split_idx[worst_idx])
    print('Objective', objective[:, worst_idx])
    print('x_L', x_L[worst_idx])
    print('x_U', x_U[worst_idx])
    if too_bad[worst_idx]:
        print('Bad objective. Using SB.')

    if torch.isnan(margin).any():
        import pdb; pdb.set_trace()

    return index
