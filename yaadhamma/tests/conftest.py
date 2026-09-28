"""Suite-wide fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_cost_db(tmp_path, monkeypatch):
    """Keep model-call cost records out of the real database during tests."""
    import costs

    monkeypatch.setattr(costs, "DEFAULT_DB", tmp_path / "costs-test.db")
