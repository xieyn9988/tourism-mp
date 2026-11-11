from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TEST_DB = Path(tempfile.gettempdir()) / f"tourism_mp_tests_{os.getpid()}.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_db():
    yield
    try:
        import main

        main.engine.dispose()
    except Exception:  # noqa: BLE001 - 清理失败不应影响测试结论
        pass
    try:
        TEST_DB.unlink(missing_ok=True)
    except OSError:
        pass
