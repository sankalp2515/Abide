"""Test setup: the engine tests use a reference agent's policy as a fixture (tests/fixtures/policies). Tests see the
product's policies (policies/) plus those fixtures in one temporary policy folder, so nothing is written to policies/."""

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _policy_dir(tmp_path_factory, monkeypatch):
    target = tmp_path_factory.mktemp("policies")
    for source in (ROOT / "policies", ROOT / "tests" / "fixtures" / "policies"):
        for f in source.glob("*.yaml"):
            shutil.copy2(f, target / f.name)
    monkeypatch.setenv("INTENTSEAL_POLICY_DIR", str(target))
    yield
