import json

import pytest

from acquirer_id.config import LLMConfig
from acquirer_id.evidence import build_report
from acquirer_id.llm import LLMRequest, LLMResponse
from acquirer_id.ranking import build_context, score_acquirers, select
from acquirer_id.rationale import (
    PrecedentActivity,
    PrecedentNote,
    Rationale,
    RiskFlag,
    Section,
    build_input,
    check,
    conviction_rubric,
    load_prompt,
    write_all,
    write_one,
)
from acquirer_id.report import render
from acquirer_id.tracing import Tracer


@pytest.fixture
def report(deals, target, cfg):
    ctx = build_context(deals, target, cfg)
    return build_report(
        deals, target, ctx, select(score_acquirers(deals, target, ctx, cfg), ctx, cfg), cfg
    )


def grounded_rationale(pack, conviction=None, **overrides) -> Rationale:
    p = pack.precedents[0]
    if conviction is None:
        conviction = conviction_rubric(pack.model_dump(mode="json"), pack.conviction_cap)
    fields = dict(
        headline=f"Precedent {p.transaction_id} at ${p.deal_size_mm}M is the closest match.",
        acquirer_overview=Section(
            text=f"Median deal size ${pack.profile.deal_size_median_mm}M.", cited_ids=[]
        ),
        strategic_fit_thesis=Section(
            text="Same sector as the target.", cited_ids=[p.transaction_id]
        ),
        precedent_activity=PrecedentActivity(
            precedents=[
                PrecedentNote(
                    transaction_id=p.transaction_id, takeaway=f"Closed at ${p.deal_size_mm}M."
                )
            ],
            pattern="One relevant precedent.",
        ),
        valuation_context=Section(text="Thin valuation evidence here.", cited_ids=[]),
        risk_flags=[
            RiskFlag(
                category="thin_evidence", title="Few deals", detail="Small sample.", cited_ids=[]
            ),
            RiskFlag(
                category="execution", title="Withdrawals", detail="Some withdrawn.", cited_ids=[]
            ),
        ],
        conviction=conviction,
        conviction_rationale="Limited relevant deals.",
        data_gaps=["Confirm current status."],
    )
    return Rationale(**(fields | overrides))


class FakeClient:
    """Answers with a grounded rationale; can be told to fail for specific models."""

    def __init__(self, report, fail_models=(), conviction=None):
        self.packs = {p.acquirer: p for p in report.packs}
        self.fail_models = set(fail_models)
        self.conviction = conviction
        self.calls: list[str] = []

    def generate(self, request: LLMRequest, schema) -> LLMResponse:
        self.calls.append(request.model)
        if request.model in self.fail_models:
            raise TimeoutError("simulated timeout")
        acquirer = json.loads(request.user)["acquirer"]["acquirer"]
        r = grounded_rationale(self.packs[acquirer], self.conviction)
        return LLMResponse(
            parsed_json=r.model_dump_json(),
            resolved_model=request.model,
            input_tokens=100,
            output_tokens=50,
            latency_ms=5,
        )


def _issue_codes(issues):
    return [i.code for i in issues]


def test_grounded_rationale_passes(report):
    pack = report.packs[0]
    assert check(grounded_rationale(pack), build_input(report, pack), pack.conviction_cap) == []


def test_invented_number_and_id_are_flagged(report):
    pack = report.packs[0]
    bad = grounded_rationale(
        pack,
        headline="Paid 23.7x on a $512.4M deal.",
        strategic_fit_thesis=Section(text="Fits.", cited_ids=["MA-1999-9999"]),
    )
    codes = _issue_codes(check(bad, build_input(report, pack), pack.conviction_cap))
    assert codes.count("ungrounded_number") == 2
    assert "unknown_id" in codes


def test_percent_of_share_and_small_counts_are_grounded(report):
    pack = report.packs[0]
    share = report.mix.strategic_share_used  # e.g. 0.75 may be written as 75%
    ok = grounded_rationale(pack, headline=f"{share * 100:.0f}% strategic; 3 of 4 deals closed.")
    assert check(ok, build_input(report, pack), pack.conviction_cap) == []


