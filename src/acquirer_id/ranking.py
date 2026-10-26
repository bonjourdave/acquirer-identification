"""Deterministic acquirer ranking.

Pipeline: validate target -> market context (sector adjacency, comparable set, mix share)
-> per-deal relevance -> per-acquirer features with shrinkage -> type-aware selection.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

from pydantic import BaseModel

from acquirer_id.config import ScoringConfig, TargetProfile
from acquirer_id.data import AcquirerType, Outcome, Transaction

NATIONAL_GEOGRAPHIES = {"National", "Multi-Regional"}
FEATURES = ("relevant_activity", "focus", "size_fit", "recency", "close_rate")


class TargetOutOfRange(ValueError):
    """The dataset cannot support a ranking for this target; refuse rather than invent one."""


# ---------- market context ----------


class ComparableSet(BaseModel):
    step: str  # which rung of the fallback ladder produced it
    transaction_ids: list[str]
    strategic_share: float

    @property
    def n(self) -> int:
        return len(self.transaction_ids)


@dataclass(frozen=True)
class MarketContext:
    as_of_year: int
    sector_lift: dict[str, float]
    sector_weight: dict[str, float]
    target_margin_pct: float
    margin_is_assumed: bool
    comparables: ComparableSet
    sector_strategic_share: float  # strategic share of all deals in the target sector
    sector_deal_count: int

    def strategic_share(self, basis: str) -> float:
        return (
            self.sector_strategic_share if basis == "sector" else self.comparables.strategic_share
        )


def validate_target(deals: list[Transaction], target: TargetProfile, cfg: ScoringConfig) -> None:
    sectors = {d.sector for d in deals}
    if target.sector not in sectors:
        raise TargetOutOfRange(
            f"Sector {target.sector!r} has no deals in the dataset. Known sectors: "
            f"{', '.join(sorted(sectors))}."
        )
    sizes = [d.deal_size_mm for d in deals]
    lo, hi = min(sizes) / cfg.size_out_of_range_factor, max(sizes) * cfg.size_out_of_range_factor
    if not lo <= target.deal_size_mm <= hi:
        raise TargetOutOfRange(
            f"Deal size ${target.deal_size_mm:,.0f}M is outside the range the dataset can "
            f"support (${lo:,.1f}M-${hi:,.0f}M)."
        )


def sector_adjacency(
    deals: list[Transaction], sector: str, cfg: ScoringConfig
) -> tuple[dict[str, float], dict[str, float]]:
    """Lift of each other sector among buyers of `sector`, vs all deals; and the derived weight.

    lift(T) = share of T in the target-sector buyers' other deals / share of T in all other deals.
    Own sector weight is 1.0. Only lift at or above adjacent_lift_threshold counts: a sector this
    buyer group visits no more often than chance is not adjacent, so its weight is 0. Excess lift
    scales the weight linearly up to adjacency_scale at adjacency_lift_cap.
    """
    buyers = {d.acquirer for d in deals if d.sector == sector}
    others = [d for d in deals if d.sector != sector]
    buyer_others = [d for d in others if d.acquirer in buyers]
    base, among = Counter(d.sector for d in others), Counter(d.sector for d in buyer_others)
    lift: dict[str, float] = {sector: float("nan")}
    weight: dict[str, float] = {sector: 1.0}
    for s, n in base.items():
        lift[s] = (among[s] / len(buyer_others)) / (n / len(others)) if buyer_others else 0.0
        excess = (min(lift[s], cfg.adjacency_lift_cap) - 1) / (cfg.adjacency_lift_cap - 1)
        # Below the threshold a sector's lift is indistinguishable from chance
        weight[s] = (
            cfg.adjacency_scale * max(excess, 0.0)
            if lift[s] >= cfg.adjacent_lift_threshold
            else 0.0
        )
    return lift, weight


def size_similarity(size_mm: float, target_mm: float, sigma_ln: float) -> float:
    return math.exp(-(math.log(size_mm / target_mm) ** 2) / (2 * sigma_ln**2))


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def comparable_set(
    deals: list[Transaction],
    target: TargetProfile,
    sector_weight: dict[str, float],
    cfg: ScoringConfig,
) -> ComparableSet:
    """Closed deals like the target, widening step by step when too few exist."""
    closed = [d for d in deals if d.outcome == Outcome.CLOSED]
    near = {s for s, w in sector_weight.items() if w >= cfg.relevant_deal_threshold}

    def in_band(d: Transaction, band: tuple[float, float]) -> bool:
        return band[0] <= d.deal_size_mm / target.deal_size_mm <= band[1]

    lo, hi = cfg.comps_size_band
    wlo, whi = cfg.comps_wide_size_band
    ladder = [
        (
            f"target + adjacent sectors, {lo:g}-{hi:g}x size",
            lambda d: d.sector in near and in_band(d, cfg.comps_size_band),
        ),
        (
            f"target + adjacent sectors, {wlo:g}-{whi:g}x size",
            lambda d: d.sector in near and in_band(d, cfg.comps_wide_size_band),
        ),
        (f"all sectors, {lo:g}-{hi:g}x size", lambda d: in_band(d, cfg.comps_size_band)),
        ("all closed deals", lambda d: True),
    ]
    for step, pred in ladder:
        picked = [d for d in closed if pred(d)]
        if len(picked) >= cfg.comps_min_deals or step == "all closed deals":
            strategic = sum(d.acquirer_type == AcquirerType.STRATEGIC for d in picked)
            return ComparableSet(
                step=step,
                transaction_ids=[d.transaction_id for d in picked],
                strategic_share=strategic / len(picked) if picked else 0.0,
            )
    raise AssertionError("unreachable: the last ladder step always returns")


def build_context(
    deals: list[Transaction], target: TargetProfile, cfg: ScoringConfig
) -> MarketContext:
    validate_target(deals, target, cfg)
    lift, weight = sector_adjacency(deals, target.sector, cfg)
    in_sector = [d for d in deals if d.sector == target.sector]
    if target.ebitda_margin_pct is not None:
        margin, assumed = target.ebitda_margin_pct, False
    else:
        sector_margins = [
            d.ebitda_margin_pct
            for d in deals
            if d.sector == target.sector and d.ebitda_margin_pct is not None
        ]
        margin, assumed = _percentile(sector_margins, cfg.strong_margin_percentile), True
    return MarketContext(
        as_of_year=max(d.deal_year for d in deals),
        sector_lift=lift,
        sector_weight=weight,
        target_margin_pct=round(margin, 1),
        margin_is_assumed=assumed,
        comparables=comparable_set(deals, target, weight, cfg),
        sector_strategic_share=sum(d.acquirer_type == AcquirerType.STRATEGIC for d in in_sector)
        / len(in_sector),
        sector_deal_count=len(in_sector),
    )


# ---------- deal relevance ----------


class DealRelevance(BaseModel):
    transaction_id: str
    sector_weight: float
    size_similarity: float
    recency: float
    relevance: float

    def is_relevant(self, threshold: float) -> bool:
        return self.sector_weight >= threshold and self.size_similarity >= threshold


def deal_relevance(
    d: Transaction, ctx: MarketContext, target: TargetProfile, cfg: ScoringConfig
) -> DealRelevance:
    if d.acquirer_type == AcquirerType.STRATEGIC and not cfg.strategic_adjacency:
        sector_w = 1.0 if d.sector == target.sector else 0.0
    else:
        sector_w = ctx.sector_weight.get(d.sector, 0.0)
    size_sim = size_similarity(d.deal_size_mm, target.deal_size_mm, cfg.size_sigma_ln)
    recency = 0.5 ** ((ctx.as_of_year - d.deal_year) / cfg.recency_half_life_years)
    w = sector_w * size_sim * recency * cfg.outcome_weights.get(d.outcome.value, 0.0)
    if target.ownership:
        own_match = d.target_ownership_pre == target.ownership
        w *= cfg.ownership_match_weight if own_match else cfg.ownership_mismatch_weight
    if target.region and d.geography == target.region:
        w *= cfg.region_match_weight
    elif target.regional and d.geography in NATIONAL_GEOGRAPHIES:
        w *= cfg.national_deal_weight_for_regional_target
    return DealRelevance(
        transaction_id=d.transaction_id,
        sector_weight=sector_w,
        size_similarity=size_sim,
        recency=recency,
        relevance=w,
    )


# ---------- acquirer scoring ----------


class FeatureValue(BaseModel):
    raw: float  # the acquirer's own observed value
    shrunk: float  # after blending toward the acquirer-type prior by deal count


class AcquirerScore(BaseModel):
    acquirer: str
    acquirer_type: AcquirerType
    n_deals: int
    n_relevant_deals: int
    features: dict[str, FeatureValue]
    score: float
    conviction_cap: str  # "High" or "Medium"; the writer may not exceed it
    deal_relevance: list[DealRelevance]


def _shrink(numerator: float, n: int, prior: float, strength: float) -> float:
    return (numerator + strength * prior) / (n + strength)


def score_acquirers(
    deals: list[Transaction], target: TargetProfile, ctx: MarketContext, cfg: ScoringConfig
) -> list[AcquirerScore]:
    by_acq: dict[str, list[Transaction]] = defaultdict(list)
    for d in deals:
        by_acq[d.acquirer].append(d)
    rel = {d.transaction_id: deal_relevance(d, ctx, target, cfg) for d in deals}

    # Per-acquirer sums of each ratio feature's numerator, then type-level priors from the pool.
    sums: dict[str, dict[str, float]] = {}
    for a, ds in by_acq.items():
        rs = [rel[d.transaction_id] for d in ds]
        median_size = statistics.median(d.deal_size_mm for d in ds)
        sums[a] = {
            "focus": sum(r.sector_weight * r.size_similarity for r in rs),
            "size_fit": len(ds)
            * size_similarity(median_size, target.deal_size_mm, cfg.size_sigma_ln),
            "recency": sum(r.recency for r in rs),
            "close_rate": sum(d.outcome == Outcome.CLOSED for d in ds),
        }
    priors: dict[AcquirerType, dict[str, float]] = {}
    for t in AcquirerType:
        members = [a for a, ds in by_acq.items() if ds[0].acquirer_type == t]
        n_type = sum(len(by_acq[a]) for a in members)
        priors[t] = {
            f: (sum(sums[a][f] for a in members) / n_type if n_type else 0.0)
            for f in ("focus", "size_fit", "recency", "close_rate")
        }

    activity = {a: sum(rel[d.transaction_id].relevance for d in ds) for a, ds in by_acq.items()}
    max_activity = max(activity.values()) or 1.0

    scores = []
    for a, ds in by_acq.items():
        n, a_type = len(ds), ds[0].acquirer_type
        rs = [rel[d.transaction_id] for d in ds]
        if not any(r.sector_weight > 0 for r in rs):
            continue  # never bought in the target sector or an adjacent one: not a candidate
        feats = {
            "relevant_activity": FeatureValue(
                raw=activity[a], shrunk=math.log1p(activity[a]) / math.log1p(max_activity)
            )
        }
        for f in ("focus", "size_fit", "recency", "close_rate"):
            feats[f] = FeatureValue(
                raw=sums[a][f] / n,
                shrunk=_shrink(sums[a][f], n, priors[a_type][f], cfg.shrinkage_strength),
            )
        n_rel = sum(r.is_relevant(cfg.relevant_deal_threshold) for r in rs)
        scores.append(
            AcquirerScore(
                acquirer=a,
                acquirer_type=a_type,
                n_deals=n,
                n_relevant_deals=n_rel,
                features=feats,
                score=sum(cfg.feature_weights[f] * feats[f].shrunk for f in FEATURES),
                conviction_cap="Medium" if n_rel <= cfg.conviction_cap_max_relevant else "High",
                deal_relevance=sorted(rs, key=lambda r: -r.relevance),
            )
        )
    return sorted(scores, key=lambda s: (-s.score, s.acquirer))


# ---------- selection ----------


class Selection(BaseModel):
    selected: list[AcquirerScore]
    strategic_slots: int
    strategic_slots_from_data: float  # k x comparable strategic share, before the floor
    floor_applied: bool
    next_by_type: dict[AcquirerType, str | None]


def select(scores: list[AcquirerScore], ctx: MarketContext, cfg: ScoringConfig) -> Selection:
    """Fill k slots in proportion to the comparable set's strategic share, with a per-type floor.

    mix_mode "score" ignores type and takes the top k by score; it exists as a backtest variant.
    """
    from_data = cfg.k * ctx.strategic_share(cfg.mix_share_basis)
    if cfg.mix_mode == "score":
        top = scores[: cfg.k]
        return Selection(
            selected=top,
            strategic_slots=sum(s.acquirer_type == AcquirerType.STRATEGIC for s in top),
            strategic_slots_from_data=round(from_data, 2),
            floor_applied=False,
            next_by_type={t: None for t in AcquirerType},
        )
    eligible = [s for s in scores if s.n_relevant_deals >= cfg.min_relevant_deals]
    pools = {t: [s for s in eligible if s.acquirer_type == t] for t in AcquirerType}
    n_strat = min(max(round(from_data), cfg.min_per_type), cfg.k - cfg.min_per_type)
    floor_applied = n_strat != round(from_data)
    # A type with too few candidates hands its slots to the other type.
    n_strat = min(n_strat, len(pools[AcquirerType.STRATEGIC]))
    n_spons = min(cfg.k - n_strat, len(pools[AcquirerType.SPONSOR]))
    n_strat = min(cfg.k - n_spons, len(pools[AcquirerType.STRATEGIC]))
    take = {AcquirerType.STRATEGIC: n_strat, AcquirerType.SPONSOR: n_spons}
    selected = sorted(
        (s for t in AcquirerType for s in pools[t][: take[t]]), key=lambda s: (-s.score, s.acquirer)
    )
    nxt = {
        t: (pools[t][take[t]].acquirer if len(pools[t]) > take[t] else None) for t in AcquirerType
    }
    return Selection(
        selected=selected,
        strategic_slots=n_strat,
        strategic_slots_from_data=round(from_data, 2),
        floor_applied=floor_applied,
        next_by_type=nxt,
    )
