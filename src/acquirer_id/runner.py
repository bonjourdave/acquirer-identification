"""One end-to-end run with its artifacts on disk, shared by the CLI and the app.

rank -> write (parallel, with per-page progress) -> report.md + report.html + JSON + run.json.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from acquirer_id import backtest
from acquirer_id import report as report_md
from acquirer_id.config import RunConfig, ScoringConfig
from acquirer_id.data import load_transactions
from acquirer_id.evidence import RankingReport
from acquirer_id.llm import ModelClient
from acquirer_id.pipeline import rank, write
from acquirer_id.rationale import RationaleResult
from acquirer_id.report_html import render_html
from acquirer_id.tracing import Tracer

GOLDEN_CASES = Path("evals/golden/ranking_cases.json")
IN_SECTOR_BASELINE = "baseline_most_active_in_sector"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_id(config_path: Path, csv_path: Path, overrides: dict | None = None) -> str:
    extra = json.dumps(overrides, sort_keys=True).encode() if overrides else b""
    digest = hashlib.sha256(config_path.read_bytes() + csv_path.read_bytes() + extra).hexdigest()
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{digest[:8]}"


def backtest_summary(csv_path: Path, cfg: ScoringConfig) -> dict | None:
    """Scorer vs the in-sector baseline on every frozen case, for the report's summary page.

    Only when the CSV is the one the cases were frozen against: other data, no claim.
    """
    if not GOLDEN_CASES.exists():
        return None
    if json.loads(GOLDEN_CASES.read_text())["csv_sha256"] != sha256(csv_path):
        return None
    deals = load_transactions(csv_path)
    cases = backtest.load_cases(GOLDEN_CASES)
    ours = backtest.metrics(backtest.evaluate(deals, cases, backtest.rank_scorer, cfg))
    base = backtest.metrics(
        backtest.evaluate(deals, cases, backtest.RANKERS[IN_SECTOR_BASELINE], cfg)
    )
    if ours is None or base is None:
        return None
    return {
        "n": ours.n,
        "scorer_hit": ours.hit_at_k,
        "scorer_mrr": ours.mrr,
        "baseline_hit": base.hit_at_k,
        "baseline_mrr": base.mrr,
    }


class RunOutput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    out_dir: Path
    report: RankingReport
    results: list[RationaleResult]
    meta: dict
    statuses: dict[str, int]
    elapsed_s: float


def total_cost(results: list[RationaleResult]) -> float | None:
    """API spend for the accepted pages in USD; None if any written page's model has no price."""
    costs = [r.cost_usd for r in results if r.rationale is not None]
    if any(c is None for c in costs):
        return None
    return round(sum(costs), 4)


def execute(
    csv_path: Path,
    run: RunConfig,
    out_root: Path,
    rid: str,
    client: ModelClient,
    on_ranked: Callable[[RankingReport], None] | None = None,
    on_result: Callable[[RationaleResult], None] | None = None,
) -> RunOutput:
    started = time.monotonic()
    report = rank(csv_path, run)
    out_dir = out_root / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ranking.json").write_text(report.model_dump_json(indent=2))
    if on_ranked:
        on_ranked(report)

    tracer = Tracer(
        out_dir / "trace.jsonl",
        rid,
        {"gen_ai.system": "openai", "prompt.version": run.llm.prompt_version},
    )
    cache_dir = out_root / ".cache" / "rationales" if run.llm.use_cache else None
    results = write(report, run, client, tracer, cache_dir, on_result)
    (out_dir / "rationales.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in results], indent=2)
    )
    models = sorted({r.model for r in results if r.model}) or [run.llm.model]
    elapsed = time.monotonic() - started
    meta = {
        "run_id": rid,
        "models": ", ".join(models),
        "prompt_version": run.llm.prompt_version,
        "backtest": backtest_summary(csv_path, run.scoring),
    }
    (out_dir / "report.md").write_text(report_md.render(report, results, meta))
    (out_dir / "report.html").write_text(render_html(report, results, meta))
    statuses = {s: sum(r.status == s for r in results) for s in ("ok", "needs_review", "failed")}
    (out_dir / "run.json").write_text(
        json.dumps(
            {
                "command": "run",
                "run_id": rid,
                "csv": str(csv_path),
                "csv_sha256": sha256(csv_path),
                "config": run.model_dump(mode="json"),
                "resolved_models": models,
                "elapsed_s": round(elapsed, 1),
                "statuses": statuses,
                "cache_hits": sum(r.cache_hit for r in results),
                "input_tokens": sum(r.input_tokens for r in results),
                "output_tokens": sum(r.output_tokens for r in results),
                "cost_usd": total_cost(results),
            },
            indent=2,
        )
    )
    return RunOutput(
        out_dir=out_dir,
        report=report,
        results=results,
        meta=meta,
        statuses=statuses,
        elapsed_s=round(elapsed, 1),
    )
