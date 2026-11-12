"""
核销作用域与演示环境重置测试。

覆盖两类回归：
  1. 核销状态只能来自本次订单自己的 VerifyRecord，不能被其他订单污染；
  2. 重置演示环境必须物理清空所有订单相关表，新订单的子项全部全新。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import main as backend

ITEM_MERCHANTS = {
    "门票": "青山景区票务",
    "咖啡": "云顶咖啡",
    "越野车": "极速越野",
}
FIXED_NOW = datetime(2026, 11, 11, 4, 0, 0)  # 北京时间 12:00，未闭园


@pytest.fixture(scope="module")
def client():
    with TestClient(backend.app) as test_client:
        yield test_client


def _make_order(
    items: list[tuple[str, backend.VerifyStatus]],
    *,
    entered_at: datetime | None = None,
) -> tuple[int, dict[str, int]]:
    with backend.SessionLocal() as db:
        merchants = {
            merchant.name: merchant
            for merchant in db.scalars(backend.select(backend.Merchant)).all()
        }
        order = backend.Order(
            order_no=f"SCOPE-{uuid.uuid4().hex[:12]}",
            total_amount=Decimal("200.00"),
            status=backend.OrderStatus.PAID,
            entered_at=entered_at,
        )
        db.add(order)
        db.flush()
        record_ids: dict[str, int] = {}
        for item_name, verify_status in items:
            record = backend.VerifyRecord(
                order_id=order.id,
                merchant_id=merchants[ITEM_MERCHANTS[item_name]].id,
                item_name=item_name,
                verify_status=verify_status,
            )
            db.add(record)
            db.flush()
            record_ids[item_name] = int(record.id)
        db.commit()
        return int(order.id), record_ids


def _status_of(order_id: int) -> dict[str, str]:
    with backend.SessionLocal() as db:
        records = db.scalars(
            backend.select(backend.VerifyRecord).where(
                backend.VerifyRecord.order_id == order_id
            )
        ).all()
    return {record.item_name: record.verify_status.value for record in records}


def test_coffee_verify_checks_only_its_own_order_ticket(client, monkeypatch):
    """
    订单 A 的门票已核销，不能放行订单 B 的咖啡核销。

    这正是“同一订单提示门票未核销 / 已核销”的数据污染来源：状态必须按
    当前订单过滤，而不是按全表或别的订单推断。
    """
    monkeypatch.setattr(backend, "_utcnow_naive", lambda: FIXED_NOW)
    order_a, _ = _make_order(
        [
            ("门票", backend.VerifyStatus.VERIFIED),
            ("咖啡", backend.VerifyStatus.UNVERIFIED),
        ],
        entered_at=FIXED_NOW - timedelta(hours=1),
    )
    order_b, records_b = _make_order(
        [
            ("门票", backend.VerifyStatus.UNVERIFIED),
            ("咖啡", backend.VerifyStatus.UNVERIFIED),
        ]
    )

    blocked = client.post(
        f"/api/verify-records/{records_b['门票']}/verify",
        params={"post": "coffee", "order_id": order_b},
    )
    assert blocked.status_code == 409, blocked.text
    assert "门票未核销" in blocked.json()["detail"]

    gate = client.post(
        f"/api/verify-records/{records_b['门票']}/verify",
        params={"post": "gate", "order_id": order_b},
    )
    assert gate.status_code == 200, gate.text

    assert _status_of(order_b) == {"门票": "verified", "咖啡": "unverified"}
    assert _status_of(order_a) == {"门票": "verified", "咖啡": "unverified"}


def test_verify_rejects_seed_record_from_another_order(client, monkeypatch):
    """显式传入 order_id 时，票号必须属于该订单，避免串单核销。"""
    monkeypatch.setattr(backend, "_utcnow_naive", lambda: FIXED_NOW)
    order_a, records_a = _make_order(
        [("门票", backend.VerifyStatus.UNVERIFIED)]
    )
    _, records_b = _make_order(
        [("门票", backend.VerifyStatus.UNVERIFIED)]
    )

    mismatch = client.post(
        f"/api/verify-records/{records_b['门票']}/verify",
        params={"post": "gate", "order_id": order_a},
    )
    assert mismatch.status_code == 409, mismatch.text
    assert "不匹配" in mismatch.json()["detail"]
    assert _status_of(order_a) == {"门票": "unverified"}


def test_reset_physically_clears_all_order_scoped_tables(client):
    """重置后 Order / VerifyRecord / SplitRecord / SplitItemRecord 均无历史残留。"""
    order_id, record_ids = _make_order(
        [
            ("门票", backend.VerifyStatus.VERIFIED),
            ("咖啡", backend.VerifyStatus.UNVERIFIED),
        ],
        entered_at=FIXED_NOW - timedelta(hours=2),
    )
    with backend.SessionLocal() as db:
        split = backend.SplitRecord(
            order_id=order_id,
            total_amount=Decimal("120.00"),
            split_amount=Decimal("80.00"),
            platform_retention=Decimal("4.00"),
            merchant_income_total=Decimal("76.00"),
            item_count=1,
        )
        db.add(split)
        db.flush()
        db.add(
            backend.SplitItemRecord(
                split_record_id=split.id,
                verify_record_id=record_ids["门票"],
                item_name="门票",
                merchant_id=1,
                merchant_name="青山景区票务",
                verify_status="verified",
                amount=Decimal("80.00"),
                split_rate=Decimal("0.05"),
                platform_commission=Decimal("4.00"),
                merchant_income=Decimal("76.00"),
            )
        )
        db.commit()

    headers = backend.build_signature_headers(
        "POST", "/api/demo/reset", body=b""
    )
    response = client.post("/api/demo/reset", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()

    with backend.SessionLocal() as db:
        assert db.scalar(
            backend.select(backend.func.count()).select_from(backend.Order)
        ) == 1
        assert db.scalar(
            backend.select(backend.func.count()).select_from(
                backend.VerifyRecord
            )
        ) == 3
        assert db.scalar(
            backend.select(backend.func.count()).select_from(
                backend.SplitRecord
            )
        ) == 0
        assert db.scalar(
            backend.select(backend.func.count()).select_from(
                backend.SplitItemRecord
            )
        ) == 0

        order = db.get(backend.Order, payload["order_id"])
        assert order is not None
        assert order.order_no == payload["order_no"]
        records = order.verify_records
        assert {record.item_name for record in records} == {
            "门票",
            "咖啡",
            "越野车",
        }
        assert all(
            record.verify_status is backend.VerifyStatus.UNVERIFIED
            for record in records
        )
        assert all(record.order_id == order.id for record in records)
