"""Make the shared fixture module importable and provide the toy class-request profile."""

import sys
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]  # + test_pipeline

from classreq_fixtures import edit  # noqa: E402
from test_pipeline import toy_profile  # noqa: E402


@pytest.fixture
def profile(tmp_path) -> Path:
    return toy_profile(tmp_path, edit)
