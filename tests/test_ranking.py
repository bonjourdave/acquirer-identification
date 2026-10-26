import math

import pytest

from acquirer_id.config import ScoringConfig, TargetProfile
from acquirer_id.data import AcquirerType
from acquirer_id.ranking import (
    TargetOutOfRange,
    _shrink,
    build_context,
    comparable_set,
    score_acquirers,
    sector_adjacency,
    select,
    size_similarity,
)


def test_adjacency_lift_matches_hand_calculation(deals, cfg):
    # HCS buyers' other deals: 1 PG, 1 Health IT, 1 Dental (of 3).
    # All non-HCS deals: 3 PG, 4 Health IT, 2 Dental (of 9).
    lift, weight = sector_adjacency(deals, "Healthcare Services", cfg)
    assert lift["Physician Groups"] == pytest.approx(1.0)
    assert lift["Health IT"] == pytest.approx(0.75)
    assert lift["Dental"] == pytest.approx(1.5)
    assert weight["Healthcare Services"] == 1.0
    assert weight["Dental"] == pytest.approx(cfg.adjacency_scale * 0.5)


def test_sectors_at_or_below_chance_get_no_weight(deals, cfg):
    _, weight = sector_adjacency(deals, "Healthcare Services", cfg)
    assert weight["Physician Groups"] == 0.0
    assert weight["Health IT"] == 0.0


def test_size_similarity_is_symmetric_in_log_space():
    assert size_similarity(200, 200, math.log(2)) == 1.0
    assert size_similarity(400, 200, math.log(2)) == pytest.approx(math.exp(-0.5))
    assert size_similarity(100, 200, math.log(2)) == pytest.approx(math.exp(-0.5))


def test_unknown_sector_is_refused(deals, cfg):
    with pytest.raises(TargetOutOfRange, match="no deals"):
        build_context(deals, TargetProfile(sector="Software", deal_size_mm=200), cfg)


def test_size_far_outside_dataset_is_refused(deals, cfg):
    with pytest.raises(TargetOutOfRange, match="outside the range"):
        build_context(deals, TargetProfile(sector="Dental", deal_size_mm=500_000), cfg)


def test_strong_margin_defaults_to_sector_percentile(deals, target, cfg):
    ctx = build_context(deals, target, cfg)
    # HCS margins in fixture: 18, 17, 12, 10 -> 75th percentile = 17.25
    assert ctx.margin_is_assumed
    assert ctx.target_margin_pct == pytest.approx(17.2, abs=0.05)


def test_comparables_use_first_ladder_step_with_enough_deals(deals, target, cfg):
    ctx = build_context(deals, target, cfg)
    assert ctx.comparables.step.startswith("target + adjacent sectors, 0.5-2x")
    assert set(ctx.comparables.transaction_ids) == {"T01", "T08"}  # closed HCS near $200M
    assert ctx.comparables.strategic_share == 0.5


def test_comparables_fall_back_when_sparse(deals, target):
    sparse = ScoringConfig(comps_min_deals=100)
    _, weight = sector_adjacency(deals, target.sector, sparse)
    comps = comparable_set(deals, target, weight, sparse)
    assert comps.step == "all closed deals"
    assert comps.n == 11  # 13 fixture deals minus T04 (Withdrawn) and T10 (Terminated)


def test_shrinkage_pulls_thin_evidence_toward_prior():
    # One deal with a perfect ratio, prior 0.1, strength 3 -> (1 + 0.3) / 4
    assert _shrink(1.0, 1, 0.1, 3.0) == pytest.approx(0.325)
    # Many deals barely move
    assert _shrink(50.0, 50, 0.1, 3.0) == pytest.approx((50 + 0.3) / 53)


def test_only_buyers_in_target_or_adjacent_sectors_are_candidates(deals, target, cfg):
    ctx = build_context(deals, target, cfg)
    names = {s.acquirer for s in score_acquirers(deals, target, ctx, cfg)}
    assert names == {"Sponsor A", "Sponsor B", "Health System X", "Health System Y"}


def test_scores_are_in_unit_interval_and_sorted(deals, target, cfg):
    ctx = build_context(deals, target, cfg)
    scores = score_acquirers(deals, target, ctx, cfg)
    assert all(0 <= s.score <= 1 for s in scores)
    assert [s.score for s in scores] == sorted((s.score for s in scores), reverse=True)


def test_thin_evidence_caps_conviction(deals, target, cfg):
    ctx = build_context(deals, target, cfg)
    by_name = {s.acquirer: s for s in score_acquirers(deals, target, ctx, cfg)}
    assert by_name["Health System X"].n_relevant_deals == 1
    assert by_name["Health System X"].conviction_cap == "Medium"


def test_selection_honours_floor_per_type(deals, target):
    # comps_min_deals=100 forces the all-closed comparable set: 5 of 11 strategic
    # min_relevant_deals=0: this tests the floor, not eligibility (the fixture is tiny)
    cfg = ScoringConfig(k=3, min_per_type=1, comps_min_deals=100, min_relevant_deals=0)
    ctx = build_context(deals, target, cfg)
    sel = select(score_acquirers(deals, target, ctx, cfg), ctx, cfg)
    types = [s.acquirer_type for s in sel.selected]
    assert len(sel.selected) == 3
    assert types.count(AcquirerType.STRATEGIC) >= 1
    assert types.count(AcquirerType.SPONSOR) >= 1


def test_selection_hands_slots_to_other_type_when_pool_runs_out(deals, target):
    cfg = ScoringConfig(k=4, min_per_type=1, comps_min_deals=2, min_relevant_deals=0)
    ctx = build_context(deals, target, cfg)
    sel = select(score_acquirers(deals, target, ctx, cfg), ctx, cfg)
    assert len(sel.selected) == 4  # 2 sponsors + 2 strategics is the whole pool
    assert sel.next_by_type == {AcquirerType.STRATEGIC: None, AcquirerType.SPONSOR: None}


def test_buyers_without_a_similar_deal_are_not_listed(deals, target):
    cfg = ScoringConfig(k=10, min_per_type=1, comps_min_deals=2)
    ctx = build_context(deals, target, cfg)
    scores = score_acquirers(deals, target, ctx, cfg)
    zero = {s.acquirer for s in scores if s.n_relevant_deals == 0}
    assert zero, "fixture needs a candidate with no similar deal"
    listed = {s.acquirer for s in select(scores, ctx, cfg).selected}
    assert not listed & zero
    padded = select(scores, ctx, cfg.model_copy(update={"min_relevant_deals": 0}))
    assert {s.acquirer for s in padded.selected} & zero


def test_lift_below_threshold_earns_no_weight(deals, cfg):
    lift, weight = sector_adjacency(deals, "Healthcare Services", cfg)
    above_one = [s for s, x in lift.items() if s != "Healthcare Services" and x > 1]
    assert above_one, "fixture needs a sector with lift above 1"
    strict = cfg.model_copy(
        update={"adjacent_lift_threshold": max(lift[s] for s in above_one) + 0.01}
    )
    _, strict_weight = sector_adjacency(deals, "Healthcare Services", strict)
    assert all(strict_weight[s] == 0 for s in above_one)
    assert strict_weight["Healthcare Services"] == 1.0