def test_banned_phrase_and_missing_risks_are_flagged(report):
    pack = report.packs[0]
    bad = grounded_rationale(pack, headline="A leading buyer.", risk_flags=[])
    codes = _issue_codes(check(bad, build_input(report, pack), pack.conviction_cap))
    assert {"banned_phrase", "too_few_risks"} <= set(codes)


def test_conviction_above_cap_is_flagged_and_clamped(report, tmp_path):
    pack = next(p for p in report.packs if p.conviction_cap == "Medium")
    client = FakeClient(report, conviction="High")
    tracer = Tracer(tmp_path / "t.jsonl", "run")
    r = write_one(report, pack, client, LLMConfig(), load_prompt("rationale_v1"), tracer, None)
    assert r.status == "needs_review"
    assert "conviction_above_cap" in _issue_codes(r.issues)
    assert r.rationale.conviction == "Medium"


def test_falls_back_after_bounded_retries(report, tmp_path):
    cfg = LLMConfig(model="primary", fallback_model="backup", max_attempts=2)
    client = FakeClient(report, fail_models={"primary"})
    tracer = Tracer(tmp_path / "t.jsonl", "run")
    r = write_one(report, report.packs[0], client, cfg, "sys", tracer, None)
    assert client.calls == ["primary", "primary", "backup"]
    assert (r.status, r.model, r.attempts) == ("ok", "backup", 3)
    spans = [json.loads(line) for line in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert [s["status"] for s in spans] == ["error", "error", "ok"]
    assert spans[0]["error"].startswith("TimeoutError")


def test_total_failure_degrades_instead_of_crashing(report, tmp_path):
    cfg = LLMConfig(model="a", fallback_model="b", max_attempts=1)
    client = FakeClient(report, fail_models={"a", "b"})
    results = write_all(report, client, cfg, Tracer(tmp_path / "t.jsonl", "run"), None)
    assert all(r.status == "failed" and r.rationale is None for r in results)
    md = render(report, results, {"run_id": "r", "models": "a", "prompt_version": "v"})
    assert md.count("Rationale unavailable") == len(report.packs)
    for p in report.packs[0].precedents:
        assert p.transaction_id in md  # evidence still shown


def test_cache_reuses_identical_requests(report, tmp_path):
    cfg = LLMConfig(model="m", fallback_model="f")
    client = FakeClient(report)
    tracer = Tracer(tmp_path / "t.jsonl", "run")
    first = write_all(report, client, cfg, tracer, tmp_path / "cache")
    n_calls = len(client.calls)
    second = write_all(report, client, cfg, tracer, tmp_path / "cache")
    assert len(client.calls) == n_calls  # no new calls
    assert all(r.cache_hit for r in second)
    assert [r.rationale for r in first] == [r.rationale for r in second]


def test_cost_from_price_table_and_zero_on_cache_hit(report, tmp_path):
    from acquirer_id.runner import total_cost

    prices = {"m": {"input": 2.0, "output": 10.0}}
    cfg = LLMConfig(model="m", fallback_model="f", prices_per_mtok=prices)
    tracer = Tracer(tmp_path / "t.jsonl", "run")
    first = write_all(report, FakeClient(report), cfg, tracer, tmp_path / "cache")
    assert all(r.cost_usd == (100 * 2.0 + 50 * 10.0) / 1e6 for r in first)
    assert total_cost(first) == round(len(first) * 0.0007, 4)
    second = write_all(report, FakeClient(report), cfg, tracer, tmp_path / "cache")
    assert total_cost(second) == 0.0
    unpriced = write_all(report, FakeClient(report), LLMConfig(model="m"), tracer, None)
    assert total_cost(unpriced) is None  # unknown price is reported as unknown, not as $0


def test_trace_records_otel_style_fields(report, tmp_path):
    tracer = Tracer(tmp_path / "t.jsonl", "run-1", {"prompt.version": "rationale_v1"})
    write_one(report, report.packs[0], FakeClient(report), LLMConfig(), "sys", tracer, None)
    span = json.loads((tmp_path / "t.jsonl").read_text())
    for key in (
        "trace_id",
        "span_id",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "latency_ms",
        "prompt.version",
        "input",
        "output",
        "issues",
    ):
        assert key in span
    assert span["trace_id"] == "run-1"


def test_prompt_version_must_match_file(tmp_path, monkeypatch):
    import acquirer_id.rationale as rationale

    (tmp_path / "x.md").write_text("---\nversion: y\n---\nbody")
    monkeypatch.setattr(rationale, "PROMPTS_DIR", tmp_path)
    with pytest.raises(ValueError, match="declares version y"):
        load_prompt("x")


def test_report_has_summary_and_every_section(report, tmp_path):
    results = write_all(
        report, FakeClient(report), LLMConfig(), Tracer(tmp_path / "t.jsonl", "r"), None
    )
    md = render(report, results, {"run_id": "r", "models": "m", "prompt_version": "v"})
    assert "## Summary" in md
    assert "| # | Acquirer | Conviction |" in md
    assert "## Appendix: how the list is built" in md
    assert md.count("dataset ends in") == 1  # shared caveat once, not per page
    assert "⚠ =" not in md  # legend only when a page is flagged
    for heading in (
        "Acquirer overview.",
        "Strategic fit.",
        "Precedent activity.",
        "Valuation context.",
        "Risk flags.",
        "Conviction:",
    ):
        assert md.count(heading) == len(report.packs)


def test_headline_opening_with_the_name_is_flagged(report):
    pack = report.packs[0]
    bad = grounded_rationale(pack, headline=f"{pack.acquirer} is a financial sponsor.")
    codes = _issue_codes(check(bad, build_input(report, pack), pack.conviction_cap))
    assert "templated_headline" in codes


def test_conviction_rubric_levels():
    def pack(n_rel, recent, size_fit):
        return {
            "n_relevant_deals": n_rel,
            "profile": {"deals_last_3_years": recent},
            "score_breakdown": {"size_fit": size_fit},
        }

    assert conviction_rubric(pack(1, 2, 0.9), "High") == "Low"  # one similar deal
    assert conviction_rubric(pack(3, 0, 0.9), "High") == "Low"  # nothing recent
    assert conviction_rubric(pack(3, 2, 0.4), "High") == "Low"  # size far from its history
    assert conviction_rubric(pack(3, 2, 0.8), "High") == "High"
    assert conviction_rubric(pack(3, 2, 0.8), "Medium") == "Medium"  # cap wins
    assert conviction_rubric(pack(2, 2, 0.9), "High") == "Medium"


def test_conviction_off_rubric_is_flagged(report):
    pack = report.packs[0]
    expected = conviction_rubric(pack.model_dump(mode="json"), pack.conviction_cap)
    other = "Low" if expected != "Low" else "Medium"
    codes = _issue_codes(
        check(grounded_rationale(pack, conviction=other), build_input(report, pack), "High")
    )
    assert "conviction_off_rubric" in codes


def test_v3_style_rules_are_flagged(report):
    pack = report.packs[0]
    bad = grounded_rationale(
        pack,
        headline="Its 0.95 size-fit score makes it the closest match.",
        valuation_context=Section(text="Median 1.0 bidders across 3 relevant deals.", cited_ids=[]),
        strategic_fit_thesis=Section(text="Supports a geographic expansion thesis.", cited_ids=[]),
        conviction_rationale="word " * 400,
    )
    codes = set(_issue_codes(check(bad, build_input(report, pack), pack.conviction_cap)))
    assert {"score_jargon", "thin_median", "page_too_long", "geography_without_region"} <= codes


def test_median_over_five_deals_is_fine(report):
    pack = report.packs[0]
    ok = grounded_rationale(
        pack, valuation_context=Section(text="Market median across 24 comps.", cited_ids=[])
    )
    codes = _issue_codes(check(ok, build_input(report, pack), pack.conviction_cap))
    assert "thin_median" not in codes


def test_placeholder_section_is_flagged(report):
    pack = report.packs[0]
    bad = grounded_rationale(pack)
    bad.precedent_activity.pattern = "REMOVE"
    codes = _issue_codes(check(bad, build_input(report, pack), pack.conviction_cap))
    assert "placeholder_text" in codes


def test_status_note_is_rendered_but_never_sent_to_the_writer(report, tmp_path):
    name = report.packs[0].acquirer
    report.entity_notes = {name: "Filed for bankruptcy."}
    report.entity_notes_source = "test source, as of 2026"
    results = write_all(
        report, FakeClient(report), LLMConfig(), Tracer(tmp_path / "t.jsonl", "r"), None
    )
    md = render(report, results, {"run_id": "r", "models": "m", "prompt_version": "v"})
    assert f"{name} †" in md and "Filed for bankruptcy." in md
    assert "bankruptcy" not in json.dumps(build_input(report, report.packs[0]))


def test_gives_a_precedent_headline_is_flagged(report):
    pack = report.packs[0]
    bad = grounded_rationale(pack, headline="The closed deal gives it a direct sector precedent.")
    codes = _issue_codes(check(bad, build_input(report, pack), pack.conviction_cap))
    assert "templated_headline" in codes


def test_html_report_has_cover_one_page_per_buyer_and_appendix(report, tmp_path):
    from acquirer_id.report_html import render_html

    results = write_all(
        report, FakeClient(report), LLMConfig(), Tracer(tmp_path / "t.jsonl", "r"), None
    )
    html = render_html(report, results, {"run_id": "r", "models": "m", "prompt_version": "v"})
    assert html.count('<section class="page">') == len(report.packs) + 2
    for pack in report.packs:
        assert f"<h2>{pack.acquirer}</h2>" in html
    assert "Appendix: how the list is built" in html


def test_html_report_escapes_untrusted_text(report, tmp_path):
    from acquirer_id.report_html import render_html

    results = write_all(
        report, FakeClient(report), LLMConfig(), Tracer(tmp_path / "t.jsonl", "r"), None
    )
    results[0].rationale.headline = "<script>alert(1)</script>"
    html = render_html(report, results, {"run_id": "r", "models": "m", "prompt_version": "v"})
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_progress_callback_fires_once_per_page_in_the_caller_thread(report, tmp_path):
    import threading

    seen = []
    caller = threading.get_ident()
    write_all(
        report,
        FakeClient(report),
        LLMConfig(),
        Tracer(tmp_path / "t.jsonl", "r"),
        None,
        on_result=lambda r: seen.append((r.acquirer, threading.get_ident())),
    )
    assert sorted(a for a, _ in seen) == sorted(p.acquirer for p in report.packs)
    assert all(t == caller for _, t in seen)


def test_backtest_is_in_the_appendix_not_on_the_cover(report, tmp_path):
    from acquirer_id.report_html import render_html

    results = write_all(
        report, FakeClient(report), LLMConfig(), Tracer(tmp_path / "t.jsonl", "r"), None
    )
    bt = {
        "n": 142,
        "scorer_hit": 0.39,
        "scorer_mrr": 0.12,
        "baseline_hit": 0.4,
        "baseline_mrr": 0.11,
    }
    meta = {"run_id": "r", "models": "m", "prompt_version": "v", "backtest": bt}
    for doc, appendix in (
        (render(report, results, meta), "## Appendix"),
        (render_html(report, results, meta), "Appendix: how the list is built"),
    ):
        assert doc.count("top 10 in 39% of cases") == 1
        assert doc.index("top 10 in 39% of cases") > doc.index(appendix)
        assert "MRR" not in doc
