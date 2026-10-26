"""Stage orchestration. Each stage takes and returns typed values; no I/O beyond loading."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from acquirer_id.config import RunConfig, load_entity_notes
from acquirer_id.data import apply_aliases, load_transactions
from acquirer_id.evidence import RankingReport, build_report
from acquirer_id.llm import ModelClient
from acquirer_id.ranking import build_context, score_acquirers, select
from acquirer_id.rationale import RationaleResult, write_all
from acquirer_id.tracing import Tracer


def rank(csv_path: Path, run: RunConfig) -> RankingReport:
    notes = load_entity_notes(run.entity_notes)
    deals = apply_aliases(load_transactions(csv_path), notes.aliases)
    ctx = build_context(deals, run.target, run.scoring)
    scores = score_acquirers(deals, run.target, ctx, run.scoring)
    selection = select(scores, ctx, run.scoring)
    report = build_report(deals, run.target, ctx, selection, run.scoring)
    listed = {p.acquirer for p in report.packs}
    report.entity_notes = {a: n for a, n in notes.status.items() if a in listed}
    report.aliases_applied = {
        old: new for old, new in notes.aliases.items() if any(d.acquirer == new for d in deals)
    }
    report.entity_notes_source = f"{notes.source}, as of {notes.as_of}" if notes.as_of else ""
    return report


def write(
    report: RankingReport,
    run: RunConfig,
    client: ModelClient,
    tracer: Tracer,
    cache_dir: Path | None,
    on_result: Callable[[RationaleResult], None] | None = None,
) -> list[RationaleResult]:
    return write_all(report, client, run.llm, tracer, cache_dir, on_result)
