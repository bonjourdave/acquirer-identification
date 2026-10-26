"""Load and validate the precedent-transaction CSV into typed records."""

from __future__ import annotations

import csv
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator


class AcquirerType(StrEnum):
    STRATEGIC = "Strategic"
    SPONSOR = "Financial Sponsor"


class Outcome(StrEnum):
    CLOSED = "Closed"
    PENDING = "Pending"
    RUMORED = "Rumored"  # present in the data, not listed in the brief
    WITHDRAWN = "Withdrawn"
    TERMINATED = "Terminated"


class Transaction(BaseModel):
    transaction_id: str
    target_company: str
    acquirer: str
    sector: str
    deal_year: int = Field(ge=1990, le=2100)
    deal_quarter: str
    deal_type: str
    geography: str
    financing_type: str
    deal_size_mm: float = Field(gt=0)
    target_revenue_mm: float | None = None
    target_ebitda_mm: float | None = None
    ebitda_margin_pct: float | None = None
    revenue_growth_pct: float | None = None
    ev_ebitda_multiple: float | None = Field(default=None, gt=0)
    ev_revenue_multiple: float | None = Field(default=None, gt=0)
    synergy_pct_of_deal: float | None = None
    outcome: Outcome
    rationale_tags: tuple[str, ...]
    num_bidders: int | None = None
    days_to_close: float | None = None
    acquirer_type: AcquirerType
    target_ownership_pre: str

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if isinstance(v, str) and v.strip() == "" else v


class DataError(ValueError):
    """The CSV cannot be used as-is. Raised with every bad row listed, not just the first."""


# `sub_sector` is dropped on purpose: it equals `sector` on every row.
_RENAMES = {"strategic_rationale_tags": "rationale_tags"}


def _parse_row(raw: dict[str, str]) -> Transaction:
    row: dict[str, object] = {_RENAMES.get(k, k): v for k, v in raw.items() if k != "sub_sector"}
    tags = str(row.get("rationale_tags") or "")
    row["rationale_tags"] = tuple(t.strip() for t in tags.split("|") if t.strip())
    return Transaction.model_validate(row)


def load_transactions(path: Path) -> list[Transaction]:
    if not path.exists():
        raise DataError(f"CSV not found: {path}. Pass --csv or see data/README.md.")
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows, errors = [], []
        for line_no, raw in enumerate(reader, start=2):
            try:
                rows.append(_parse_row(raw))
            except ValidationError as e:
                fields = ", ".join(str(err["loc"][0]) for err in e.errors())
                errors.append(f"line {line_no} ({raw.get('transaction_id', '?')}): {fields}")
    if errors:
        raise DataError(f"{len(errors)} invalid row(s):\n  " + "\n  ".join(errors))
    if not rows:
        raise DataError(f"No transactions in {path}")
    ids = [t.transaction_id for t in rows]
    if len(set(ids)) != len(ids):
        raise DataError("Duplicate transaction_id values in CSV")
    return rows


def apply_aliases(deals: list[Transaction], aliases: dict[str, str]) -> list[Transaction]:
    """Merge deals of renamed or combined entities under the current name."""
    return [
        d.model_copy(update={"acquirer": aliases[d.acquirer]}) if d.acquirer in aliases else d
        for d in deals
    ]
