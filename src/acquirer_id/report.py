"""Render the banker-facing Markdown report. Formatting only; no business logic."""

from __future__ import annotations

from acquirer_id.data import AcquirerType
from acquirer_id.evidence import EvidencePack, MixSummary, MultipleStats, RankingReport
from acquirer_id.rationale import RationaleResult

RISK_LABELS = {
    "competitive_process": "Competitive process",
    "financing": "Financing",
    "execution": "Execution",
    "antitrust_or_overlap": "Antitrust / overlap",
    "integration": "Integration",
    "appetite": "Appetite",
    "thin_evidence": "Thin evidence",
    "other": "Other",
}


def _kind(t: AcquirerType) -> str:
    return "Strategic" if t == AcquirerType.STRATEGIC else "Sponsor"


def _multiples(m: MultipleStats) -> str:
    if not m.n:
        return "no deals"
    if m.median_ev_ebitda is None and m.ev_ebitda_values:  # too few deals for a median
        vals = ", ".join(f"{x}x" for x in m.ev_ebitda_values)
        return f"EV/EBITDA {vals} (n={m.n}, too few for a median)"
    ebitda = f"{m.median_ev_ebitda}x" if m.median_ev_ebitda is not None else "n/a"
    rev = f"{m.median_ev_revenue}x" if m.median_ev_revenue is not None else "n/a"
    return f"{ebitda} EV/EBITDA, {rev} EV/Revenue (n={m.n})"


MAX_INLINE_IDS = 6  # longer citation lists (e.g. the full comp set) collapse to a count


def _ids(ids: list[str]) -> str:
    if not ids:
        return ""
    if len(ids) > MAX_INLINE_IDS:
        return f" _[{len(ids)} deals cited; IDs in rationales.json]_"
    return f" _[{', '.join(ids)}]_"


FEATURE_LABELS = {
    "relevant_activity": "Relevant activity",
    "focus": "Focus",
    "size_fit": "Size fit",
    "recency": "Recency",
    "close_rate": "Close rate",
}
FEATURE_DEFINITIONS = {
    "relevant_activity": "sum over the acquirer's deals of sector weight x size similarity x "
    "recency x outcome x ownership/region match, log-scaled to the most active acquirer",
    "focus": "average sector weight x size similarity per deal: how much of its activity looks "
    "like this target",
    "size_fit": "similarity of the acquirer's median deal size to the target's",
    "recency": "average recency weight of its deals (4-year half-life)",
    "close_rate": "share of its deals that closed",
}
SHARED_CHECKS = [
    "Current status, ownership and acquisition appetite of each buyer: the dataset ends in "
    "{as_of}.",
    "The target's real EBITDA margin, geography and service mix (assumed below where not given).",
    "Financing capacity and portfolio conflicts for sponsors; integration capacity for strategics.",
]


def _table(
    packs: list[EvidencePack], by_rank: dict[int, RationaleResult], notes: dict[str, str]
) -> list[str]:
    lines = ["| # | Acquirer | Conviction | Similar deals | Why |", "|---|---|---|---|---|"]
    for pack in packs:
        r = by_rank.get(pack.rank)
        conviction = r.rationale.conviction if r and r.rationale else "n/a"
        why = r.rationale.headline if r and r.rationale else "_rationale unavailable; see evidence_"
        flag = (" ⚠" if r and r.status != "ok" else "") + (" †" if pack.acquirer in notes else "")
        lines.append(
            f"| {pack.rank} | {pack.acquirer}{flag} | {conviction} "
            f"| {pack.n_relevant_deals} of {pack.profile.n_deals} | {why} |"
        )
    return lines


def _shortfall(mix: MixSummary) -> str:
    """Why a type got fewer slots than its share implies: too few buyers with a similar deal."""
    wanted = round(mix.strategic_slots_from_data)
    if mix.floor_applied or wanted == mix.strategic_slots:
        return ""
    if mix.strategic_slots < wanted:
        n, short, other = mix.strategic_slots, "strategics", "sponsors"
    else:
        n, short, other = mix.sponsor_slots, "sponsors", "strategics"
    return f"; only {n} {short} have a similar deal, so the remaining slots go to {other}"


def backtest_sentence(bt: dict) -> str:
    """Plain text for the appendix of both reports: validation is for the reader who asks how
    good the method is, not for the cover."""
    return (
        f"Replaying each of {bt['n']} deals from 2022-24 using only earlier deals, the actual "
        f"buyer appears in this method's top 10 in {bt['scorer_hit']:.0%} of cases, vs "
        f"{bt['baseline_hit']:.0%} for the 10 most active buyers in the sector. On this dataset "
        "the method matches that simple baseline; its value is the evidence explained per name."
    )


