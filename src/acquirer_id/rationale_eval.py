"""Golden set 2: score rationales against facts and written rules on fixed cases.

Cases are (target, acquirer) pairs. Evidence packs are rebuilt from the CSV at eval time and
compared to a stored hash, so a ranking change that alters the writer's input is visible.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel

from acquirer_id.config import LLMConfig, ScoringConfig, TargetProfile
from acquirer_id.data import Transaction
from acquirer_id.evidence import EvidencePack, RankingReport, build_report, pack_for
from acquirer_id.llm import ModelClient
from acquirer_id.ranking import build_context, score_acquirers, select
from acquirer_id.rationale import RationaleResult, load_prompt, write_one
from acquirer_id.tracing import Tracer


class RationaleCase(BaseModel):
    case_id: str
    target: TargetProfile
    acquirer: str
    tags: list[str]
    pack_sha: str


def pack_sha(pack: EvidencePack) -> str:
    return hashlib.sha256(pack.model_dump_json().encode()).hexdigest()[:12]


def case_inputs(
    deals: list[Transaction], target: TargetProfile, acquirer: str, cfg: ScoringConfig
) -> tuple[RankingReport, EvidencePack]:
    ctx = build_context(deals, target, cfg)
    scores = score_acquirers(deals, target, ctx, cfg)
    report = build_report(deals, target, ctx, select(scores, ctx, cfg), cfg)
    pack = next((p for p in report.packs if p.acquirer == acquirer), None)
    if pack is None:
        rank, score = next((i, s) for i, s in enumerate(scores, 1) if s.acquirer == acquirer)
        pack = pack_for(deals, target, ctx, score, cfg, rank)
    return report, pack


def freeze(
    deals: list[Transaction],
    default_target: TargetProfile,
    alt_target: TargetProfile,
    extra_default_acquirers: list[str],
    cfg: ScoringConfig,
) -> list[RationaleCase]:
    """The default target's full list (so run-level duplication is measurable) plus hard extras."""
    cases = []

    def add(target: TargetProfile, acquirer: str, tags: list[str]) -> None:
        _, pack = case_inputs(deals, target, acquirer, cfg)
        auto = []
        if pack.n_relevant_deals <= cfg.conviction_cap_max_relevant:
            auto.append("thin_evidence")
        if pack.profile.withdrawn_or_terminated:
            auto.append("has_withdrawn_or_terminated")
        if any(p.outcome != "Closed" for p in pack.precedents):
            auto.append("non_closed_precedent")
        slug = re.sub(r"\W+", "-", f"{target.sector}-{target.deal_size_mm:g}-{acquirer}").lower()
        cases.append(
            RationaleCase(
                case_id=slug.strip("-"),
                target=target,
                acquirer=acquirer,
                tags=tags + [pack.acquirer_type.value] + auto,
                pack_sha=pack_sha(pack),
            )
        )

    ctx = build_context(deals, default_target, cfg)
    sel = select(score_acquirers(deals, default_target, ctx, cfg), ctx, cfg)
    for s in sel.selected:
        add(default_target, s.acquirer, ["default_list"])
    for a in extra_default_acquirers:
        add(default_target, a, ["extra"])
    ctx_alt = build_context(deals, alt_target, cfg)
    sel_alt = select(score_acquirers(deals, alt_target, ctx_alt, cfg), ctx_alt, cfg)
    for t in {s.acquirer_type for s in sel_alt.selected}:
        top = next(s for s in sel_alt.selected if s.acquirer_type == t)
        add(alt_target, top.acquirer, ["generalization"])
    return cases


# ---------- scoring ----------


class CaseScore(BaseModel):
    case_id: str
    tags: list[str]
    pack_drift: bool
    result: RationaleResult


def headline_similarity(results: list[RationaleResult]) -> dict[str, float]:
    """Pairwise similarity of headlines with the acquirer's own name masked (1.0 = identical)."""
    heads = [r.rationale.headline.replace(r.acquirer, "X").lower() for r in results if r.rationale]
    sims = [SequenceMatcher(None, a, b).ratio() for a, b in itertools.combinations(heads, 2)]
    openings = Counter(" ".join(h.split()[:4]) for h in heads)
    return {
        "mean": round(statistics.fmean(sims), 3) if sims else 0.0,
        "max": round(max(sims), 3) if sims else 0.0,
        "most_common_opening_share": round(openings.most_common(1)[0][1] / len(heads), 2)
        if heads
        else 0.0,
    }


def summarize(scores: list[CaseScore]) -> dict:
    results = [s.result for s in scores]
    done = [r for r in results if r.rationale]
    default = [s.result for s in scores if "default_list" in s.tags]
    convictions = Counter(r.rationale.conviction for r in done)
    return {
        "cases": len(scores),
        "pass_rate": round(sum(r.status == "ok" for r in results) / len(results), 2),
        "failed": sum(r.status == "failed" for r in results),
        "issues": dict(Counter(i.code for r in results for i in r.issues).most_common()),
        "pack_drift": [s.case_id for s in scores if s.pack_drift],
        "headline_similarity_default_list": headline_similarity(default),
        "convictions": dict(convictions),
        "latency_ms_p50": int(statistics.median(r.latency_ms for r in done)) if done else None,
        "latency_ms_max": max((r.latency_ms for r in done), default=None),
        "tokens_in": sum(r.input_tokens for r in results),
        "tokens_out": sum(r.output_tokens for r in results),
    }


def run_eval(
    deals: list[Transaction],
    cases: list[RationaleCase],
    client: ModelClient,
    llm_cfg: LLMConfig,
    scoring: ScoringConfig,
    tracer: Tracer,
) -> list[CaseScore]:
    system = load_prompt(llm_cfg.prompt_version)

    def one(case: RationaleCase) -> CaseScore:
        report, pack = case_inputs(deals, case.target, case.acquirer, scoring)
        result = write_one(report, pack, client, llm_cfg, system, tracer, cache_dir=None)
        return CaseScore(
            case_id=case.case_id,
            tags=case.tags,
            pack_drift=pack_sha(pack) != case.pack_sha,
            result=result,
        )

    with ThreadPoolExecutor(max_workers=llm_cfg.max_workers) as pool:
        return list(pool.map(one, cases))


def load_cases(path: Path) -> list[RationaleCase]:
    import json

    return [RationaleCase.model_validate(c) for c in json.loads(path.read_text())["cases"]]
