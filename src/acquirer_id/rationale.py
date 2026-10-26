"""Writer stage: one LLM call per acquirer, validated before anything is rendered.

Flow per acquirer: build input from the evidence pack -> cache lookup -> model call with bounded
retries and a fallback model -> schema parse -> grounding checks. Every attempt is traced.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from acquirer_id.config import LLMConfig
from acquirer_id.evidence import MIN_N_FOR_MEDIAN, EvidencePack, RankingReport
from acquirer_id.llm import LLMRequest, LLMResponse, ModelClient
from acquirer_id.tracing import Tracer

PROMPTS_DIR = Path(__file__).parents[2] / "prompts"
CONVICTION_ORDER = {"Low": 0, "Medium": 1, "High": 2}
BANNED_PHRASES = (
    "leading",
    "well-positioned",
    "well positioned",
    "proven track record",
    "strong track record",
    "strategic acquisitions",
    "robust",
    "best-in-class",
    "uniquely",
    "compelling opportunity",
)
SMALL_COUNT_MAX = 12  # integers up to this are treated as counts of listed items (rule 2)
MAX_PAGE_WORDS = 330  # model-written words per page; with the precedent facts it fits one page
# Internal ranking vocabulary a banker should never see on a page (prompt v3 rule 10)
JARGON = re.compile(r"\b(scores?|size[- ]fit|lift|relevance)\b", re.I)
# "median ... across 3 deals": a median over fewer than 5 deals (prompt v3 rule 5)
THIN_MEDIAN = re.compile(
    r"\bmedian\b(?:[^.;]|\.\d){0,80}?\b(?:across|on|over|from|of)\s+(\d+)\b", re.I
)


# ---------- output contract ----------


class Section(BaseModel):
    text: str
    cited_ids: list[str]


class PrecedentNote(BaseModel):
    transaction_id: str
    takeaway: str


class PrecedentActivity(BaseModel):
    precedents: list[PrecedentNote]
    pattern: str


class RiskFlag(BaseModel):
    category: Literal[
        "competitive_process",
        "financing",
        "execution",
        "antitrust_or_overlap",
        "integration",
        "appetite",
        "thin_evidence",
        "other",
    ]
    title: str
    detail: str
    cited_ids: list[str]


class Rationale(BaseModel):
    headline: str
    acquirer_overview: Section
    strategic_fit_thesis: Section
    precedent_activity: PrecedentActivity
    valuation_context: Section
    risk_flags: list[RiskFlag]
    conviction: Literal["High", "Medium", "Low"]
    conviction_rationale: str
    data_gaps: list[str]


class Issue(BaseModel):
    code: Literal[
        "ungrounded_number",
        "unknown_id",
        "conviction_above_cap",
        "too_few_risks",
        "banned_phrase",
        "uncited_precedents",
        "outcome_not_stated",
        "templated_headline",
        "score_jargon",
        "thin_median",
        "page_too_long",
        "geography_without_region",
        "placeholder_text",
        "conviction_off_rubric",
    ]
    detail: str


class RationaleResult(BaseModel):
    rank: int
    acquirer: str
    status: Literal["ok", "needs_review", "failed"]
    rationale: Rationale | None
    issues: list[Issue]
    model: str | None  # resolved model that produced the accepted output
    attempts: int
    cache_hit: bool
    error: str | None = None
    trace_span_id: str | None = None  # links eval results back to the trace
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None  # None when the model has no price in config; 0 on a cache hit


# ---------- prompt and input ----------


def load_prompt(version: str) -> str:
    text = (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    declared = re.search(r"^version:\s*(\S+)", front, re.M)
    if not declared or declared.group(1) != version:
        raise ValueError(
            f"prompt file {version}.md declares version {declared and declared.group(1)}"
        )
    return body.strip()


def build_input(report: RankingReport, pack: EvidencePack) -> dict:
    """Everything the writer may use. Nothing else reaches the model."""
    return {
        "target": report.target.model_dump(mode="json"),
        "market": report.valuation.model_dump(mode="json"),
        "mix": report.mix.model_dump(mode="json"),
        "acquirer": pack.model_dump(mode="json"),
    }


# ---------- grounding checks ----------


_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _collect_numbers(obj: object, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int | float):
        out.add(float(obj))
        if 0 < obj <= 1:  # shares may be written as percentages
            out.add(round(obj * 100, 6))
    elif isinstance(obj, str):
        for m in _NUMBER.findall(obj):
            out.add(float(m.replace(",", "")))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_numbers(v, out)


def _collect_ids(obj: object, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "transaction_id" and isinstance(v, str):
                out.add(v)
            elif k == "comp_transaction_ids":
                out.update(v)
            else:
                _collect_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_ids(v, out)


def _is_grounded(written: str, allowed: set[float]) -> bool:
    value = float(written.replace(",", ""))
    if value.is_integer() and "." not in written and value <= SMALL_COUNT_MAX:
        return True
    decimals = len(written.split(".")[1]) if "." in written else 0
    return any(round(a, decimals) == value for a in allowed)


def _texts(r: Rationale) -> list[tuple[str, str]]:
    out = [
        ("headline", r.headline),
        ("acquirer_overview", r.acquirer_overview.text),
        ("strategic_fit_thesis", r.strategic_fit_thesis.text),
        ("precedent_activity.pattern", r.precedent_activity.pattern),
        ("valuation_context", r.valuation_context.text),
        ("conviction_rationale", r.conviction_rationale),
    ]
    out += [(f"precedent {p.transaction_id}", p.takeaway) for p in r.precedent_activity.precedents]
    out += [(f"risk '{f.title}'", f"{f.title}. {f.detail}") for f in r.risk_flags]
    return out


def conviction_rubric(pack: dict, cap: str) -> str:
    """Prompt v2 rule 6, in code, so the eval can measure adherence."""
    n_rel = pack["n_relevant_deals"]
    size_fit = pack["score_breakdown"]["size_fit"]
    if n_rel <= 1 or pack["profile"]["deals_last_3_years"] == 0 or size_fit < 0.5:
        level = "Low"
    elif n_rel >= 3 and size_fit >= 0.7:
        level = "High"
    else:
        level = "Medium"
    return min(level, cap, key=CONVICTION_ORDER.__getitem__)


def check(r: Rationale, writer_input: dict, conviction_cap: str) -> list[Issue]:
    acquirer = writer_input["acquirer"]["acquirer"]
    allowed_numbers: set[float] = set()
    _collect_numbers(writer_input, allowed_numbers)
    known_ids: set[str] = set()
    _collect_ids(writer_input, known_ids)
    issues: list[Issue] = []

    cited = (
        r.acquirer_overview.cited_ids
        + r.strategic_fit_thesis.cited_ids
        + r.valuation_context.cited_ids
        + [p.transaction_id for p in r.precedent_activity.precedents]
        + [i for f in r.risk_flags for i in f.cited_ids]
    )
    for i in sorted(set(cited) - known_ids):
        issues.append(Issue(code="unknown_id", detail=f"cited {i!r}, not in the evidence pack"))

    id_pattern = re.compile(
        "|".join(re.escape(i) for i in sorted(known_ids, key=len, reverse=True))
    )
    for where, text in _texts(r):
        scrubbed = id_pattern.sub(" ", text) if known_ids else text
        for n in _NUMBER.findall(scrubbed):
            if not _is_grounded(n, allowed_numbers):
                issues.append(Issue(code="ungrounded_number", detail=f"{where}: {n}"))
        lowered = text.lower()
        for phrase in BANNED_PHRASES:
            if phrase in lowered:
                issues.append(Issue(code="banned_phrase", detail=f"{where}: {phrase!r}"))

    if CONVICTION_ORDER[r.conviction] > CONVICTION_ORDER[conviction_cap]:
        issues.append(
            Issue(code="conviction_above_cap", detail=f"{r.conviction} > cap {conviction_cap}")
        )
    if len(r.risk_flags) < 2:
        issues.append(Issue(code="too_few_risks", detail=f"{len(r.risk_flags)} risk flag(s)"))
    if not r.precedent_activity.precedents:
        issues.append(Issue(code="uncited_precedents", detail="no precedents cited"))

    # A cited deal that did not close must be described as what it was (prompt rule 4).
    outcomes = {p["transaction_id"]: p["outcome"] for p in writer_input["acquirer"]["precedents"]}
    page = " ".join(text for _, text in _texts(r)).lower()
    for note in r.precedent_activity.precedents:
        outcome = outcomes.get(note.transaction_id)
        if outcome and outcome != "Closed" and outcome.lower() not in page:
            issues.append(
                Issue(code="outcome_not_stated", detail=f"{note.transaction_id} is {outcome}")
            )
    if re.match(rf"\s*(the\s+)?{re.escape(acquirer)}\b", r.headline, re.I):
        issues.append(Issue(code="templated_headline", detail="headline opens with the name"))
    if re.search(r"\bgives\b.{0,80}\bprecedent\b", r.headline, re.I):
        issues.append(Issue(code="templated_headline", detail="'<deal> gives <buyer> a precedent'"))
    expected = conviction_rubric(writer_input["acquirer"], conviction_cap)
    if r.conviction != expected:
        issues.append(
            Issue(code="conviction_off_rubric", detail=f"{r.conviction}, rubric gives {expected}")
        )
    # The status check moved to the report's front page, so pages are not checked for it.
    for where, text in _texts(r):
        if m := JARGON.search(text):
            issues.append(Issue(code="score_jargon", detail=f"{where}: {m.group(0)!r}"))
        for m in THIN_MEDIAN.finditer(text):
            if int(m.group(1)) < MIN_N_FOR_MEDIAN:
                issues.append(Issue(code="thin_median", detail=f"{where}: {m.group(0)!r}"))
    for where, text in _texts(r):
        if len(text.split()) < 3:  # e.g. a pattern of "REMOVE" (golden set 2, prompt v3 run 1)
            issues.append(Issue(code="placeholder_text", detail=f"{where}: {text!r}"))
    words = sum(len(t.split()) for _, t in _texts(r)) + sum(len(g.split()) for g in r.data_gaps)
    if words > MAX_PAGE_WORDS:
        issues.append(Issue(code="page_too_long", detail=f"{words} words > {MAX_PAGE_WORDS}"))
    if writer_input["target"]["profile"].get("region") is None and "geograph" in page:
        issues.append(Issue(code="geography_without_region", detail="target region is unknown"))
    return issues


# ---------- generation ----------


def _cache_key(model: str, system: str, user: str) -> str:
    return hashlib.sha256("\x1f".join((model, system, user)).encode()).hexdigest()


def write_one(
    report: RankingReport,
    pack: EvidencePack,
    client: ModelClient,
    cfg: LLMConfig,
    system: str,
    tracer: Tracer,
    cache_dir: Path | None,
) -> RationaleResult:
    writer_input = build_input(report, pack)
    user = json.dumps(writer_input, indent=1)
    attempts, last_error = 0, None
    for model in (cfg.model, cfg.fallback_model):
        key = _cache_key(model, system, user)
        cached = cache_dir / f"{key}.json" if cache_dir else None
        for attempt in range(1, cfg.max_attempts + 1):
            attempts += 1
            span = {
                "acquirer": pack.acquirer,
                "rank": pack.rank,
                "model": model,
                "attempt": attempt,
            }
            try:
                if cached and cached.exists():
                    resp, hit = LLMResponse.model_validate_json(cached.read_text()), True
                else:
                    resp = client.generate(
                        LLMRequest(
                            model=model,
                            system=system,
                            user=user,
                            reasoning_effort=cfg.reasoning_effort,
                            timeout_s=cfg.timeout_s,
                        ),
                        Rationale,
                    )
                    hit = False
                rationale = Rationale.model_validate_json(resp.parsed_json)
            except Exception as e:  # API error, timeout, schema failure: all bounded below
                last_error = f"{type(e).__name__}: {str(e)[:300]}"
                tracer.span(
                    "rationale.generate", span, status="error", error=last_error, input=user
                )
                continue
            if cached and not hit:
                cached.write_text(resp.model_dump_json())
            issues = check(rationale, writer_input, pack.conviction_cap)
            if any(i.code == "conviction_above_cap" for i in issues):
                rationale.conviction = pack.conviction_cap  # enforce the evidence rule
            cost = 0.0 if hit else _cost(cfg, model, resp)  # a cache hit makes no API call
            span_id = tracer.span(
                "rationale.generate",
                span,
                status="ok",
                cache_hit=hit,
                response_model=resp.resolved_model,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cost_usd=cost,
                latency_ms=0 if hit else resp.latency_ms,
                input=user,
                output=json.loads(resp.parsed_json),
                issues=[i.model_dump() for i in issues],
            )
            return RationaleResult(
                rank=pack.rank,
                acquirer=pack.acquirer,
                status="needs_review" if issues else "ok",
                rationale=rationale,
                issues=issues,
                model=resp.resolved_model,
                attempts=attempts,
                cache_hit=hit,
                trace_span_id=span_id,
                latency_ms=0 if hit else resp.latency_ms,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cost_usd=cost,
            )
    return RationaleResult(
        rank=pack.rank,
        acquirer=pack.acquirer,
        status="failed",
        rationale=None,
        issues=[],
        model=None,
        attempts=attempts,
        cache_hit=False,
        error=last_error,
    )


def _cost(cfg: LLMConfig, model: str, resp: LLMResponse) -> float | None:
    price = cfg.prices_per_mtok.get(model)
    if not price:
        return None
    return round(
        (resp.input_tokens * price["input"] + resp.output_tokens * price["output"]) / 1e6, 5
    )


def write_all(
    report: RankingReport,
    client: ModelClient,
    cfg: LLMConfig,
    tracer: Tracer,
    cache_dir: Path | None,
    on_result: Callable[[RationaleResult], None] | None = None,
) -> list[RationaleResult]:
    """Write every page in parallel. `on_result` runs in the caller's thread as each finishes,
    so a UI can show progress without touching worker threads."""
    system = load_prompt(cfg.prompt_version)
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers) as pool:
        futures = [
            pool.submit(write_one, report, p, client, cfg, system, tracer, cache_dir)
            for p in report.packs
        ]
        for f in as_completed(futures):
            results.append(f.result())
            if on_result:
                on_result(results[-1])
    return sorted(results, key=lambda r: r.rank)
