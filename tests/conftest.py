"""pytest 共享夹具：每个测试使用独立的临时 SQLite 文件。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test_app.db")


@pytest.fixture()
def client(db_path):
    app = create_app(db_path=db_path)
    with TestClient(app) as test_client:
        test_client.app_state = app.state
        yield test_client


def make_template_payload():
    """一份合法且可成功推演的模板（菱形分支 + latest）。"""
    return {
        "points": [
            {"id": "a", "release": 0, "latest": 100},
            {"id": "b", "release": 0, "latest": 100},
            {"id": "c", "release": 0, "latest": 100},
            {"id": "d", "release": 1, "latest": 100},
        ],
        "relations": [
            {"from": "a", "to": "b", "min_gap": 5},
            {"from": "a", "to": "c", "min_gap": 20},
            {"from": "b", "to": "d", "min_gap": 3},
            {"from": "c", "to": "d", "min_gap": 0},
        ],
    }
