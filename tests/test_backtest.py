import json

import pytest

from acquirer_id.backtest import (
    CaseResult,
    GoldenCase,
    case_target,
    compare,
    evaluate,
    freeze_cases,
    load_cases,
    metrics,
    rank_most_active_in_sector,
    rank_scorer,
)
from acquirer_id.config import ScoringConfig
from acquirer_id.data import AcquirerType, load_transactions
from tests.conftest import REAL_CSV


def test_freeze_assigns_splits_by_year(deals):
    cases = freeze_cases(deals, dev_years={2022}, holdout_years={2023, 2024})
    assert {c.transaction_id: c.split for c in cases} == {
        "T01": "holdout",
        "T02": "holdout",
        "T08": "dev",
        "T11": "holdout",
        "T12": "dev",
        "T13": "holdout",
    }


def test_case_target_hides_the_buyer_and_keeps_the_profile(deals):
    d = next(d for d in deals if d.transaction_id == "T03")  # National Health IT deal
    t = case_target(d)
    assert (t.sector, t.deal_size_mm, t.ownership) == ("Health IT", 50, "PE-Backed")
    assert t.regional is False and t.region is None
    assert "Sponsor A" not in t.model_dump_json()


def test_rankers_only_see_strictly_earlier_years(deals):
    seen_years = []

    def spy(prior, target, cfg):
        seen_years.append(max(d.deal_year for d in prior))
        return []

    evaluate(deals, [GoldenCase(transaction_id="T01", split="holdout")], spy, ScoringConfig())
    assert seen_years == [2023]  # T01 is a 2024 deal


def test_hit_and_reciprocal_rank(deals):
    cfg = ScoringConfig(k=10)
    # T08 (2022 HCS, Health System X): prior HCS buyers are Health System Y (2 deals)
    # and nobody else, so the in-sector baseline cannot contain Health System X.
    (r,) = evaluate(
        deals, [GoldenCase(transaction_id="T08", split="dev")], rank_most_active_in_sector, cfg
    )
    assert r.ranked == ["Health System Y"]
    assert (r.hit, r.reciprocal_rank, r.label_prior_deals) == (False, 0.0, 0)


def test_scorer_returns_empty_when_sector_has_no_history(deals):
    d = next(d for d in deals if d.transaction_id == "T05")  # 2018 Health IT, first in sector
    prior = [p for p in deals if p.deal_year < d.deal_year]
    assert rank_scorer(prior, case_target(d), ScoringConfig()) == []


def _result(tid, hit):
    return CaseResult(
        transaction_id=tid,
        split="dev",
        sector="Dental",
        deal_size_mm=100,
        label="x",
        label_type=AcquirerType.SPONSOR,
        label_prior_deals=1,
        ranked=["x"] if hit else [],
        hit=hit,
        reciprocal_rank=float(hit),
    )


def test_metrics_and_paired_comparison():
    cand = [_result("a", True), _result("b", False), _result("c", True)]
    ref = [_result("a", False), _result("b", True), _result("c", True)]
    m = metrics(cand)
    assert (m.n, m.hit_at_k, m.mrr) == (3, pytest.approx(0.667), pytest.approx(0.667))
    cmp = compare(cand, ref)
    assert (cmp.wins, cmp.losses, cmp.won_cases, cmp.lost_cases) == (1, 1, ["a"], ["b"])


@pytest.mark.skipif(not REAL_CSV.exists(), reason="real dataset not present")
def test_frozen_golden_cases_match_the_dataset():
    from pathlib import Path

    path = Path(__file__).parents[1] / "evals" / "golden" / "ranking_cases.json"
    meta = json.loads(path.read_text())
    by_id = {d.transaction_id: d for d in load_transactions(REAL_CSV)}
    cases = load_cases(path)
    assert len(cases) == 142
    for c in cases:
        year = by_id[c.transaction_id].deal_year
        assert year in (meta["dev_years"] if c.split == "dev" else meta["holdout_years"])
