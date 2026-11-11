"""
门票生命周期、入园/离园、园区配置测试。

运行：pytest -q test_lifecycle.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
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


OLD_ORDERS_DDL = """
CREATE TABLE orders (
    id INTEGER NOT NULL,
    order_no VARCHAR(32) NOT NULL,
    total_amount NUMERIC(10, 2) NOT NULL,
    status VARCHAR(16) DEFAULT 'paid' NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id)
);
"""


def test_order_lifecycle_columns_migration(tmp_path: Path):
    db_path = tmp_path / "old_lifecycle.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        OLD_ORDERS_DDL
        + """
        INSERT INTO orders (id, order_no, total_amount, status)
            VALUES (1, 'OLD-LIFE-1', 100, 'paid');
        """
    )
    conn.close()

    old_engine = backend.create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        backend._migrate_order_lifecycle_columns(old_engine)

        with old_engine.begin() as db_conn:
            columns = {
                row[1]
                for row in db_conn.exec_driver_sql("PRAGMA table_info(orders)")
            }
            assert {"entered_at", "exited_at"} <= columns
            assert (
                db_conn.exec_driver_sql(
                    "SELECT order_no FROM orders WHERE id = 1"
                ).scalar()
                == "OLD-LIFE-1"
            )

        backend._migrate_order_lifecycle_columns(old_engine)
        with old_engine.begin() as db_conn:
            versions = {
                row[0]
                for row in db_conn.exec_driver_sql(
                    "SELECT version FROM schema_migrations"
                )
            }
            assert backend.ORDER_LIFECYCLE_COLUMNS_MIGRATION in versions
    finally:
        old_engine.dispose()


def test_backfill_entry_time_from_verified_ticket(tmp_path: Path):
    db_path = tmp_path / "backfill_lifecycle.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        OLD_ORDERS_DDL
        + """
        ALTER TABLE orders ADD COLUMN entered_at DATETIME;
        ALTER TABLE orders ADD COLUMN exited_at DATETIME;
        CREATE TABLE verify_records (
            id INTEGER NOT NULL PRIMARY KEY,
            order_id INTEGER NOT NULL,
            item_name VARCHAR(120) NOT NULL,
            verify_status VARCHAR(16) NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO orders (id, order_no, total_amount, status)
            VALUES (1, 'OLD-LIFE-1', 100, 'paid'),
                   (2, 'OLD-LIFE-2', 100, 'paid'),
                   (3, 'OLD-LIFE-3', 100, 'paid');
        INSERT INTO verify_records
            (id, order_id, item_name, verify_status, updated_at)
            VALUES (1, 1, '门票', 'verified', '2026-11-10 02:00:00'),
                   (2, 2, '咖啡', 'verified', '2026-11-10 03:00:00'),
                   (3, 3, '门票', 'verified', '2026-11-10 04:00:00');
        UPDATE orders SET entered_at = '2026-11-09 01:00:00' WHERE id = 3;
        """
    )
    conn.close()

    old_engine = backend.create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        backend._backfill_order_entry_times(old_engine)

        with old_engine.begin() as db_conn:
            first = db_conn.exec_driver_sql(
                "SELECT entered_at FROM orders WHERE id = 1"
            ).scalar()
            second = db_conn.exec_driver_sql(
                "SELECT entered_at FROM orders WHERE id = 2"
            ).scalar()
            third = db_conn.exec_driver_sql(
                "SELECT entered_at FROM orders WHERE id = 3"
            ).scalar()
            assert str(first).startswith("2026-11-10 02:00:00")
            assert second is None
            assert str(third).startswith("2026-11-09 01:00:00")
    finally:
        old_engine.dispose()


ITEM_MERCHANTS = {
    "门票": "青山景区票务",
    "咖啡": "云顶咖啡",
    "越野车": "极速越野",
}


def _make_order(
    items: list[tuple[str, backend.VerifyStatus]],
    *,
    total_amount: Decimal = Decimal("200.00"),
    lifecycle: str = "completed",
) -> int:
    now = backend._utcnow_naive()
    if lifecycle == "completed":
        entered_at, exited_at = now - timedelta(days=10), now - timedelta(days=9)
    elif lifecycle == "not_started":
        entered_at, exited_at = None, None
    elif lifecycle == "active":
        entered_at, exited_at = now, None
    elif lifecycle == "post_leave":
        entered_at, exited_at = now - timedelta(hours=2), now - timedelta(hours=1)
    else:
        raise ValueError(f"未知 lifecycle: {lifecycle}")

    with backend.SessionLocal() as db:
        merchants = {
            merchant.name: merchant
            for merchant in db.scalars(backend.select(backend.Merchant)).all()
        }
        order = backend.Order(
            order_no=f"LIFE-{uuid.uuid4().hex[:12]}",
            total_amount=total_amount,
            status=backend.OrderStatus.PAID,
            entered_at=entered_at,
            exited_at=exited_at,
        )
        db.add(order)
        db.flush()
        for item_name, verify_status in items:
            db.add(
                backend.VerifyRecord(
                    order_id=order.id,
                    merchant_id=merchants[ITEM_MERCHANTS[item_name]].id,
                    item_name=item_name,
                    verify_status=verify_status,
                )
            )
        db.commit()
        return int(order.id)


def _set_park_settings(
    refund_after_lifecycle: bool,
    refund_window_hours: int = 72,
) -> None:
    with backend.SessionLocal() as db:
        settings = backend._load_park_settings(db)
        settings.refund_after_lifecycle = refund_after_lifecycle
        settings.refund_window_hours = refund_window_hours
        db.commit()


def _record_id(order_id: int, item_name: str) -> int:
    with backend.SessionLocal() as db:
        record = db.scalar(
            backend.select(backend.VerifyRecord).where(
                backend.VerifyRecord.order_id == order_id,
                backend.VerifyRecord.item_name == item_name,
            )
        )
        assert record is not None
        return int(record.id)


@pytest.fixture
def frozen_now(monkeypatch):
    fixed = datetime(2026, 11, 11, 4, 0, 0)
    monkeypatch.setattr(backend, "_utcnow_naive", lambda: fixed)
    return fixed


def test_ticket_verify_starts_lifecycle(client, frozen_now):
    order_id = _make_order(
        [
            ("门票", backend.VerifyStatus.UNVERIFIED),
            ("咖啡", backend.VerifyStatus.UNVERIFIED),
        ],
        total_amount=Decimal("120.00"),
        lifecycle="not_started",
    )
    ticket_id = _record_id(order_id, "门票")

    verify = client.post(
        f"/api/verify-records/{ticket_id}/verify", params={"post": "gate"}
    )
    assert verify.status_code == 200, verify.text

    with backend.SessionLocal() as db:
        order = db.get(backend.Order, order_id)
        assert order.entered_at == frozen_now
        snapshot = backend._order_lifecycle(
            order, backend._load_park_settings(db), now=frozen_now
        )
        assert snapshot.status == backend.LifecycleStatus.ACTIVE
        assert snapshot.can_verify is True
        assert snapshot.can_split is False


def test_ticket_verify_blocked_after_closing(client, monkeypatch):
    monkeypatch.setattr(
        backend, "_utcnow_naive", lambda: datetime(2026, 11, 11, 11, 0, 0)
    )
    order_id = _make_order(
        [("门票", backend.VerifyStatus.UNVERIFIED)],
        total_amount=Decimal("80.00"),
        lifecycle="not_started",
    )
    ticket_id = _record_id(order_id, "门票")

    verify = client.post(
        f"/api/verify-records/{ticket_id}/verify", params={"post": "gate"}
    )
    assert verify.status_code == 409, verify.text
    assert "闭园" in verify.json()["detail"]


def test_verify_blocked_after_visit_ends(client, frozen_now):
    _set_park_settings(False, 72)
    order_id = _make_order(
        [
            ("门票", backend.VerifyStatus.VERIFIED),
            ("咖啡", backend.VerifyStatus.UNVERIFIED),
        ],
        total_amount=Decimal("120.00"),
        lifecycle="completed",
    )
    coffee_id = _record_id(order_id, "咖啡")

    verify = client.post(
        f"/api/verify-records/{coffee_id}/verify", params={"post": "coffee"}
    )
    assert verify.status_code == 409, verify.text
    assert "重新购票" in verify.json()["detail"]
