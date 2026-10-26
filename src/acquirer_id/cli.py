"""Command-line entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from acquirer_id import backtest, runner
from acquirer_id.config import RunConfig, ScoringConfig, load_run_config, with_target_overrides
from acquirer_id.data import DataError, load_transactions
from acquirer_id.evidence import RankingReport
from acquirer_id.pipeline import rank
from acquirer_id.ranking import TargetOutOfRange
from acquirer_id.rationale import RationaleResult

DEFAULT_CSV = Path("data/ma_transactions_500.csv")
DEFAULT_CONFIG = Path("configs/default_target.yaml")
DEFAULT_CASES = Path("evals/golden/ranking_cases.json")
DEFAULT_RATIONALE_CASES = Path("evals/golden/rationale_cases.json")
# Golden set 2 extras: a sponsor just outside the list with withdrawn/terminated deals, and a
# small Dental target to show nothing healthcare-services-specific leaks into the prompt.
RATIONALE_EXTRA_ACQUIRERS = ["New Mountain Capital"]
RATIONALE_ALT_TARGET = {
    "name": "Small private dental group",
    "sector": "Dental",
    "deal_size_mm": 60,
    "ownership": "Private",
}
DEV_YEARS, HOLDOUT_YEARS = {2022}, {2023, 2024}

# Ablations of the scorer, each a set of ScoringConfig overrides on top of --config.
VARIANTS: dict[str, dict[str, object]] = {
    "scorer": {},  # the shipped default
    "scorer_v1": {"mix_share_basis": "comparables", "strategic_adjacency": True},
    "scorer_comparables_mix": {"mix_share_basis": "comparables"},
    "scorer_strategic_adjacency": {"strategic_adjacency": True},
    "scorer_no_floor": {"min_per_type": 0},
    "scorer_pure_score": {"mix_mode": "score"},
    "scorer_no_adjacency": {"adjacency_scale": 0.0},
    "scorer_no_shrinkage": {"shrinkage_strength": 0.0},
    "scorer_no_eligibility": {"min_relevant_deals": 0},  # old rule: zero-evidence buyers listed
    "scorer_lift_floor_1": {"adjacent_lift_threshold": 1.0},  # old rule: any lift above 1
}
REFERENCE = "baseline_most_active_in_sector"


_run_id = runner.run_id


TARGET_FLAGS = {  # flag -> (TargetProfile field, type, help)
    "name": ("name", str, "Target name shown in the report."),
    "sector": ("sector", str, "Sector, as spelled in the CSV (e.g. Dental)."),
    "size": ("deal_size_mm", float, "Enterprise value in USD millions."),
    "ownership": ("ownership", str, "Pre-deal ownership, as in the CSV (e.g. Private)."),
    "region": ("region", str, "Named region, as in the CSV (e.g. Southeast)."),
    "margin": ("ebitda_margin_pct", float, "Real EBITDA margin in percent (default: assumed)."),
}


def _add_target_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("target overrides (applied on top of --config)")
    for flag, (_, kind, help_text) in TARGET_FLAGS.items():
        g.add_argument(f"--{flag}", type=kind, help=help_text)
    g.add_argument("--national", action="store_true", help="Target is national, not regional.")


def _target_overrides(args: argparse.Namespace) -> dict:
    out = {
        field: getattr(args, flag)
        for flag, (field, _, _) in TARGET_FLAGS.items()
        if getattr(args, flag, None) is not None
    }
    if getattr(args, "national", False):
        out |= {"regional": False, "region": None}
    elif "region" in out:
        out["regional"] = True
    return out


def _load_run(args: argparse.Namespace) -> tuple[RunConfig, dict]:
    """The config file plus any target flags."""
    overrides = _target_overrides(args)
    return with_target_overrides(load_run_config(args.config), overrides), overrides


def _print_summary(report: RankingReport) -> None:
    t, m, v = report.target, report.mix, report.valuation
    margin_note = " (assumed: sector 75th pct)" if t.margin_is_assumed else ""
    print(
        f"Target: {t.profile.name} | {t.profile.sector} | ${t.profile.deal_size_mm:,.0f}M EV | "
        f"margin {t.target_margin_pct}%{margin_note}"
    )
    print(
        f"Adjacent sectors (lift): {', '.join(f'{s} {x}' for s, x in t.adjacent_sectors.items())}"
    )
    print(
        f"Comparables: {v.all_comps.n} closed deals ({v.comparable_set_basis}); "
        f"median EV/EBITDA {v.all_comps.median_ev_ebitda}x"
    )
    floor = " (floor applied)" if m.floor_applied else ""
    print(
        f"Mix: {m.sponsor_slots} sponsors / {m.strategic_slots} strategics{floor}; "
        f"strategic share {m.strategic_share_used:.0%} ({m.mix_share_basis} basis; "
        f"size-matched comparables {m.comparable_strategic_share:.0%}) -> "
        f"{m.strategic_slots_from_data} of {m.strategic_slots + m.sponsor_slots}"
    )
    print()
    print(
        f"{'#':>2}  {'Acquirer':28} {'Type':10} {'Score':>5}  {'n':>3} {'rel':>3}  cap     "
        "act  foc  size rec  close"
    )
    for p in report.packs:
        b = p.score_breakdown
        kind = "Strategic" if p.acquirer_type.value == "Strategic" else "Sponsor"
        print(
            f"{p.rank:>2}  {p.acquirer:28} {kind:10} {p.score:5.3f}  {p.profile.n_deals:>3} "
            f"{p.n_relevant_deals:>3}  {p.conviction_cap:6}  {b['relevant_activity']:.2f} "
            f"{b['focus']:.2f} {b['size_fit']:.2f} {b['recency']:.2f} {b['close_rate']:.2f}"
        )
    nxt = ", ".join(f"{k.value}: {n}" for k, n in m.next_by_type.items() if n)
    print(f"\nNext in line: {nxt}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _freeze_cases(args: argparse.Namespace) -> int:
    deals = load_transactions(args.csv)
    cases = backtest.freeze_cases(deals, DEV_YEARS, HOLDOUT_YEARS)
    args.cases.parent.mkdir(parents=True, exist_ok=True)
    args.cases.write_text(
        json.dumps(
            {
                "description": "Golden set 1: IDs only, rebuilt from the CSV at eval time.",
                "csv_sha256": _sha256(args.csv),
                "dev_years": sorted(DEV_YEARS),
                "holdout_years": sorted(HOLDOUT_YEARS),
                "cases": [c.model_dump() for c in cases],
            },
            indent=1,
        )
    )
    counts = {s: sum(c.split == s for c in cases) for s in ("dev", "holdout")}
    print(f"Froze {len(cases)} cases {counts} to {args.cases}")
    return 0


def _fmt(m: backtest.Metrics | None) -> str:
    return f"{m.hit_at_k:5.0%} {m.mrr:5.2f} {m.n:>3}" if m else f"{'-':>5} {'-':>5} {0:>3}"


def _backtest(args: argparse.Namespace) -> int:
    deals = load_transactions(args.csv)
    frozen = json.loads(args.cases.read_text())
    if frozen["csv_sha256"] != _sha256(args.csv):
        print(
            "warning: CSV differs from the one the golden cases were frozen against",
            file=sys.stderr,
        )
    cases = [c for c in backtest.load_cases(args.cases) if args.split in ("all", c.split)]
    base_cfg = load_run_config(args.config).scoring

    runs: dict[str, list[backtest.CaseResult]] = {}
    for name in ("baseline_most_active", REFERENCE):
        runs[name] = backtest.evaluate(deals, cases, backtest.RANKERS[name], base_cfg)
    for name in args.variants:
        cfg = ScoringConfig.model_validate(base_cfg.model_dump() | VARIANTS[name])
        runs[name] = backtest.evaluate(deals, cases, backtest.rank_scorer, cfg)

    out_dir = args.out / f"backtest-{_run_id(args.config, args.csv)}-{args.split}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    slices = list(backtest.SLICES)
    print(f"Backtest: split={args.split}, {len(cases)} cases, k={base_cfg.k}. Cells: hit@k  MRR  n")
    print(
        f"{'ranker':32}"
        + "".join(f"{s[:22]:>24}" for s in slices)
        + f"{'vs in-sector baseline':>26}"
    )
    for name, results in runs.items():
        s = backtest.summarize(results)
        cmp = backtest.compare(results, runs[REFERENCE])
        summary[name] = {
            "slices": {k: (v.model_dump() if v else None) for k, v in s.items()},
            "vs_reference": cmp.model_dump(),
        }
        with (out_dir / f"{name}.jsonl").open("w") as f:
            for r in results:
                f.write(r.model_dump_json() + "\n")
        print(
            f"{name:32}"
            + "".join(f"{_fmt(s[k]):>24}" for k in slices)
            + f"{f'+{cmp.wins} / -{cmp.losses}':>26}"
        )
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "split": args.split,
                "n_cases": len(cases),
                "reference": REFERENCE,
                "config": base_cfg.model_dump(mode="json"),
                "csv_sha256": _sha256(args.csv),
                "rankers": summary,
            },
            indent=2,
        )
    )
    print(f"\nPer-case results: {out_dir}/<ranker>.jsonl")
    return 0


def _print_result(r: RationaleResult) -> None:
    conv = r.rationale.conviction if r.rationale else "-"
    note = "; ".join(i.code for i in r.issues) or r.error or ""
    print(f"{r.rank:>2} {r.acquirer:28} {r.status:12} {conv:7} {note[:80]}", flush=True)


def _run(args: argparse.Namespace) -> int:
    from acquirer_id.llm import OpenAIClient

    run, overrides = _load_run(args)
    if args.model:
        run.llm.model = args.model
    if args.no_cache:
        run.llm.use_cache = False

    def ranked(report: RankingReport) -> None:
        print(
            f"Ranked {len(report.packs)} acquirers; writing rationales with {run.llm.model}. "
            "Pages as they finish:",
            flush=True,
        )

    out = runner.execute(
        args.csv,
        run,
        args.out,
        _run_id(args.config, args.csv, overrides),
        OpenAIClient(),
        on_ranked=ranked,
        on_result=_print_result,
    )
    cost = runner.total_cost(out.results)
    print(
        f"\n{out.statuses} in {out.elapsed_s:.1f}s, "
        f"{'cost n/a' if cost is None else f'~${cost:.3f}'}. Report: {out.out_dir}/report.md and "
        f"report.html (print to PDF). Trace: {out.out_dir}/trace.jsonl"
    )
    return 0 if out.statuses["failed"] == 0 else 1


def _freeze_rationale_cases(args: argparse.Namespace) -> int:
    from acquirer_id import rationale_eval
    from acquirer_id.config import TargetProfile

    deals = load_transactions(args.csv)
    run = load_run_config(args.config)
    cases = rationale_eval.freeze(
        deals,
        run.target,
        TargetProfile(**RATIONALE_ALT_TARGET),
        RATIONALE_EXTRA_ACQUIRERS,
        run.scoring,
    )
    args.cases.parent.mkdir(parents=True, exist_ok=True)
    args.cases.write_text(
        json.dumps(
            {
                "description": "Golden set 2: target-acquirer pairs, rebuilt from CSV.",
                "csv_sha256": _sha256(args.csv),
                "cases": [c.model_dump(mode="json") for c in cases],
            },
            indent=1,
        )
    )
    for c in cases:
        print(f"{c.case_id:60} {', '.join(c.tags)}")
    print(f"Froze {len(cases)} cases to {args.cases}")
    return 0


def _eval_rationales(args: argparse.Namespace) -> int:
    from acquirer_id import rationale_eval
    from acquirer_id.llm import OpenAIClient
    from acquirer_id.tracing import Tracer

    deals = load_transactions(args.csv)
    run = load_run_config(args.config)
    cases = rationale_eval.load_cases(args.cases)
    client = OpenAIClient()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rows = []
    for model in args.models:
        cfg = run.llm.model_copy(
            update={
                "model": model,
                "prompt_version": args.prompt_version,
                "max_workers": len(cases),
            }
        )
        out_dir = args.out / f"rationale-eval-{stamp}" / f"{args.prompt_version}-{model}"
        out_dir.mkdir(parents=True, exist_ok=True)
        tracer = Tracer(
            out_dir / "trace.jsonl",
            f"{stamp}-{model}",
            {
                "gen_ai.system": "openai",
                "prompt.version": args.prompt_version,
                "eval": "golden_set_2",
            },
        )
        scores = rationale_eval.run_eval(deals, cases, client, cfg, run.scoring, tracer)
        with (out_dir / "results.jsonl").open("w") as f:
            for s in scores:
                f.write(s.model_dump_json() + "\n")
        summary = rationale_eval.summarize(scores)
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        rows.append((model, summary))
        for s in scores:
            r = s.result
            codes = ", ".join(i.code for i in r.issues) or r.error or ""
            conv = r.rationale.conviction if r.rationale else "-"
            print(f"  {model:16} {s.case_id[:52]:52} {r.status:12} {conv:6} {codes[:60]}")
    print(f"\nprompt {args.prompt_version}, {len(cases)} cases")
    print(
        f"{'model':16} {'pass':>5} {'fail':>4} {'head sim mean/max':>18} {'same opening':>12} "
        f"{'p50 s':>6} {'max s':>6} {'tok in':>7} {'tok out':>7}  convictions / issues"
    )
    for model, s in rows:
        h = s["headline_similarity_default_list"]
        print(
            f"{model:16} {s['pass_rate']:5.0%} {s['failed']:>4} "
            f"{h['mean']:>9.2f}/{h['max']:<8.2f} {h['most_common_opening_share']:>12.0%} "
            f"{(s['latency_ms_p50'] or 0) / 1000:6.1f} {(s['latency_ms_max'] or 0) / 1000:6.1f} "
            f"{s['tokens_in']:>7} {s['tokens_out']:>7}  {s['convictions']} {s['issues']}"
        )
        if s["pack_drift"]:
            print(f"  warning: evidence packs changed since freeze: {s['pack_drift']}")
    print(f"\nResults and traces: {args.out}/rationale-eval-{stamp}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acquirer-id", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_rank = sub.add_parser("rank", help="Rank acquirers and write evidence packs (no LLM).")
    p_rank.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p_rank.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_rank.add_argument("--out", type=Path, default=Path("output"))
    _add_target_flags(p_rank)

    p_run = sub.add_parser("run", help="Rank, write the 10 rationales with the LLM, render report.")
    p_run.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p_run.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_run.add_argument("--out", type=Path, default=Path("output"))
    p_run.add_argument("--model", help="Override llm.model from the config.")
    p_run.add_argument("--no-cache", action="store_true", help="Regenerate even if cached.")
    _add_target_flags(p_run)

    p_frz2 = sub.add_parser("freeze-rationale-cases", help="Write golden set 2 (run once).")
    p_frz2.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p_frz2.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_frz2.add_argument("--cases", type=Path, default=DEFAULT_RATIONALE_CASES)

    p_ev2 = sub.add_parser("eval-rationales", help="Score rationales on golden set 2.")
    p_ev2.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p_ev2.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_ev2.add_argument("--cases", type=Path, default=DEFAULT_RATIONALE_CASES)
    p_ev2.add_argument("--models", nargs="+", default=["gpt-6.1-sol"])
    p_ev2.add_argument("--prompt-version", default="rationale_v1")
    p_ev2.add_argument("--out", type=Path, default=Path("output"))

    p_freeze = sub.add_parser("freeze-cases", help="Write the golden ranking cases (run once).")
    p_freeze.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p_freeze.add_argument("--cases", type=Path, default=DEFAULT_CASES)

    p_bt = sub.add_parser("backtest", help="Replay historical deals against the rankers.")
    p_bt.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p_bt.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p_bt.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    p_bt.add_argument(
        "--split",
        choices=["dev", "holdout", "all"],
        default="dev",
        help="Tune on dev. Run holdout once, at the end.",
    )
    p_bt.add_argument("--variants", nargs="+", choices=list(VARIANTS), default=list(VARIANTS))
    p_bt.add_argument("--out", type=Path, default=Path("output"))
    args = parser.parse_args(argv)

    try:
        if args.command == "freeze-cases":
            return _freeze_cases(args)
        if args.command == "backtest":
            return _backtest(args)
        if args.command == "run":
            return _run(args)
        if args.command == "freeze-rationale-cases":
            return _freeze_rationale_cases(args)
        if args.command == "eval-rationales":
            return _eval_rationales(args)
        run, overrides = _load_run(args)
        report = rank(args.csv, run)
    except (DataError, TargetOutOfRange, ValidationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out_dir = args.out / _run_id(args.config, args.csv, overrides)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ranking.json").write_text(report.model_dump_json(indent=2))
    (out_dir / "run.json").write_text(
        json.dumps(
            {
                "command": "rank",
                "csv": str(args.csv),
                "csv_sha256": hashlib.sha256(args.csv.read_bytes()).hexdigest(),
                "config": run.model_dump(mode="json"),
            },
            indent=2,
        )
    )
    _print_summary(report)
    print(f"Wrote {out_dir}/ranking.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
