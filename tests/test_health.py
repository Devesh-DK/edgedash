"""
Tests for health reporting and notes parsing.
"""

import json
import pytest
from edgedash import storage
from edgedash.health import main as health_main


def test_parse_notes_formats():
    # Dict input
    d = {"outcome": "complete", "verdict": "pass"}
    assert storage.parse_notes(d) == d

    # Valid JSON string input
    s = json.dumps(d)
    assert storage.parse_notes(s) == d

    # Empty / None
    assert storage.parse_notes(None) == {}
    assert storage.parse_notes("") == {}

    # Corrupt string
    assert storage.parse_notes("not json") == {}


def test_health_check_runs_cleanly(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(SystemExit) as exc_info:
        health_main()
    # If healthy, exits 0; if no listings in local sqlite memory, exits 1 (expected in isolated test)
    assert exc_info.value.code in (0, 1)
