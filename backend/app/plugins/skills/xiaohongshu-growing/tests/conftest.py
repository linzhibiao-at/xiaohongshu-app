"""pytest 公共配置：隔离数据目录、把 scripts 加入 import 路径。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """每个用例用独立的 SQLite 库，避免互相污染。"""
    monkeypatch.setenv("GROWING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROWING_REPORT_DIR", str(tmp_path / "reports"))
    yield tmp_path
