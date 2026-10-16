"""Settings names (D-038): INTENTSEAL_* is the current prefix; ASG_* still works when the new name is unset."""

from __future__ import annotations

import os

from intentseal_core import adopt_legacy_env


def test_legacy_prefix_fills_only_unset_names(monkeypatch):
    monkeypatch.setenv("ASG_PUBLIC_KEY", "legacy-value")
    monkeypatch.delenv("INTENTSEAL_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("ASG_CORS_ORIGINS", "https://old.example.test")
    monkeypatch.setenv("INTENTSEAL_CORS_ORIGINS", "https://new.example.test")
    adopt_legacy_env()
    assert os.environ["INTENTSEAL_PUBLIC_KEY"] == "legacy-value"
    assert os.environ["INTENTSEAL_CORS_ORIGINS"] == "https://new.example.test"
    monkeypatch.delenv("INTENTSEAL_PUBLIC_KEY")
