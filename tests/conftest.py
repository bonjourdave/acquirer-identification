from pathlib import Path

import pytest

from acquirer_id.config import ScoringConfig, TargetProfile
from acquirer_id.data import Transaction, load_transactions

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "transactions_small.csv"
REAL_CSV = Path(__file__).parents[1] / "data" / "ma_transactions_500.csv"


@pytest.fixture
def deals() -> list[Transaction]:
    return load_transactions(FIXTURE_CSV)


@pytest.fixture
def target() -> TargetProfile:
    return TargetProfile(sector="Healthcare Services", deal_size_mm=200)


@pytest.fixture
def cfg() -> ScoringConfig:
    return ScoringConfig(k=3, min_per_type=1, comps_min_deals=2)
