"""Golden set 1: replay historical deals and check whether the real buyer is ranked.

Each case is a past deal. Its target profile is derived from the deal, the real acquirer is the
label, and every ranker sees only deals from strictly earlier years.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from acquirer_id.config import ScoringConfig, TargetProfile
from acquirer_id.data import AcquirerType, Transaction
from acquirer_id.ranking import (
    NATIONAL_GEOGRAPHIES,
    TargetOutOfRange,
    build_context,
    score_acquirers,
    select,
)

Ranker = Callable[[list[Transaction], TargetProfile, ScoringConfig], list[str]]


class GoldenCase(BaseModel):
    transaction_id: str
    split: str  # "dev" or "holdout"


def freeze_cases(
    deals: list[Transaction], dev_years: set[int], holdout_years: set[int]
) -> list[GoldenCase]:
    split_of = {y: "dev" for y in dev_years} | {y: "holdout" for y in holdout_years}
    return [
        GoldenCase(transaction_id=d.transaction_id, split=split_of[d.deal_year])
        for d in sorted(deals, key=lambda d: d.transaction_id)
        if d.deal_year in split_of
    ]


def load_cases(path: Path) -> list[GoldenCase]:
    return [GoldenCase.model_validate(c) for c in json.loads(path.read_text())["cases"]]


def case_target(d: Transaction) -> TargetProfile:
    """The profile a banker would have had for this target, without the buyer."""
    regional = d.geography not in NATIONAL_GEOGRAPHIES
    return TargetProfile(
        name=d.target_company,
        sector=d.sector,
        deal_size_mm=d.deal_size_mm,
        ownership=d.target_ownership_pre,
        regional=regional,
        region=d.geography if regional else None,
        ebitda_margin_pct=d.ebitda_margin_pct,
    )


# ---------- rankers: each returns up to k acquirer names, best first ----------


def rank_scorer(prior: list[Transaction], target: TargetProfile, cfg: ScoringConfig) -> list[str]:
    try:
        ctx = build_context(prior, target, cfg)
    except TargetOutOfRange:
        return []  # no history in this sector yet: an honest miss, not an error
    sel = select(score_acquirers(prior, target, ctx, cfg), ctx, cfg)
    return [s.acquirer for s in sel.selected]


def rank_most_active(prior: list[Transaction], _t: TargetProfile, cfg: ScoringConfig) -> list[str]:
    counts = Counter(d.acquirer for d in prior)
    return [a for a, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: cfg.k]]


def rank_most_active_in_sector(
    prior: list[Transaction], t: TargetProfile, cfg: ScoringConfig
) -> list[str]:
    return rank_most_active([d for d in prior if d.sector == t.sector], t, cfg)


RANKERS: dict[str, Ranker] = {
    "baseline_most_active": rank_most_active,
    "baseline_most_active_in_sector": rank_most_active_in_sector,
    "scorer": rank_scorer,
}


# ---------- evaluation ----------


class CaseResult(BaseModel):
    """One replayed deal: the record error analysis reads."""

    transaction_id: str
    split: str
    sector: str
    deal_size_mm: float
    label: str
    label_type: AcquirerType
    label_prior_deals: int  # 0 = first-time buyer, unpredictable from history
    ranked: list[str]
    hit: bool
    reciprocal_rank: float


def evaluate(
    deals: list[Transaction],
    cases: list[GoldenCase],
    ranker: Ranker,
    cfg: ScoringConfig,
) -> list[CaseResult]:
    by_id = {d.transaction_id: d for d in deals}
    results = []
    for c in cases:
        d = by_id[c.transaction_id]
        prior = [p for p in deals if p.deal_year < d.deal_year]
        ranked = ranker(prior, case_target(d), cfg)
        pos = ranked.index(d.acquirer) + 1 if d.acquirer in ranked else None
        results.append(
            CaseResult(
                transaction_id=d.transaction_id,
                split=c.split,
                sector=d.sector,
                deal_size_mm=d.deal_size_mm,
                label=d.acquirer,
                label_type=d.acquirer_type,
                label_prior_deals=sum(p.acquirer == d.acquirer for p in prior),
                ranked=ranked,
                hit=pos is not None,
                reciprocal_rank=1 / pos if pos else 0.0,
            )
        )
    return results


class Metrics(BaseModel):
    n: int
    hit_at_k: float
    mrr: float


def metrics(results: list[CaseResult]) -> Metrics | None:
    if not results:
        return None
    return Metrics(
        n=len(results),
        hit_at_k=round(sum(r.hit for r in results) / len(results), 3),
        mrr=round(statistics.fmean(r.reciprocal_rank for r in results), 3),
    )


SLICES: dict[str, Callable[[CaseResult], bool]] = {
    "all": lambda r: True,
    "sponsor_label": lambda r: r.label_type == AcquirerType.SPONSOR,
    "strategic_label": lambda r: r.label_type == AcquirerType.STRATEGIC,
    "strategic_thin_history": lambda r: (
        r.label_type == AcquirerType.STRATEGIC and 1 <= r.label_prior_deals <= 2
    ),
    "first_time_buyer": lambda r: r.label_prior_deals == 0,
    "healthcare_services_near_200m": lambda r: (
        r.sector == "Healthcare Services" and 100 <= r.deal_size_mm <= 400
    ),
}


def summarize(results: list[CaseResult]) -> dict[str, Metrics | None]:
    return {name: metrics([r for r in results if pred(r)]) for name, pred in SLICES.items()}


class PairedComparison(BaseModel):
    """Same cases, two rankers: where they disagree is the only signal at this sample size."""

    wins: int  # candidate hit, reference missed
    losses: int  # candidate missed, reference hit
    won_cases: list[str]
    lost_cases: list[str]


def compare(candidate: list[CaseResult], reference: list[CaseResult]) -> PairedComparison:
    ref = {r.transaction_id: r.hit for r in reference}
    won = [r.transaction_id for r in candidate if r.hit and not ref[r.transaction_id]]
    lost = [r.transaction_id for r in candidate if not r.hit and ref[r.transaction_id]]
    return PairedComparison(wins=len(won), losses=len(lost), won_cases=won, lost_cases=lost)
