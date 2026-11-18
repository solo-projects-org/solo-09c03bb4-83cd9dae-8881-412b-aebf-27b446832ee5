"""Shared pytest fixtures."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    storage_dir = tmp_path / "storage"
    monkeypatch.setenv("CATALOG_DB_PATH", str(db_path))
    monkeypatch.setenv("CATALOG_STORAGE_DIR", str(storage_dir))
    # settings is cached via lru_cache; each test gets fresh env values
    from app import config
    config.get_settings.cache_clear()

    from app.main import app
    with TestClient(app) as c:
        yield c
