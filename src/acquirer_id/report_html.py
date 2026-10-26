"""Render the report as one self-contained, print-ready HTML file.

Same content as the Markdown report: a cover page, one A4 page per buyer, an appendix. Print to
PDF from any browser. Formatting only, no business logic. All CSV and model text is
escaped, since it is untrusted. `buyer_card` and `CSS` are reused by the app.
"""

from __future__ import annotations

from html import escape

from acquirer_id.data import AcquirerType
from acquirer_id.evidence import EvidencePack, MultipleStats, RankingReport
from acquirer_id.rationale import RationaleResult
from acquirer_id.report import (
    FEATURE_DEFINITIONS,
    FEATURE_LABELS,
    RISK_LABELS,
    SHARED_CHECKS,
    _kind,
    _multiples,
    _shortfall,
    backtest_sentence,
)

CSS = """
:root {
  --ink: #1b2430; --muted: #5b6676; --line: #d9dee5; --panel: #f4f6f9; --accent: #1f4e79;
  --high: #1e7b4a; --medium: #a86b00; --low: #9a3b3b; --page-bg: #ffffff;
}
* { box-sizing: border-box; }
body { margin: 0; background: #e9edf2; color: var(--ink);
  font: 9.6pt/1.42 "Inter", "Segoe UI", system-ui, -apple-system, sans-serif; }
.page { background: var(--page-bg); width: 210mm; min-height: 297mm; margin: 12px auto;
  padding: 14mm 15mm 12mm; box-shadow: 0 1px 4px rgba(0,0,0,.12); position: relative; }
@media print {
  @page { size: A4; margin: 0; }
  body { background: none; }
  .page { margin: 0; box-shadow: none; page-break-after: always; break-after: page; }
  .page:last-child { page-break-after: auto; break-after: auto; }
}
@media screen and (max-width: 820px) {
  .page { width: auto; min-height: 0; margin: 8px; padding: 16px; } }
h1 { font-size: 19pt; margin: 0 0 2px; letter-spacing: -.01em; }
h2 { font-size: 15pt; margin: 0; letter-spacing: -.01em; }
h3 { font-size: 8.4pt; text-transform: uppercase; letter-spacing: .06em; color: var(--accent);
  margin: 12px 0 4px; }
p { margin: 0 0 6px; }
.sub { color: var(--muted); margin-bottom: 10px; }
.note { border-left: 3px solid var(--line); padding: 2px 8px; color: var(--muted); margin: 8px 0; }
.status { border-left-color: var(--low); color: var(--ink); background: #fbf1f1; }
.topline { display: flex; justify-content: space-between; align-items: center; color: var(--muted);
  font-size: 8.4pt; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
.pill { display: inline-block; border: 1px solid var(--line); border-radius: 999px;
  padding: 1px 8px; font-size: 8pt; color: var(--muted); }
.badge { display: inline-block; border-radius: 4px; padding: 2px 9px; font-weight: 700;
  font-size: 8.4pt; color: #fff; letter-spacing: .04em; }
.badge.High { background: var(--high); } .badge.Medium { background: var(--medium); }
.badge.Low { background: var(--low); } .badge.na { background: var(--muted); }
.title { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.headline { font-size: 11.5pt; font-weight: 600; margin: 6px 0 10px; line-height: 1.35; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin: 0 0 4px; }
.kpi { background: var(--panel); border-radius: 6px; padding: 6px 8px; }
.kpi b { display: block; font-size: 12pt; }
.kpi span { color: var(--muted); font-size: 7.8pt; }
.cols { display: grid; grid-template-columns: 1.35fr 1fr; gap: 14px; }
table { border-collapse: collapse; width: 100%; font-size: 8.4pt; }
th { text-align: left; color: var(--muted); font-weight: 600; border-bottom: 1px solid var(--ink);
  padding: 3px 5px; }
td { border-bottom: 1px solid var(--line); padding: 3px 5px; vertical-align: top; }
td.num, th.num { text-align: right; white-space: nowrap; }
code { font: 7.8pt ui-monospace, Menlo, Consolas, monospace; color: var(--muted); }
ul.risks { list-style: none; padding: 0; margin: 0; }
ul.risks li { background: var(--panel); border-radius: 6px; padding: 5px 8px; margin-bottom: 5px; }
ul.risks em { color: var(--muted); font-style: normal; font-size: 7.8pt; text-transform: uppercase;
  letter-spacing: .05em; display: block; }
.box { background: var(--panel); border-radius: 6px; padding: 6px 8px; }
.cites { color: var(--muted); font-size: 7.6pt; }
.foot { position: absolute; bottom: 7mm; left: 15mm; right: 15mm; display: flex;
  justify-content: space-between; color: var(--muted); font-size: 7.4pt; }
@media screen and (max-width: 820px) { .foot { position: static; margin-top: 12px; }
  .cols, .kpis { grid-template-columns: 1fr 1fr; } }
"""


