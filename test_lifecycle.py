"""
门票生命周期、入园/离园、园区配置测试。

运行：pytest -q test_lifecycle.py
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as backend

TEST_DB = Path(os.environ["DATABASE_URL"].removeprefix("sqlite:///"))
NOW = datetime(2026, 11, 11, 4, 0, 0)  # 北京时间 12:00，未闭园


@pytest.fixture(scope="module")
def client():
    with TestClient(backend.app) as test_client:
        yield test_client


def _eval(**overrides):
    params = dict(
        entered_at=None,
        exited_at=None,
        has_ticket=True,
        has_pending_items=True,
        refund_after_lifecycle=False,
        refund_window_hours=72,
        now=NOW,
    )
    params.update(overrides)
    return backend.evaluate_lifecycle(**params)


def test_lifecycle_not_applicable_without_ticket():
    snapshot = _eval(has_ticket=False)
    assert snapshot.status == backend.LifecycleStatus.NOT_APPLICABLE
    assert snapshot.can_split is True
    assert snapshot.can_verify is True
    assert snapshot.can_refund is True
    assert snapshot.leave_at is None


def test_lifecycle_not_started_before_and_after_closing():
    before = _eval(entered_at=None)
    assert before.status == backend.LifecycleStatus.NOT_STARTED
    assert before.reason == backend.LifecycleReason.NOT_ENTERED
    assert before.can_verify is True
    assert before.can_refund is True
    assert before.can_split is False

    after = _eval(now=datetime(2026, 11, 11, 11, 0, 0))  # 北京 19:00
    assert after.status == backend.LifecycleStatus.NOT_STARTED
    assert after.reason == backend.LifecycleReason.PARK_CLOSED
    assert after.can_verify is False


def test_lifecycle_active_in_park_uses_utc_closing_boundary():
    snapshot = _eval(entered_at=datetime(2026, 11, 11, 2, 0, 0))
    assert snapshot.status == backend.LifecycleStatus.ACTIVE
    assert snapshot.reason == backend.LifecycleReason.IN_PARK
    assert snapshot.leave_at == datetime(2026, 11, 11, 10, 0, 0)
    assert snapshot.can_verify is True
    assert snapshot.can_refund is True
    assert snapshot.can_split is False


def test_all_items_settled_completes_immediately():
    snapshot = _eval(
        entered_at=datetime(2026, 11, 11, 2, 0, 0),
        has_pending_items=False,
    )
    assert snapshot.status == backend.LifecycleStatus.COMPLETED
    assert snapshot.reason == backend.LifecycleReason.ALL_ITEMS_SETTLED
    assert snapshot.can_split is True
    assert snapshot.can_verify is False
    assert snapshot.can_refund is False


def test_closing_completes_when_post_leave_refund_disabled():
    snapshot = _eval(
        entered_at=datetime(2026, 11, 11, 2, 0, 0),
        now=datetime(2026, 11, 11, 11, 0, 0),
        refund_after_lifecycle=False,
    )
    assert snapshot.status == backend.LifecycleStatus.COMPLETED
    assert snapshot.reason == backend.LifecycleReason.POST_LEAVE_REFUND_DISABLED
    assert snapshot.can_split is True
    assert snapshot.can_refund is False


def test_refund_window_extends_lifecycle_until_exact_deadline():
    entered_at = datetime(2026, 11, 11, 2, 0, 0)
    deadline = datetime(2026, 11, 14, 10, 0, 0)

    open_snapshot = _eval(
        entered_at=entered_at,
        now=datetime(2026, 11, 11, 11, 0, 0),
        refund_after_lifecycle=True,
    )
    assert open_snapshot.status == backend.LifecycleStatus.ACTIVE
    assert open_snapshot.reason == backend.LifecycleReason.POST_LEAVE_REFUND_WINDOW
    assert open_snapshot.refund_deadline == deadline
    assert open_snapshot.can_refund is True
    assert open_snapshot.can_verify is False
    assert open_snapshot.can_split is False

    about_to_expire = _eval(
        entered_at=entered_at,
        now=datetime(2026, 11, 14, 9, 59, 0),
        refund_after_lifecycle=True,
    )
    assert about_to_expire.status == backend.LifecycleStatus.ACTIVE

    expired = _eval(
        entered_at=entered_at,
        now=deadline,
        refund_after_lifecycle=True,
    )
    assert expired.status == backend.LifecycleStatus.COMPLETED
    assert expired.reason == backend.LifecycleReason.REFUND_WINDOW_EXPIRED
    assert expired.can_split is True


def test_exit_scan_shortens_leave_at():
    entered_at = datetime(2026, 11, 11, 2, 0, 0)
    exited_at = datetime(2026, 11, 11, 4, 0, 0)

    completed = _eval(
        entered_at=entered_at,
        exited_at=exited_at,
        now=datetime(2026, 11, 11, 8, 0, 0),
        refund_after_lifecycle=True,
        refund_window_hours=3,
    )
    assert completed.leave_at == exited_at
    assert completed.status == backend.LifecycleStatus.COMPLETED
    assert completed.reason == backend.LifecycleReason.REFUND_WINDOW_EXPIRED

    still_open = _eval(
        entered_at=entered_at,
        exited_at=exited_at,
        now=datetime(2026, 11, 11, 8, 0, 0),
        refund_after_lifecycle=True,
        refund_window_hours=5,
    )
    assert still_open.status == backend.LifecycleStatus.ACTIVE
    assert still_open.refund_deadline == datetime(2026, 11, 11, 9, 0, 0)


def test_early_exit_completes_when_post_leave_refund_disabled():
    snapshot = _eval(
        entered_at=datetime(2026, 11, 11, 2, 0, 0),
        exited_at=datetime(2026, 11, 11, 4, 0, 0),
        now=datetime(2026, 11, 11, 5, 0, 0),
        refund_after_lifecycle=False,
    )
    assert snapshot.status == backend.LifecycleStatus.COMPLETED
    assert snapshot.reason == backend.LifecycleReason.POST_LEAVE_REFUND_DISABLED
    assert snapshot.can_split is True


def _signed_request(client: TestClient, method: str, path: str, *, json_body=None):
    body = b""
    if json_body is not None:
        body = json.dumps(
            json_body, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    headers = backend.build_signature_headers(method, path, body=body)
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, headers=headers, content=body)


def test_park_settings_update_requires_signature_and_validates(client: TestClient):
    path = "/api/park-settings"

    unsigned = client.put(
        path,
        json={"refund_after_lifecycle": True, "refund_window_hours": 24},
    )
    assert unsigned.status_code == 403, unsigned.text

    signed = _signed_request(
        client,
        "PUT",
        path,
        json_body={"refund_after_lifecycle": True, "refund_window_hours": 24},
    )
    assert signed.status_code == 200, signed.text
    assert signed.json() == {
        "refund_after_lifecycle": True,
        "refund_window_hours": 24,
    }
    assert client.get(path).json() == {
        "refund_after_lifecycle": True,
        "refund_window_hours": 24,
    }

    for bad_hours in (-1, 8761):
        invalid = _signed_request(
            client,
            "PUT",
            path,
            json_body={
                "refund_after_lifecycle": True,
                "refund_window_hours": bad_hours,
            },
        )
        assert invalid.status_code == 422, invalid.text

    reset = _signed_request(
        client,
        "PUT",
        path,
        json_body={"refund_after_lifecycle": False, "refund_window_hours": 72},
    )
    assert reset.status_code == 200, reset.text
