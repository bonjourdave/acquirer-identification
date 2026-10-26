"""Target profile and scoring configuration, loaded from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class TargetProfile(BaseModel):
    """The company being sold. Everything target-specific enters the pipeline through here."""

    name: str = "Target"
    sector: str
    deal_size_mm: float = Field(gt=0)
    ownership: str | None = "Private"
    # None = unspecified. "regional" without a named region only down-weights national deals.
    region: str | None = None
    regional: bool = True
    # None = "strong": the target sector's margin percentile given by strong_margin_percentile.
    ebitda_margin_pct: float | None = None
    description: str = ""


class ScoringConfig(BaseModel):
    """Every knob of the ranking stage. Hand-set and judged by the backtest."""

    k: int = Field(default=10, ge=1)
    min_per_type: int = Field(default=1, ge=0)  # a process-design floor, not a likelihood claim
    mix_mode: Literal["proportional", "score"] = "proportional"
    # Where the strategic share for the mix comes from. "sector" (every deal in the target sector)
    # replaced "comparables" (the narrow comparable set, too few deals to size the mix) after
    # the backtest.
    mix_share_basis: Literal["comparables", "sector"] = "sector"
    # Strategics in this data stay in one sector (median 1; sector association p < 0.001), so
    # crediting them for adjacent-sector deals only adds candidates who never buy here.
    strategic_adjacency: bool = False

    # Deal relevance
    size_sigma_ln: float = (
        0.693  # ln(2): a deal 2x off the target size keeps exp(-0.5) of its weight
    )
    recency_half_life_years: float = 4.0
    adjacency_scale: float = 0.8  # max weight an adjacent sector can reach (own sector = 1.0)
    adjacency_lift_cap: float = 2.0
    outcome_weights: dict[str, float] = {
        "Closed": 1.0,
        "Pending": 0.8,
        "Rumored": 0.3,
        "Withdrawn": 0.3,
        "Terminated": 0.3,
    }
    ownership_match_weight: float = 1.0
    ownership_mismatch_weight: float = 0.8
    national_deal_weight_for_regional_target: float = 0.85
    region_match_weight: float = 1.2

    # Acquirer score = weighted sum of features in [0, 1]
    feature_weights: dict[str, float] = {
        "relevant_activity": 0.40,
        "focus": 0.25,
        "size_fit": 0.15,
        "recency": 0.10,
        "close_rate": 0.10,
    }
    shrinkage_strength: float = 3.0  # pseudo-deals of the type prior blended into each ratio

    # A deal counts as "relevant" evidence when its sector weight and size similarity both pass this
    relevant_deal_threshold: float = 0.5
    conviction_cap_max_relevant: int = 2  # <= this many relevant deals caps conviction at Medium
    # Listed only with at least this many relevant deals; a slot the type cannot fill goes to the
    # other type rather than to a buyer with no similar deal.
    min_relevant_deals: int = Field(default=1, ge=0)

    # Comparable set for mix and valuation comps, with a fallback ladder
    comps_size_band: tuple[float, float] = (0.5, 2.0)
    comps_wide_size_band: tuple[float, float] = (0.25, 4.0)
    comps_min_deals: int = 15
    # Lift from ~26 target-sector buyers: 1.04-1.19 is noise, so require a clear margin
    adjacent_lift_threshold: float = 1.25
    strong_margin_percentile: float = 0.75

    # Inputs outside this multiple of the dataset's size range are refused, not guessed at
    size_out_of_range_factor: float = 4.0

    precedents_per_pack: int = 6


class LLMConfig(BaseModel):
    """Writer stage settings. Model names are config, never code."""

    model: str = "gpt-6.1-sol"
    fallback_model: str = "gpt-5.6-terra"  # grounded on golden set 2; luna invented facts
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    timeout_s: float = 32.0  # calls take 19-30s; one timeout + retry must fit the 60s budget
    max_attempts: int = Field(default=2, ge=1)  # per model, for API errors and schema failures
    max_workers: int = Field(default=10, ge=1)
    prompt_version: str = "rationale_v4"
    use_cache: bool = True  # identical (model, prompt, input) reuses the stored response
    # USD per 1M tokens, keyed by model. Unknown models report tokens without a cost.
    # OpenAI standard tier, from the developers.openai.com model pages, retrieved 2026-10-24.
    # Cached-input discounts are ignored, so a run's cost is an upper bound.
    prices_per_mtok: dict[str, dict[str, float]] = {
        "gpt-6.1-sol": {"input": 2.00, "output": 10.00},
        "gpt-5.6-terra": {"input": 2.00, "output": 12.00},
        "gpt-6-luna": {"input": 0.10, "output": 0.50},
    }


class EntityNotes(BaseModel):
    """Outside-the-dataset facts about acquirers, each a claim to verify."""

    as_of: str = ""
    source: str = ""
    aliases: dict[str, str] = {}  # old name -> current entity
    status: dict[str, str] = {}  # entity -> status note shown to the banker, not the model


class RunConfig(BaseModel):
    target: TargetProfile
    scoring: ScoringConfig = ScoringConfig()
    llm: LLMConfig = LLMConfig()
    entity_notes: Path | None = None  # in YAML: relative to the config file's directory


def load_run_config(path: Path) -> RunConfig:
    with path.open(encoding="utf-8") as f:
        run = RunConfig.model_validate(yaml.safe_load(f))
    if run.entity_notes is not None and not run.entity_notes.is_absolute():
        run.entity_notes = path.parent / run.entity_notes  # works from any working directory
    return run


def load_entity_notes(path: Path | None) -> EntityNotes:
    if path is None:
        return EntityNotes()
    with path.open(encoding="utf-8") as f:
        return EntityNotes.model_validate(yaml.safe_load(f))


def with_target_overrides(run: RunConfig, overrides: dict) -> RunConfig:
    """The run with target fields replaced. A changed target gets a name and description built
    from its fields, so the config's brief-specific wording never reaches the writer for another
    target."""
    if not overrides:
        return run
    t = run.target.model_dump() | overrides
    if "name" not in overrides:
        t["name"] = f"~${t['deal_size_mm']:,.0f}M {t['sector']} target"
    geo = (
        f"in the {t['region']} region"
        if t["region"]
        else ("regional" if t["regional"] else "national")
    )
    margin = f", {t['ebitda_margin_pct']}% EBITDA margin" if t["ebitda_margin_pct"] else ""
    t["description"] = (
        f"{t['ownership'] or 'Unspecified-ownership'} {t['sector']} company, {geo}{margin}."
    )
    return run.model_copy(update={"target": TargetProfile.model_validate(t)})
