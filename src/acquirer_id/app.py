"""Streamlit front end: the same runner as the CLI, with a target form and live progress.

Run with `make app`. No business logic here: the form builds the same target
overrides as the CLI flags, `runner.execute` does the work, and the report shown is the same
print-ready HTML the CLI writes.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from acquirer_id import runner
from acquirer_id.config import load_run_config, with_target_overrides
from acquirer_id.data import AcquirerType, DataError, load_transactions
from acquirer_id.evidence import RankingReport
from acquirer_id.pipeline import rank
from acquirer_id.ranking import NATIONAL_GEOGRAPHIES, TargetOutOfRange
from acquirer_id.rationale import RationaleResult
from acquirer_id.report_html import render_html

CSV = Path(os.environ.get("ACQUIRER_ID_CSV", "data/ma_transactions_500.csv"))
CONFIG = Path(os.environ.get("ACQUIRER_ID_CONFIG", "configs/default_target.yaml"))
OUT = Path("output")
NOT_SET = "Not specified"

st.set_page_config(page_title="Acquirer identification", page_icon="📇", layout="wide")


@st.cache_data
def _choices(csv_path: str) -> dict[str, list[str]]:
    deals = load_transactions(Path(csv_path))
    return {
        "sectors": sorted({d.sector for d in deals}),
        "regions": sorted({d.geography for d in deals} - NATIONAL_GEOGRAPHIES),
        "ownerships": sorted({d.target_ownership_pre for d in deals}),
    }


def _index(options: list[str], value: str | None) -> int:
    return options.index(value) if value in options else 0


def _ranking_rows(report: RankingReport) -> list[dict]:
    return [
        {
            "#": p.rank,
            "Acquirer": p.acquirer,
            "Type": "Strategic" if p.acquirer_type == AcquirerType.STRATEGIC else "Sponsor",
            "Similar deals": p.n_relevant_deals,
            "Deals in dataset": p.profile.n_deals,
            "Median deal ($M)": p.profile.deal_size_median_mm,
            "Score": p.score,
        }
        for p in report.packs
    ]


def _progress_line(r: RationaleResult) -> str:
    if not r.rationale:
        return f"✗ **{r.acquirer}**: rationale unavailable ({r.error or 'model error'})"
    mark = "✓" if r.status == "ok" else "⚠"
    return f"{mark} **{r.acquirer}** · {r.rationale.conviction} · {r.rationale.headline}"


def _show(out: runner.RunOutput) -> None:
    report, mix, bt = out.report, out.report.mix, out.meta.get("backtest")
    ok = out.statuses["ok"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Runtime", f"{out.elapsed_s:.1f}s", help="Brief's budget: under 60s")
    c2.metric("Pages passing checks", f"{ok} of {len(out.results)}")
    c3.metric("Mix", f"{mix.sponsor_slots} sponsors / {mix.strategic_slots} strategics")
    if bt:
        c4.metric(
            "Backtest hit@10",
            f"{bt['scorer_hit']:.0%}",
            f"{bt['scorer_hit'] - bt['baseline_hit']:+.0%} vs in-sector baseline",
            delta_color="off",
        )
    html = render_html(report, out.results, out.meta)
    d1, d2, d3, _ = st.columns([1, 1, 1, 3])
    d1.download_button("Report (HTML → print to PDF)", html, "report.html", "text/html")
    d2.download_button(
        "Report (Markdown)", (out.out_dir / "report.md").read_text(), "report.md", "text/markdown"
    )
    d3.download_button(
        "Evidence (JSON)", report.model_dump_json(indent=2), "ranking.json", "application/json"
    )
    st.caption(
        f"Run {out.meta['run_id']} · writer {out.meta['models']} · prompt "
        f"{out.meta['prompt_version']} · artifacts and trace in `{out.out_dir}`"
    )
    # Safe to embed: render_html escapes every CSV and model string (tested), so the iframe only
    # ever contains our own template, never markup from the data or the LLM.
    st.iframe(html, height=1150)


# ---------- page ----------

st.title("Acquirer identification")
st.caption(
    "Ranks likely buyers from historical deals in code, then writes a one-page, data-backed "
    "rationale per buyer with an LLM. Every figure is checked against the dataset."
)

if not CSV.exists():
    st.error(f"Dataset not found at `{CSV}`. Copy the CSV there (see README), then reload.")
    st.stop()

base = load_run_config(CONFIG)
choices = _choices(str(CSV))


def _api_key() -> str | None:
    """OPENAI_API_KEY from the environment, else from .streamlit/secrets.toml (gitignored)."""
    if key := os.environ.get("OPENAI_API_KEY"):
        return key
    try:
        return st.secrets.get("OPENAI_API_KEY") or None
    except Exception:  # no secrets file, or an unreadable one: treat as no key
        return None


api_key = _api_key()
has_key = bool(api_key)

with st.sidebar.form("target"):
    st.subheader("Target")
    sector = st.selectbox(
        "Sector", choices["sectors"], _index(choices["sectors"], base.target.sector)
    )
    size = st.number_input(
        "Enterprise value ($M)", min_value=1.0, value=float(base.target.deal_size_mm), step=10.0
    )
    ownership = st.selectbox(
        "Ownership", choices["ownerships"], _index(choices["ownerships"], base.target.ownership)
    )
    region = st.selectbox(
        "Region", [NOT_SET, *choices["regions"]], help="Leave unset if unknown (brief: 'regional')"
    )
    national = st.checkbox("National, not regional", value=not base.target.regional)
    margin_known = st.checkbox("EBITDA margin known", help="Otherwise: sector 75th percentile")
    margin = st.number_input("EBITDA margin (%)", 0.0, 80.0, 18.0, 0.5)
    submitted = st.form_submit_button(
        "Find acquirers" if has_key else "Rank (no API key)", type="primary"
    )
if not has_key:
    st.sidebar.info(
        "No `OPENAI_API_KEY`: the app ranks buyers but cannot write the one-pagers. Export it "
        'before `make app`, or put `OPENAI_API_KEY = "sk-..."` in `.streamlit/secrets.toml`.'
    )

if submitted:
    overrides: dict = {}
    if sector != base.target.sector:
        overrides["sector"] = sector
    if size != base.target.deal_size_mm:
        overrides["deal_size_mm"] = size
    if ownership != base.target.ownership:
        overrides["ownership"] = ownership
    if national:
        overrides |= {"regional": False, "region": None}
    elif region != NOT_SET:
        overrides |= {"regional": True, "region": region}
    if margin_known:
        overrides["ebitda_margin_pct"] = margin
    run = with_target_overrides(base, overrides)
    st.session_state.pop("out", None)

    try:
        if not has_key:
            report = rank(CSV, run)
            st.subheader(run.target.name)
            st.dataframe(_ranking_rows(report), hide_index=True, width="stretch")
            st.info("Set `OPENAI_API_KEY` and restart to write the one-page rationales.")
            st.stop()

        from acquirer_id.llm import OpenAIClient

        st.subheader(run.target.name)
        table = st.empty()
        status = st.status("Ranking acquirers…", expanded=True)
        bar = st.progress(0.0)
        done: list[RationaleResult] = []
        total = {"pages": 1}

        def on_ranked(report: RankingReport) -> None:
            total["pages"] = max(len(report.packs), 1)
            table.dataframe(_ranking_rows(report), hide_index=True, width="stretch")
            status.update(label=f"Writing {len(report.packs)} one-pagers in parallel…")

        def on_result(r: RationaleResult) -> None:
            done.append(r)
            status.write(_progress_line(r))
            bar.progress(len(done) / total["pages"])

        out = runner.execute(
            CSV,
            run,
            OUT,
            runner.run_id(CONFIG, CSV, overrides),
            OpenAIClient(api_key),
            on_ranked=on_ranked,
            on_result=on_result,
        )
        bar.empty()
        table.empty()
        state = "complete" if out.statuses["failed"] == 0 else "error"
        status.update(
            label=f"Done in {out.elapsed_s:.1f}s · {out.statuses['ok']} of {len(out.results)} "
            "pages passed every check",
            state=state,
            expanded=False,
        )
        st.session_state["out"] = out
    except (DataError, TargetOutOfRange) as e:
        st.error(str(e))
        st.stop()

if "out" in st.session_state:
    if not submitted:
        st.subheader(st.session_state["out"].report.target.profile.name)
    _show(st.session_state["out"])
elif not submitted:
    st.info(
        "Set the target in the sidebar and press **Find acquirers**. "
        "The defaults are the brief's target."
    )