def _backtest_line(bt: dict) -> str:
    return "**Backtest:** " + backtest_sentence(bt)


def render(report: RankingReport, results: list[RationaleResult], meta: dict) -> str:
    t, mix, v = report.target, report.mix, report.valuation
    by_rank = {r.rank: r for r in results}
    p = t.profile
    margin = f"{t.target_margin_pct}%" + (
        f" (assumed: {p.sector} 75th percentile)" if t.margin_is_assumed else ""
    )
    geography = p.region or ("regional, region not specified" if p.regional else "national")
    sponsors = [k for k in report.packs if k.acquirer_type == AcquirerType.SPONSOR]
    strategics = [k for k in report.packs if k.acquirer_type == AcquirerType.STRATEGIC]
    lines = [
        f"# Likely acquirers: {p.name}",
        "",
        f"_{p.sector} · ~${p.deal_size_mm:,.0f}M EV · {p.ownership or 'ownership n/a'} · "
        f"EBITDA margin {margin} · {geography}_",
        "",
        f"> Built from a curated dataset of historical deals through {t.as_of_year}. Acquirer "
        "names are real; deal data is illustrative. Every figure comes from that dataset. "
        "Conviction measures the strength of each buyer's evidence (rubric in the appendix), "
        "not its rank: rank comes from the score.",
        "",
        "## Summary",
        "",
        f"**Mix: {mix.sponsor_slots} sponsors / {mix.strategic_slots} strategics.** Sized from the "
        f"strategic share of all {mix.sector_deal_count} {p.sector} deals "
        f"({mix.strategic_share_used:.0%})"
        + (", with a minimum of each type applied" if mix.floor_applied else "")
        + _shortfall(mix)
        + f". Among the {v.all_comps.n} size-matched closed comparables, "
        f"{mix.comparable_strategic_share:.0%} were strategic: weighting size over sector would "
        "tilt the list toward sponsors.",
        "",
    ]
    if sponsors:
        lines += [f"### Financial sponsors ({len(sponsors)})", ""] + _table(
            sponsors, by_rank, report.entity_notes
        )
        lines += [""]
    if strategics:
        lines += [
            f"### Strategic buyers ({len(strategics)})",
            "",
            "_Strategic evidence is thin: each has a handful of deals, all in one sector. They "
            "are here for price tension and because they buy in this sector, not on volume._",
            "",
        ] + _table(strategics, by_rank, report.entity_notes)
    nxt = [f"{_kind(k)}: {n}" for k, n in mix.next_by_type.items() if n]
    if nxt:
        lines += ["", f"Next in line: {', '.join(nxt)}."]
    if any(r.status != "ok" for r in results):
        lines += ["", "⚠ = automated checks flagged this page; see its review note."]
    if report.entity_notes:
        lines += ["", "† = status note from outside the dataset; see the buyer's page."]
    if report.aliases_applied:
        merged = "; ".join(f"{old} counted as {new}" for old, new in report.aliases_applied.items())
        lines += [
            "",
            f"_Entities merged: {merged} ({report.entity_notes_source}; verify)._",
        ]
    adj = ", ".join(f"{s} ({x})" for s, x in t.adjacent_sectors.items()) or "none"
    lines += [
        "",
        "## Market context (applies to every page)",
        "",
        f"**Valuation comps** ({v.comparable_set_basis}, closed): {_multiples(v.all_comps)}. "
        f"At or above target margin: {_multiples(v.above_target_margin_comps)}. "
        f"Strategic buyers: {_multiples(v.strategic_comps)}. "
        f"Sponsors: {_multiples(v.sponsor_comps)}.  ",
        f"**Adjacent sectors used for sponsors** (buyer-overlap lift): {adj}.",
        "",
        "**Verify before outreach (every buyer):**",
        "",
    ]
    lines += [f"- {c.format(as_of=t.as_of_year)}" for c in SHARED_CHECKS]
    lines += ["", "---", ""]
    for pack in report.packs:
        note = report.entity_notes.get(pack.acquirer)
        lines += _page(pack, by_rank.get(pack.rank), note, report.entity_notes_source)
    lines += _appendix(report, meta)
    return "\n".join(lines).rstrip() + "\n"