def _e(x: object) -> str:
    return escape(str(x))


def _money(x: float) -> str:
    return f"${x:,.1f}M"


def _cites(ids: list[str]) -> str:
    if not ids:
        return ""
    shown = ", ".join(ids) if len(ids) <= 6 else f"{len(ids)} deals; IDs in rationales.json"
    return f' <span class="cites">[{_e(shown)}]</span>'


def _badge(conviction: str | None) -> str:
    level = conviction or "n/a"
    cls = level if level in ("High", "Medium", "Low") else "na"
    return f'<span class="badge {cls}">{_e(level.upper())}</span>'


def _own_multiples(m: MultipleStats) -> str:
    return _e(_multiples(m)) if m.n else "No closed similar deals with multiples."


def buyer_card(
    pack: EvidencePack,
    r: RationaleResult | None,
    total: int,
    note: str | None = None,
    note_source: str = "",
) -> str:
    """One buyer's page body (no page wrapper), so the app can embed the same card."""
    x = r.rationale if r else None
    prof = pack.profile
    precedents = {p.transaction_id: p for p in pack.precedents}
    out = [
        '<div class="topline">'
        f"<span>Buyer {pack.rank} of {total}</span>"
        f'<span class="pill">{_e(_kind(pack.acquirer_type))}</span></div>',
        f'<div class="title"><h2>{_e(pack.acquirer)}</h2>'
        f"{_badge(x.conviction if x else None)}</div>",
    ]
    if note:
        out.append(
            f'<p class="note status"><b>Status (outside the dataset; {_e(note_source)}):</b> '
            f"{_e(note)}</p>"
        )
    if r and r.issues:
        detail = "; ".join(f"{i.code} ({i.detail})" for i in r.issues)
        out.append(f'<p class="note status"><b>Review note:</b> {_e(detail)}</p>')
    if x:
        out.append(f'<p class="headline">{_e(x.headline)}</p>')
    out.append(
        '<div class="kpis">'
        f'<div class="kpi"><b>{prof.n_deals}</b><span>deals in dataset, '
        f"{prof.first_year}-{prof.last_year}</span></div>"
        f'<div class="kpi"><b>{pack.n_relevant_deals}</b><span>similar to this target</span></div>'
        f'<div class="kpi"><b>{_money(prof.deal_size_median_mm)}</b>'
        "<span>median deal size</span></div>"
        f'<div class="kpi"><b>{prof.deals_last_3_years}</b>'
        "<span>deals in the last 3 years</span></div>"
        "</div>"
    )
    if not x:
        reason = f": {r.error}" if r and r.error else ""
        out.append(
            f'<p class="note status">Rationale unavailable (model error{_e(reason)}). '
            "Evidence only.</p>"
        )
    else:
        out.append('<div class="cols"><div>')
        out.append(
            "<h3>Acquirer overview</h3>"
            f"<p>{_e(x.acquirer_overview.text)}{_cites(x.acquirer_overview.cited_ids)}</p>"
            "<h3>Strategic fit</h3>"
            f"<p>{_e(x.strategic_fit_thesis.text)}{_cites(x.strategic_fit_thesis.cited_ids)}</p>"
        )
        out.append("</div><div>")
        out.append(
            "<h3>Valuation context</h3>"
            f'<div class="box"><p>{_e(x.valuation_context.text)}'
            f"{_cites(x.valuation_context.cited_ids)}</p></div>"
            '<h3>Risk flags</h3><ul class="risks">'
            + "".join(
                f"<li><em>{_e(RISK_LABELS[f.category])}</em><b>{_e(f.title)}.</b> "
                f"{_e(f.detail)}{_cites(f.cited_ids)}</li>"
                for f in x.risk_flags
            )
            + "</ul>"
        )
        out.append("</div></div>")
    notes = (
        {n.transaction_id: n.takeaway for n in x.precedent_activity.precedents}
        if x
        else {p.transaction_id: "" for p in pack.precedents}
    )
    rows = []
    for tid, takeaway in notes.items():
        p = precedents.get(tid)
        if not p:
            rows.append(
                f"<tr><td><code>{_e(tid)}</code></td><td colspan=6>{_e(takeaway)}</td></tr>"
            )
            continue
        mult = f"{p.ev_ebitda_multiple}x" if p.ev_ebitda_multiple else "n/a"
        rows.append(
            f"<tr><td><code>{_e(tid)}</code><br>{p.deal_year}</td><td>{_e(p.sector)}<br>"
            f"<span class=cites>{_e(p.deal_type)}</span></td>"
            f'<td class="num">{_money(p.deal_size_mm)}</td><td class="num">{_e(mult)}</td>'
            f"<td>{_e(p.outcome)}</td><td>{_e(takeaway)}</td></tr>"
        )
    out.append(
        "<h3>Precedent activity</h3>"
        + (f"<p>{_e(x.precedent_activity.pattern)}</p>" if x else "")
        + '<table><tr><th>Deal</th><th>Sector / type</th><th class="num">EV</th>'
        '<th class="num">EV/EBITDA</th><th>Outcome</th><th>What it shows</th></tr>'
        + "".join(rows)
        + "</table>"
    )
    if x:
        out.append(f"<h3>Conviction: {_e(x.conviction)}</h3><p>{_e(x.conviction_rationale)}</p>")
        if x.data_gaps:
            out.append(
                "<h3>Also verify</h3><ul>"
                + "".join(f"<li>{_e(g)}</li>" for g in x.data_gaps)
                + "</ul>"
            )
    return "\n".join(out)


