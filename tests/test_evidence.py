from acquirer_id.data import Outcome
from acquirer_id.evidence import build_report
from acquirer_id.ranking import build_context, score_acquirers, select


def _report(deals, target, cfg):
    ctx = build_context(deals, target, cfg)
    sel = select(score_acquirers(deals, target, ctx, cfg), ctx, cfg)
    return build_report(deals, target, ctx, sel, cfg)


def test_precedents_are_real_deals_of_that_acquirer(deals, target, cfg):
    by_id = {d.transaction_id: d for d in deals}
    for pack in _report(deals, target, cfg).packs:
        assert pack.precedents
        for p in pack.precedents:
            assert by_id[p.transaction_id].acquirer == pack.acquirer
            assert p.deal_size_mm == by_id[p.transaction_id].deal_size_mm


def test_valuation_comps_are_closed_deals(deals, target, cfg):
    by_id = {d.transaction_id: d for d in deals}
    v = _report(deals, target, cfg).valuation
    assert all(by_id[i].outcome is Outcome.CLOSED for i in v.comp_transaction_ids)
    assert v.all_comps.n == 2
    # 2 comps: too few for a median, so the values are given instead
    assert v.all_comps.median_ev_ebitda is None
    assert v.all_comps.ev_ebitda_values == [12.0, 13.5]


def test_ranks_are_contiguous_and_follow_score(deals, target, cfg):
    packs = _report(deals, target, cfg).packs
    assert [p.rank for p in packs] == list(range(1, len(packs) + 1))
    assert [p.score for p in packs] == sorted((p.score for p in packs), reverse=True)


def test_profile_counts_match_data(deals, target, cfg):
    pack = next(p for p in _report(deals, target, cfg).packs if p.acquirer == "Sponsor A")
    assert pack.profile.n_deals == 4
    assert pack.profile.withdrawn_or_terminated == 1
    assert pack.profile.deals_in_target_sector == 1
    assert pack.risk_signals.leveraged_share_of_deals == 0.75


def test_median_only_from_five_deals(deals):
    from acquirer_id.evidence import multiple_stats

    closed = [d for d in deals if d.outcome.value == "Closed" and d.ev_ebitda_multiple]
    assert multiple_stats(closed[:4]).median_ev_ebitda is None
    five = multiple_stats(closed[:5])
    assert five.median_ev_ebitda is not None and five.ev_ebitda_values == []
