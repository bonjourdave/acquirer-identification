import json
from pathlib import Path

import pytest

from acquirer_id.cli import main
from acquirer_id.evidence import RankingReport
from tests.conftest import FIXTURE_CSV, REAL_CSV


def _config(tmp_path, sector="Healthcare Services", k=3):
    path = tmp_path / "target.yaml"
    path.write_text(
        f"target:\n  sector: {sector}\n  deal_size_mm: 200\n"
        f"scoring:\n  k: {k}\n  min_per_type: 1\n  comps_min_deals: 2\n"
    )
    return path


def test_rank_writes_valid_report_and_run_metadata(tmp_path, capsys):
    rc = main(
        [
            "rank",
            "--csv",
            str(FIXTURE_CSV),
            "--config",
            str(_config(tmp_path)),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    (run_dir,) = (tmp_path / "out").iterdir()
    report = RankingReport.model_validate_json((run_dir / "ranking.json").read_text())
    # k=3, but only 2 fixture candidates have a similar deal; the list is not padded
    assert len(report.packs) == 2
    assert all(p.n_relevant_deals >= 1 for p in report.packs)
    run = json.loads((run_dir / "run.json").read_text())
    assert run["config"]["target"]["sector"] == "Healthcare Services"
    assert len(run["csv_sha256"]) == 64
    assert "Sponsor A" in capsys.readouterr().out


def test_unknown_sector_exits_nonzero_with_message(tmp_path, capsys):
    rc = main(
        [
            "rank",
            "--csv",
            str(FIXTURE_CSV),
            "--config",
            str(_config(tmp_path, "Software")),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 2
    assert "no deals" in capsys.readouterr().err


@pytest.mark.skipif(not REAL_CSV.exists(), reason="real dataset not present (see data/README.md)")
def test_default_target_on_real_data(tmp_path):
    rc = main(["rank", "--csv", str(REAL_CSV), "--out", str(tmp_path / "out")])
    assert rc == 0
    (run_dir,) = (tmp_path / "out").iterdir()
    report = RankingReport.model_validate_json((run_dir / "ranking.json").read_text())
    assert len(report.packs) == 10
    assert report.mix.strategic_slots >= 2  # configs/default_target.yaml sets min_per_type: 2
    assert len({p.acquirer for p in report.packs}) == 10


def test_target_flags_override_the_config(tmp_path):
    rc = main(
        [
            "rank",
            "--csv",
            str(FIXTURE_CSV),
            "--config",
            str(_config(tmp_path)),
            "--out",
            str(tmp_path / "out"),
            "--size",
            "180",
            "--region",
            "Southeast",
            "--margin",
            "15",
        ]
    )
    assert rc == 0
    (run_dir,) = (tmp_path / "out").iterdir()
    t = json.loads((run_dir / "run.json").read_text())["config"]["target"]
    assert (t["deal_size_mm"], t["region"], t["regional"], t["ebitda_margin_pct"]) == (
        180.0,
        "Southeast",
        True,
        15.0,
    )
    assert (
        t["description"]
        == "Private Healthcare Services company, in the Southeast region, 15.0% EBITDA margin."
    )


def test_invalid_target_flag_is_a_clean_error(tmp_path, capsys):
    rc = main(
        ["rank", "--csv", str(FIXTURE_CSV), "--config", str(_config(tmp_path)), "--size", "-5"]
    )
    assert rc == 2
    assert "greater than 0" in capsys.readouterr().err


def test_entity_notes_path_resolves_from_the_config_not_the_working_directory(
    tmp_path, monkeypatch
):
    from acquirer_id.config import load_run_config

    config = Path(__file__).parents[1] / "configs" / "default_target.yaml"
    monkeypatch.chdir(tmp_path)
    run = load_run_config(config)
    assert run.entity_notes == config.parent / "entity_notes.yaml"
    assert run.entity_notes.exists()
