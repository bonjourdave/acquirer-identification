from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tests.conftest import FIXTURE_CSV  # noqa: E402

ROOT = Path(__file__).parents[1]
APP = ROOT / "src" / "acquirer_id" / "app.py"


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    """Run each app test from an empty directory with no key in the environment, so a
    developer's real .streamlit/secrets.toml (read from the working directory) never reaches a
    test and no test can call the API."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ACQUIRER_ID_CONFIG", str(ROOT / "configs" / "default_target.yaml"))


def test_without_a_key_the_app_ranks_and_says_why_it_cannot_write(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ACQUIRER_ID_CSV", str(FIXTURE_CSV))
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception
    at.get("form_submit_button")[0].click().run()
    assert not at.exception
    assert at.dataframe, "ranking table shown"
    assert any("OPENAI_API_KEY" in i.value for i in at.info)


def test_missing_dataset_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ACQUIRER_ID_CSV", str(tmp_path / "missing.csv"))
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert any("Dataset not found" in e.value for e in at.error)


def test_key_from_streamlit_secrets_enables_writing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ACQUIRER_ID_CSV", str(FIXTURE_CSV))
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.secrets["OPENAI_API_KEY"] = "sk-test-not-real"
    at.run()
    assert not at.exception
    assert at.get("form_submit_button")[0].label == "Find acquirers"
