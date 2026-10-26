from pathlib import Path

import pytest

from acquirer_id.data import AcquirerType, DataError, Outcome, load_transactions
from tests.conftest import FIXTURE_CSV


def test_loads_typed_records(deals):
    assert len(deals) == 13
    t01 = next(d for d in deals if d.transaction_id == "T01")
    assert t01.acquirer_type is AcquirerType.SPONSOR
    assert t01.outcome is Outcome.CLOSED
    assert t01.rationale_tags == ("Geographic Expansion", "Platform Build")
    assert not hasattr(t01, "sub_sector")


def test_blank_cells_become_none(deals):
    withdrawn = next(d for d in deals if d.transaction_id == "T04")
    assert withdrawn.days_to_close is None


def _write_variant(tmp_path: Path, edit) -> Path:
    lines = FIXTURE_CSV.read_text().splitlines()
    lines = edit(lines)
    out = tmp_path / "variant.csv"
    out.write_text("\n".join(lines) + "\n")
    return out


def test_reports_every_invalid_row(tmp_path):
    def corrupt(lines):
        lines[1] = lines[1].replace(",200,", ",-5,")  # negative deal size
        lines[2] = lines[2].replace("Closed", "Maybe")  # unknown outcome
        return lines

    with pytest.raises(DataError) as e:
        load_transactions(_write_variant(tmp_path, corrupt))
    assert "2 invalid row(s)" in str(e.value)
    assert "line 2 (T01): deal_size_mm" in str(e.value)
    assert "line 3 (T02): outcome" in str(e.value)


def test_rejects_duplicate_ids(tmp_path):
    with pytest.raises(DataError, match="Duplicate"):
        load_transactions(_write_variant(tmp_path, lambda ls: ls + [ls[1]]))


def test_missing_file_points_to_csv_flag(tmp_path):
    with pytest.raises(DataError, match="--csv"):
        load_transactions(tmp_path / "nope.csv")


def test_aliases_merge_deals_under_the_current_entity(deals):
    from acquirer_id.data import apply_aliases

    merged = apply_aliases(deals, {"Health System Y": "Health System X"})
    assert sum(d.acquirer == "Health System X" for d in merged) == 3
    assert not any(d.acquirer == "Health System Y" for d in merged)
    assert len(merged) == len(deals)