def _summary_table(
    packs: list[EvidencePack], by_rank: dict[int, RationaleResult], notes: dict[str, str]
) -> str:
    rows = []
    for pack in packs:
        r = by_rank.get(pack.rank)
        x = r.rationale if r else None
        flag = (" ⚠" if r and r.status != "ok" else "") + (" †" if pack.acquirer in notes else "")
        rows.append(
            f"<tr><td>{pack.rank}</td><td><b>{_e(pack.acquirer)}</b>{flag}</td>"
            f"<td>{_badge(x.conviction if x else None)}</td>"
            f'<td class="num">{pack.n_relevant_deals} of {pack.profile.n_deals}</td>'
            f"<td>{_e(x.headline) if x else '<i>rationale unavailable</i>'}</td></tr>"
        )
    return (
        '<table><tr><th>#</th><th>Acquirer</th><th>Conviction</th><th class="num">Similar deals'
        "</th><th>Why</th></tr>" + "".join(rows) + "</table>"
    )


def _page(body: str, foot_left: str, foot_right: str) -> str:
    return (
        f'<section class="page">{body}<div class="foot"><span>{_e(foot_left)}</span>'
        f"<span>{_e(foot_right)}</span></div></section>"
    )


def render_html(report: RankingReport, results: list[RationaleResult], meta: dict) -> str:
    t, mix, v = report.target, report.mix, report.valuation
    p = t.profile
    by_rank = {r.rank: r for r in results}
    total = len(report.packs)
    margin = f"{t.target_margin_pct}%" + (
        " (assumed: sector 75th percentile)" if t.margin_is_assumed else ""
    )
    geography = p.region or ("regional, region not specified" if p.regional else "national")
    sponsors = [k for k in report.packs if k.acquirer_type == AcquirerType.SPONSOR]
    strategics = [k for k in report.packs if k.acquirer_type == AcquirerType.STRATEGIC]
    foot = f"Likely acquirers · {p.name}"

    cover = [
        f'<div class="topline"><span>Buyer list · data through {t.as_of_year}</span>'
        f"<span>{_e(meta.get('run_id', ''))}</span></div>",
        f"<h1>Likely acquirers: {_e(p.name)}</h1>",
        f'<p class="sub">{_e(p.sector)} · ~${p.deal_size_mm:,.0f}M EV · '
        f"{_e(p.ownership or 'ownership n/a')} · EBITDA margin {_e(margin)} · {_e(geography)}</p>",
        '<p class="note">Built from a curated dataset of historical deals. Acquirer names are '
        "real; deal data is illustrative. Every figure comes from that dataset. Conviction "
        "measures the strength of each buyer's evidence (rubric in the appendix), not its "
        "rank.</p>",
        f"<p><b>Mix: {mix.sponsor_slots} sponsors / {mix.strategic_slots} strategics.</b> Sized "
        f"from the strategic share of all {mix.sector_deal_count} {_e(p.sector)} deals "
        f"({mix.strategic_share_used:.0%}){_e(_shortfall(mix))}. Among the {v.all_comps.n} "
        f"size-matched closed comparables, {mix.comparable_strategic_share:.0%} were "
        "strategic.</p>",
    ]
    if sponsors:
        cover += [
            f"<h3>Financial sponsors ({len(sponsors)})</h3>",
            _summary_table(sponsors, by_rank, report.entity_notes),
        ]
    if strategics:
        cover += [
            f"<h3>Strategic buyers ({len(strategics)})</h3>",
            '<p class="cites">Strategic evidence is thin: each has a handful of deals, all in one '
            "sector. They are here for price tension and because they buy in this sector.</p>",
            _summary_table(strategics, by_rank, report.entity_notes),
        ]
    nxt = [f"{_kind(k)}: {n}" for k, n in mix.next_by_type.items() if n]
    if nxt:
        cover.append(f'<p class="cites">Next in line: {_e(", ".join(nxt))}.</p>')
    if report.entity_notes:
        cover.append('<p class="cites">† status note from outside the dataset; see the page.</p>')
    if report.aliases_applied:
        merged = "; ".join(f"{o} counted as {n}" for o, n in report.aliases_applied.items())
        cover.append(
            f'<p class="cites">Entities merged: {_e(merged)} ({_e(report.entity_notes_source)}; '
            "verify).</p>"
        )
    adj = ", ".join(f"{s} ({x})" for s, x in t.adjacent_sectors.items()) or "none"
    cover += [
        "<h3>Market context (applies to every page)</h3>",
        f'<div class="box"><p><b>Valuation comps</b> ({_e(v.comparable_set_basis)}, closed): '
        f"{_e(_multiples(v.all_comps))}. At or above target margin: "
        f"{_e(_multiples(v.above_target_margin_comps))}. Strategic buyers: "
        f"{_e(_multiples(v.strategic_comps))}. Sponsors: {_e(_multiples(v.sponsor_comps))}.</p>"
        f"<p><b>Adjacent sectors used for sponsors</b> (buyer-overlap lift): {_e(adj)}.</p></div>",
        "<h3>Verify before outreach (every buyer)</h3><ul>"
        + "".join(f"<li>{_e(c.format(as_of=t.as_of_year))}</li>" for c in SHARED_CHECKS)
        + "</ul>",
    ]
    pages = [_page("\n".join(cover), foot, "Summary")]
    for pack in report.packs:
        card = buyer_card(
            pack,
            by_rank.get(pack.rank),
            total,
            report.entity_notes.get(pack.acquirer),
            report.entity_notes_source,
        )
        pages.append(_page(card, foot, f"Buyer {pack.rank} of {total}"))

    feats = list(FEATURE_LABELS)
    w = report.feature_weights
    appendix = [
        "<h2>Appendix: how the list is built</h2>",
        "<p>Score = weighted sum of five features, each 0-1, with ratio features shrunk toward the "
        "acquirer type's average in proportion to deal count. Sponsors and strategics are ranked "
        "separately; the mix sets how many of each are listed. Only buyers with at least one "
        "similar deal are listed.</p>",
        '<table><tr><th>Feature</th><th class="num">Weight</th><th>Definition</th></tr>'
        + "".join(
            f'<tr><td>{FEATURE_LABELS[f]}</td><td class="num">{w.get(f, 0):.2f}</td>'
            f"<td>{_e(FEATURE_DEFINITIONS[f])}</td></tr>"
            for f in feats
        )
        + "</table>",
        "<h3>Conviction rubric</h3><p>Low = at most 1 similar deal, no deal in the last 3 years, "
        "or size fit below 0.5. High = at least 3 similar deals and size fit at least 0.7. "
        "Otherwise Medium. Capped at Medium with 2 or fewer similar deals.</p>",
        "<h3>Scores</h3><table><tr><th>#</th><th>Acquirer</th><th class=num>Score</th>"
        + "".join(f'<th class="num">{FEATURE_LABELS[f]}</th>' for f in feats)
        + "</tr>"
        + "".join(
            f"<tr><td>{k.rank}</td><td>{_e(k.acquirer)}</td><td class=num>{k.score:.3f}</td>"
            + "".join(f'<td class="num">{k.score_breakdown.get(f, 0):.2f}</td>' for f in feats)
            + "</tr>"
            for k in report.packs
        )
        + "</table>",
        (
            f"<h3>Backtest</h3><p>{_e(backtest_sentence(meta['backtest']))}</p>"
            if meta.get("backtest")
            else ""
        ),
        f'<p class="cites" style="margin-top:10px">Run {_e(meta.get("run_id", ""))} · writer '
        f"{_e(meta.get('models', ''))} · prompt {_e(meta.get('prompt_version', ''))}. "
        "Print this page to PDF from a browser (A4, no margins, background graphics on).</p>",
    ]
    pages.append(_page("\n".join(appendix), foot, "Appendix"))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Likely acquirers · {_e(p.name)}</title><style>{CSS}</style></head><body>"
        + "\n".join(pages)
        + "</body></html>\n"
    )
