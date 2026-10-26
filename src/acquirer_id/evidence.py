"""Per-acquirer evidence packs: the only context the writing stage sees.

Every figure the rationale may cite is computed here and rounded once, so the grounding
validator can check the prose against these values exactly.
"""

from __future__ import annotations

import statistics
from collections import Counter

from pydantic import BaseModel

from acquirer_id.config import ScoringConfig, TargetProfile
from acquirer_id.data import AcquirerType, Outcome, Transaction
from acquirer_id.ranking import AcquirerScore, MarketContext, Selection


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


def _median(xs: list[float]) -> float | None:
    return _r(statistics.median(xs)) if xs else None


MIN_N_FOR_MEDIAN = 5  # below this a median misleads; the individual values are given instead


class MultipleStats(BaseModel):
    n: int
    median_ev_ebitda: float | None  # None when n < MIN_N_FOR_MEDIAN
    median_ev_revenue: float | None
    ev_ebitda_values: list[float] = []  # each deal's multiple, only when n < MIN_N_FOR_MEDIAN
    ev_revenue_values: list[float] = []


def multiple_stats(deals: list[Transaction]) -> MultipleStats:
    ebitda = [d.ev_ebitda_multiple for d in deals if d.ev_ebitda_multiple is not None]
    revenue = [d.ev_revenue_multiple for d in deals if d.ev_revenue_multiple is not None]
    if len(deals) < MIN_N_FOR_MEDIAN:
        return MultipleStats(
            n=len(deals),
            median_ev_ebitda=None,
            median_ev_revenue=None,
            ev_ebitda_values=sorted(_r(x) for x in ebitda),
            ev_revenue_values=sorted(_r(x, 2) for x in revenue),
        )
    return MultipleStats(
        n=len(deals),
        median_ev_ebitda=_median(ebitda),
        median_ev_revenue=_r(statistics.median(revenue), 2) if revenue else None,
    )


class Precedent(BaseModel):
    transaction_id: str
    target_company: str
    deal_year: int
    sector: str
    deal_type: str
    deal_size_mm: float
    ev_ebitda_multiple: float | None
    ev_revenue_multiple: float | None
    ebitda_margin_pct: float | None
    geography: str
    outcome: str
    target_ownership_pre: str
    financing_type: str
    num_bidders: int | None
    rationale_tags: list[str]
    relevance: float


class ValuationContext(BaseModel):
    """Market comps shared by all acquirers: the comparable set from the ranking stage."""

    comparable_set_basis: str
    all_comps: MultipleStats
    above_target_margin_comps: MultipleStats  # comps at or above the target's margin
    strategic_comps: MultipleStats
    sponsor_comps: MultipleStats
    comp_transaction_ids: list[str]


class AcquirerProfile(BaseModel):
    """What the dataset implies about the acquirer. Not outside knowledge."""

    n_deals: int
    first_year: int
    last_year: int
    deals_last_3_years: int
    deals_by_sector: dict[str, int]
    deal_size_min_mm: float
    deal_size_median_mm: float
    deal_size_max_mm: float
    deal_types: dict[str, int]
    financing_types: dict[str, int]
    rationale_tags: dict[str, int]
    closed: int
    withdrawn_or_terminated: int
    deals_in_target_sector: int


class RiskSignals(BaseModel):
    """Raw inputs for the Risk Flags section; the writer interprets, code supplies the numbers."""

    median_bidders_on_relevant_deals: float | None  # None when fewer than MIN_N_FOR_MEDIAN deals
    bidders_on_relevant_deals: list[int] = []  # the counts themselves, when too few for a median
    leveraged_share_of_deals: float
    withdrawn_or_terminated_share: float
    deals_in_target_sector: int  # in-market overlap: antitrust / integration proxy for strategics
    single_sector_buyer: bool


class EvidencePack(BaseModel):
    rank: int
    acquirer: str
    acquirer_type: AcquirerType
    score: float
    score_breakdown: dict[str, float]  # feature -> shrunk value in [0, 1]
    n_relevant_deals: int
    conviction_cap: str
    profile: AcquirerProfile
    own_relevant_multiples: MultipleStats  # this acquirer's closed relevant deals
    precedents: list[Precedent]
    risk_signals: RiskSignals


class TargetSummary(BaseModel):
    profile: TargetProfile
    target_margin_pct: float
    margin_is_assumed: bool
    as_of_year: int
    adjacent_sectors: dict[str, float]  # sector -> lift, for sectors with nonzero weight


class MixSummary(BaseModel):
    strategic_slots: int
    sponsor_slots: int
    mix_share_basis: str  # "sector" or "comparables"
    strategic_share_used: float
    sector_deal_count: int
    comparable_strategic_share: float  # shown alongside for context: the size-matched deals
    strategic_slots_from_data: float
    floor_applied: bool
    next_by_type: dict[AcquirerType, str | None]


class RankingReport(BaseModel):
    target: TargetSummary
    valuation: ValuationContext
    mix: MixSummary
    packs: list[EvidencePack]
    feature_weights: dict[str, float] = {}  # published in the report appendix
    # Outside-the-dataset notes: rendered for the banker, never part of the writer input
    entity_notes: dict[str, str] = {}
    aliases_applied: dict[str, str] = {}
    entity_notes_source: str = ""