def _appendix(report: RankingReport, meta: dict) -> list[str]:
    feats = list(FEATURE_LABELS)
    w = report.feature_weights
    lines = [
        "## Appendix: how the list is built",
        "",
        "**Score** = weighted sum of five features, each 0-1, with ratio features shrunk toward "
        "the acquirer type's average in proportion to deal count. Sponsors and strategics are "
        "ranked separately, then the mix above sets how many of each are listed.",
        "",
        "| Feature | Weight | Definition |",
        "|---|---|---|",
    ]
    lines += [
        f"| {FEATURE_LABELS[f]} | {w.get(f, 0):.2f} | {FEATURE_DEFINITIONS[f]} |" for f in feats
    ]
    lines += [
        "",
        "**Conviction rubric:** Low = at most 1 similar deal, no deal in the last 3 years, or size "
        "fit below 0.5. High = at least 3 similar deals and size fit at least 0.7. Otherwise "
        "Medium. Capped at Medium with 2 or fewer similar deals. A *similar deal* is in the "
        "target's sector (or a strongly adjacent one, for sponsors) and near its size.",
        "",
        "| # | Acquirer | Score | " + " | ".join(FEATURE_LABELS[f] for f in feats) + " |",
        "|---|---|---|" + "---|" * len(feats),
    ]
    for pack in report.packs:
        b = pack.score_breakdown
        lines.append(
            f"| {pack.rank} | {pack.acquirer} | {pack.score:.3f} | "
            + " | ".join(f"{b.get(f, 0):.2f}" for f in feats)
            + " |"
        )
    if meta.get("backtest"):
        lines += ["", _backtest_line(meta["backtest"])]
    lines += [
        "",
        f"_Run {meta['run_id']} · writer {meta['models']} · prompt {meta['prompt_version']}._",
    ]
    return lines


def _page(
    pack: EvidencePack, r: RationaleResult | None, note: str | None = None, source: str = ""
) -> list[str]:
    head = f"## {pack.rank}. {pack.acquirer} · {_kind(pack.acquirer_type)}"
    status = [f"> **Status (outside the dataset; {source}):** {note}", ""] if note else []
    precedents = {p.transaction_id: p for p in pack.precedents}
    if not r or not r.rationale:
        lines = [
            head,
            "",
            *status,
            "_Rationale unavailable (model error"
            + (f": {r.error}" if r and r.error else "")
            + "). Evidence only:_",
            "",
        ]
        lines += [_precedent_line(p.transaction_id, "", precedents) for p in pack.precedents]
        return lines + ["", "---", ""]

    x = r.rationale
    lines = [f"{head} · **{x.conviction} conviction**", "", *status, f"**{x.headline}**", ""]
    if r.issues:
        lines += [
            "> **Review note:** automated checks flagged "
            + "; ".join(f"{i.code} ({i.detail})" for i in r.issues)
            + ".",
            "",
        ]
    lines += [
        "**Acquirer overview.** " + x.acquirer_overview.text + _ids(x.acquirer_overview.cited_ids),
        "",
        "**Strategic fit.** "
        + x.strategic_fit_thesis.text
        + _ids(x.strategic_fit_thesis.cited_ids),
        "",
        "**Precedent activity.** " + x.precedent_activity.pattern,
        "",
    ]
    lines += [
        _precedent_line(n.transaction_id, n.takeaway, precedents)
        for n in x.precedent_activity.precedents
    ]
    lines += [
        "",
        "**Valuation context.** " + x.valuation_context.text + _ids(x.valuation_context.cited_ids),
        "",
        "**Risk flags.**",
        "",
    ]
    lines += [
        f"- _{RISK_LABELS[f.category]}_: **{f.title}.** {f.detail}{_ids(f.cited_ids)}"
        for f in x.risk_flags
    ]
    lines += ["", f"**Conviction: {x.conviction}.** {x.conviction_rationale}", ""]
    if x.data_gaps:
        lines += ["**Also verify.**", ""] + [f"- {g}" for g in x.data_gaps] + [""]
    return lines + ["---", ""]


def _precedent_line(tid: str, takeaway: str, precedents: dict) -> str:
    p = precedents.get(tid)
    if not p:
        return f"- `{tid}`: {takeaway}"
    multiple = f", {p.ev_ebitda_multiple}x EV/EBITDA" if p.ev_ebitda_multiple else ""
    facts = (
        f"{p.deal_year} · {p.sector} · {p.deal_type} · ${p.deal_size_mm:,.1f}M{multiple} · "
        f"{p.outcome}"
    )
    return f"- `{tid}` ({facts}){': ' + takeaway if takeaway else ''}"
