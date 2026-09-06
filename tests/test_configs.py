"""Tests for per-community config persistence and community-name validation."""
import json
import logging

import pytest

import configs as c


def test_is_valid_community():
    for good in ("riskofrain2", "v-rising", "a_b.c9", "A-Z"):
        assert c.is_valid_community(good), good
    for bad in ("", "..", ".hidden", "a/b", "a\\b", "a b", "../etc", "a:b",
                None, 42, "x" * 129):
        assert not c.is_valid_community(bad), repr(bad)


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "CONFIGS_DIR", tmp_path)
    c.save_config("riskofrain2", {"workers": 4, "tags": ["a", "b"]})
    assert c.load_config("riskofrain2") == {"workers": 4, "tags": ["a", "b"]}
    # Atomic write: no leftover temp file.
    assert list(tmp_path.glob("*.tmp")) == []


def test_load_corrupted_config_returns_empty_and_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(c, "CONFIGS_DIR", tmp_path)
    (tmp_path / "broken.json").write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="configs"):
        assert c.load_config("broken") == {}
    assert any("Unreadable config" in r.message for r in caplog.records)


def test_save_with_unsafe_community_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "CONFIGS_DIR", tmp_path)
    with pytest.raises(ValueError):
        c.save_config("../escape", {"a": 1})
    assert list(tmp_path.iterdir()) == []


def test_list_configs_skips_corrupted(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(c, "CONFIGS_DIR", tmp_path)
    (tmp_path / "good.json").write_text(json.dumps({"a": 1}))
    (tmp_path / "bad.json").write_text("nope")
    with caplog.at_level(logging.WARNING, logger="configs"):
        result = c.list_configs()
    assert result == [{"community": "good", "a": 1}]