def _acquirer_profile(ds: list[Transaction], target: TargetProfile, as_of: int) -> AcquirerProfile:
    sizes = [d.deal_size_mm for d in ds]
    return AcquirerProfile(
        n_deals=len(ds),
        first_year=min(d.deal_year for d in ds),
        last_year=max(d.deal_year for d in ds),
        deals_last_3_years=sum(d.deal_year > as_of - 3 for d in ds),
        deals_by_sector=dict(Counter(d.sector for d in ds).most_common()),
        deal_size_min_mm=_r(min(sizes)),
        deal_size_median_mm=_r(statistics.median(sizes)),
        deal_size_max_mm=_r(max(sizes)),
        deal_types=dict(Counter(d.deal_type for d in ds).most_common()),
        financing_types=dict(Counter(d.financing_type for d in ds).most_common()),
        rationale_tags=dict(Counter(t for d in ds for t in d.rationale_tags).most_common()),
        closed=sum(d.outcome == Outcome.CLOSED for d in ds),
        withdrawn_or_terminated=sum(
            d.outcome in (Outcome.WITHDRAWN, Outcome.TERMINATED) for d in ds
        ),
        deals_in_target_sector=sum(d.sector == target.sector for d in ds),
    )


def build_report(
    deals: list[Transaction],
    target: TargetProfile,
    ctx: MarketContext,
    selection: Selection,
    cfg: ScoringConfig,
) -> RankingReport:
    by_id = {d.transaction_id: d for d in deals}
    comps = [by_id[i] for i in ctx.comparables.transaction_ids]
    valuation = ValuationContext(
        comparable_set_basis=ctx.comparables.step,
        all_comps=multiple_stats(comps),
        above_target_margin_comps=multiple_stats(
            [d for d in comps if (d.ebitda_margin_pct or 0) >= ctx.target_margin_pct]
        ),
        strategic_comps=multiple_stats(
            [d for d in comps if d.acquirer_type == AcquirerType.STRATEGIC]
        ),
        sponsor_comps=multiple_stats([d for d in comps if d.acquirer_type == AcquirerType.SPONSOR]),
        comp_transaction_ids=ctx.comparables.transaction_ids,
    )
    packs = [
        _pack(rank, s, by_id, target, ctx, cfg)
        for rank, s in enumerate(selection.selected, start=1)
    ]
    return RankingReport(
        target=TargetSummary(
            profile=target,
            target_margin_pct=ctx.target_margin_pct,
            margin_is_assumed=ctx.margin_is_assumed,
            as_of_year=ctx.as_of_year,
            adjacent_sectors={
                s: round(ctx.sector_lift[s], 2)
                for s, w in sorted(ctx.sector_weight.items(), key=lambda kv: -kv[1])
                if w > 0 and s != target.sector
            },
        ),
        valuation=valuation,
        mix=MixSummary(
            strategic_slots=selection.strategic_slots,
            sponsor_slots=len(selection.selected) - selection.strategic_slots,
            mix_share_basis=cfg.mix_share_basis,
            strategic_share_used=round(ctx.strategic_share(cfg.mix_share_basis), 2),
            sector_deal_count=ctx.sector_deal_count,
            comparable_strategic_share=round(ctx.comparables.strategic_share, 2),
            strategic_slots_from_data=selection.strategic_slots_from_data,
            floor_applied=selection.floor_applied,
            next_by_type=selection.next_by_type,
        ),
        packs=packs,
        feature_weights=cfg.feature_weights,
    )


def pack_for(
    deals: list[Transaction],
    target: TargetProfile,
    ctx: MarketContext,
    score: AcquirerScore,
    cfg: ScoringConfig,
    rank: int,
) -> EvidencePack:
    """Evidence pack for any scored acquirer, selected or not (used by the rationale eval)."""
    return _pack(rank, score, {d.transaction_id: d for d in deals}, target, ctx, cfg)


def _pack(
    rank: int,
    s: AcquirerScore,
    by_id: dict[str, Transaction],
    target: TargetProfile,
    ctx: MarketContext,
    cfg: ScoringConfig,
) -> EvidencePack:
    ds = [by_id[r.transaction_id] for r in s.deal_relevance]
    relevant = [
        by_id[r.transaction_id]
        for r in s.deal_relevance
        if r.is_relevant(cfg.relevant_deal_threshold)
    ]
    precedents = [
        Precedent(
            **by_id[r.transaction_id].model_dump(
                include=set(Precedent.model_fields) - {"rationale_tags", "relevance", "outcome"}
            ),
            outcome=by_id[r.transaction_id].outcome.value,
            rationale_tags=list(by_id[r.transaction_id].rationale_tags),
            relevance=round(r.relevance, 3),
        )
        for r in s.deal_relevance[: cfg.precedents_per_pack]
    ]
    bidders = [d.num_bidders for d in relevant if d.num_bidders is not None]
    profile = _acquirer_profile(ds, target, ctx.as_of_year)
    return EvidencePack(
        rank=rank,
        acquirer=s.acquirer,
        acquirer_type=s.acquirer_type,
        score=round(s.score, 3),
        score_breakdown={f: round(v.shrunk, 2) for f, v in s.features.items()},
        n_relevant_deals=s.n_relevant_deals,
        conviction_cap=s.conviction_cap,
        profile=profile,
        own_relevant_multiples=multiple_stats([d for d in relevant if d.outcome == Outcome.CLOSED]),
        precedents=precedents,
        risk_signals=RiskSignals(
            median_bidders_on_relevant_deals=(
                _median([float(b) for b in bidders]) if len(bidders) >= MIN_N_FOR_MEDIAN else None
            ),
            bidders_on_relevant_deals=sorted(bidders) if len(bidders) < MIN_N_FOR_MEDIAN else [],
            leveraged_share_of_deals=round(
                sum(d.financing_type == "Leveraged" for d in ds) / len(ds), 2
            ),
            withdrawn_or_terminated_share=round(profile.withdrawn_or_terminated / len(ds), 2),
            deals_in_target_sector=profile.deals_in_target_sector,
            single_sector_buyer=len(profile.deals_by_sector) == 1,
        ),
    )
