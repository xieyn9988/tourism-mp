"""
分账 / 退款业务防护与流水查询测试
==================================

覆盖：
    1. 部分退款后仍可对“已核销”子项分账，未核销 / 已退款子项记 0；
    2. 没有已核销子项时拒绝分账（409），不产生流水；
    3. 分账成功后订单状态为 split，重复分账被拦截（409）；
    4. 已分账订单不可退款（409）；
    5. 正常退款会把未核销子项标记为 refunded，且退款后不可重复退款；
    6. 已退款子项不可再核销；
    7. 分账详情与分页查询接口可用，且需要 HMAC 签名；
    8. 旧库 orders.status / verify_records.verify_status 约束迁移，
       split_records.split_amount 列迁移与历史回填。

测试使用独立的临时 SQLite 数据库，不会污染 tourism.db。

运行方式：
    pytest -q test_split_guard.py
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

# 必须在导入 main 之前切换数据库，main.py 在模块加载时读取 DATABASE_URL。
TEST_DB = Path(tempfile.gettempdir()) / f"tourism_split_guard_{os.getpid()}.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402

import main as backend  # noqa: E402


def _split_path(order_id: int) -> str:
    return f"/api/order/{order_id}/split"


def _refund_path(order_id: int) -> str:
    return f"/api/order/{order_id}/refund"


def _signed_request(client: TestClient, method: str, path: str):
    """按完整 path（含 query string）生成签名并发起请求。"""
    return client.request(
        method,
        path,
        headers=backend.build_signature_headers(method, path),
    )


ITEM_MERCHANTS = {
    "门票": "青山景区票务",
    "咖啡": "云顶咖啡",
    "越野车": "极速越野",
}


def _create_order(
    status: backend.OrderStatus,
    *,
    verified: bool = False,
    item_name: str = "门票",
    total_amount: Decimal = Decimal("100.00"),
) -> int:
    """直接写库创建一张带核销记录的订单，避免依赖接口签名创建订单。"""
    return _create_order_with_items(
        status,
        [
            (
                item_name,
                backend.VerifyStatus.VERIFIED
                if verified
                else backend.VerifyStatus.UNVERIFIED,
            )
        ],
        total_amount=total_amount,
    )


def _create_order_with_items(
    status: backend.OrderStatus,
    items: list[tuple[str, backend.VerifyStatus]],
    *,
    total_amount: Decimal = Decimal("200.00"),
) -> int:
    """创建一张包含多个核销子项的订单，子项按项目名归属各自商户。"""
    with backend.SessionLocal() as db:
        merchants = {
            merchant.name: merchant
            for merchant in db.scalars(select(backend.Merchant)).all()
        }
        assert merchants, "seed_data 应已初始化商户"

        order = backend.Order(
            order_no=f"TEST-{status.value}-{uuid.uuid4().hex[:12]}",
            total_amount=total_amount,
            status=status,
        )
        db.add(order)
        db.flush()
        for name, verify_status in items:
            merchant_name = ITEM_MERCHANTS.get(name)
            merchant = merchants.get(merchant_name) if merchant_name else None
            if merchant is None:
                merchant = next(iter(merchants.values()))
            db.add(
                backend.VerifyRecord(
                    order_id=order.id,
                    merchant_id=merchant.id,
                    item_name=name,
                    verify_status=verify_status,
                )
            )
        db.commit()
        return int(order.id)


def _record_statuses(order_id: int) -> dict[str, str]:
    """读取订单下各子项的核销 / 退款状态，便于断言落库结果。"""
    with backend.SessionLocal() as db:
        records = db.scalars(
            select(backend.VerifyRecord).where(
                backend.VerifyRecord.order_id == order_id
            )
        ).all()
        return {record.item_name: record.verify_status.value for record in records}


def _count_split_records(order_id: int) -> int:
    """用原生 sqlite3 统计流水，表不存在时按 0 处理。"""
    conn = sqlite3.connect(TEST_DB, timeout=5)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM split_records WHERE order_id = ?", (order_id,)
        ).fetchone()
        return int(row[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _count_split_items(order_id: int) -> int:
    conn = sqlite3.connect(TEST_DB, timeout=5)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM split_record_items i "
            "JOIN split_records s ON s.id = i.split_record_id "
            "WHERE s.order_id = ?",
            (order_id,),
        ).fetchone()
        return int(row[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


@pytest.fixture(scope="module")
def client():
    with TestClient(backend.app) as test_client:
        yield test_client
    backend.engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()


def test_refunded_order_without_verified_items_cannot_split(client: TestClient):
    order_id = _create_order(
        backend.OrderStatus.REFUNDED,
        verified=False,
        item_name="门票",
    )

    response = _signed_request(client, "POST", _split_path(order_id))

    assert response.status_code == 409, response.text
    assert "没有已核销项目" in response.json()["detail"]
    assert _count_split_records(order_id) == 0


def test_partial_verification_splits_verified_items_only(client: TestClient):
    """门票 + 咖啡已核销、越野车未核销：只对 80 + 40 = 120 元分账。"""
    order_id = _create_order_with_items(
        backend.OrderStatus.PAID,
        [
            ("门票", backend.VerifyStatus.VERIFIED),
            ("咖啡", backend.VerifyStatus.VERIFIED),
            ("越野车", backend.VerifyStatus.UNVERIFIED),
        ],
    )

    response = _signed_request(client, "POST", _split_path(order_id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order_status"] == "split"
    assert body["total_amount"] == "200.00"
    assert body["split_amount"] == "120.00"
    assert body["platform_retention"] == "8.00"       # 80×5% + 40×10%
    assert body["merchant_income_total"] == "112.00"  # 76 + 36

    items = {item["item_name"]: item for item in body["items"]}
    assert items["门票"]["split_eligible"] is True
    assert items["门票"]["amount"] == "80.00"
    assert items["咖啡"]["split_eligible"] is True
    assert items["咖啡"]["amount"] == "40.00"
    assert items["越野车"]["split_eligible"] is False
    assert items["越野车"]["amount"] == "0.00"
    assert items["越野车"]["platform_commission"] == "0.00"
    assert items["越野车"]["merchant_income"] == "0.00"

    # 未核销的越野车商户不会出现在商户结算汇总中。
    assert {row["merchant_name"] for row in body["merchants"]} == {
        "青山景区票务",
        "云顶咖啡",
    }
    # 项目快照保留全部子项，但分账项目数只统计已核销子项。
    assert _count_split_items(order_id) == 3


def test_partial_refund_then_split_verified_items(client: TestClient):
    """业务主线：核销门票 + 咖啡，退款越野车后，仍可对 120 元分账。"""
    order_id = _create_order_with_items(
        backend.OrderStatus.PAID,
        [
            ("门票", backend.VerifyStatus.VERIFIED),
            ("咖啡", backend.VerifyStatus.VERIFIED),
            ("越野车", backend.VerifyStatus.UNVERIFIED),
        ],
    )

    refund = _signed_request(client, "POST", _refund_path(order_id))
    assert refund.status_code == 200, refund.text
    refund_body = refund.json()
    assert refund_body["refunded"] is True
    assert refund_body["refund_amount"] == "72.00"  # 80 - 10% 手续费
    assert _record_statuses(order_id) == {
        "门票": "verified",
        "咖啡": "verified",
        "越野车": "refunded",
    }

    split = _signed_request(client, "POST", _split_path(order_id))
    assert split.status_code == 200, split.text
    body = split.json()
    assert body["order_status"] == "split"
    assert body["split_amount"] == "120.00"
    assert body["platform_retention"] == "8.00"
    assert body["merchant_income_total"] == "112.00"

    items = {item["item_name"]: item for item in body["items"]}
    assert items["门票"]["amount"] == "80.00"
    assert items["咖啡"]["amount"] == "40.00"
    assert items["越野车"]["verify_status"] == "refunded"
    assert items["越野车"]["split_eligible"] is False
    assert items["越野车"]["amount"] == "0.00"

    # 已分账后不能重复分账，也不能再退款。
    assert (
        _signed_request(client, "POST", _split_path(order_id)).status_code == 409
    )
    assert (
        _signed_request(client, "POST", _refund_path(order_id)).status_code
        in (409, 400)
    )
    assert _record_statuses(order_id)["越野车"] == "refunded"


def test_split_sets_status_and_blocks_duplicate(client: TestClient):
    order_id = _create_order(backend.OrderStatus.PAID, verified=True)
    path = _split_path(order_id)

    first = _signed_request(client, "POST", path)
    assert first.status_code == 200, first.text
    assert first.json()["order_status"] == "split"
    assert first.json()["split_amount"] == "100.00"
    assert _count_split_records(order_id) == 1
    assert _count_split_items(order_id) == 1

    with backend.SessionLocal() as db:
        assert db.get(backend.Order, order_id).status == backend.OrderStatus.SPLIT

    second = _signed_request(client, "POST", path)
    assert second.status_code == 409, second.text
    assert "重复分账" in second.json()["detail"]
    assert _count_split_records(order_id) == 1


def test_split_order_cannot_refund(client: TestClient):
    order_id = _create_order(backend.OrderStatus.PAID, verified=True)
    split_path = _split_path(order_id)

    split = _signed_request(client, "POST", split_path)
    assert split.status_code == 200, split.text

    refund_path = _refund_path(order_id)
    refund = _signed_request(client, "POST", refund_path)
    assert refund.status_code == 409, refund.text
    assert refund.json()["detail"] == "订单已分账，不可退款"

    # 退款被拦截后订单仍保持已分账，不能出现“既分账又退款”的状态。
    with backend.SessionLocal() as db:
        assert db.get(backend.Order, order_id).status == backend.OrderStatus.SPLIT


def test_all_unverified_refund_marks_refunded_and_blocks_split(client: TestClient):
    order_id = _create_order(backend.OrderStatus.PAID, verified=False)
    refund_path = _refund_path(order_id)

    refund = _signed_request(client, "POST", refund_path)
    assert refund.status_code == 200, refund.text
    assert refund.json()["refunded"] is True

    with backend.SessionLocal() as db:
        assert db.get(backend.Order, order_id).status == backend.OrderStatus.REFUNDED
    # 退款成功后，未核销子项被标记为 refunded，防止后续误核销。
    assert _record_statuses(order_id) == {"门票": "refunded"}

    split = _signed_request(client, "POST", _split_path(order_id))
    assert split.status_code == 409, split.text
    assert "没有已核销项目" in split.json()["detail"]
    assert _count_split_records(order_id) == 0

    again = _signed_request(client, "POST", refund_path)
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "订单已退款，不能重复退款"


def test_refunded_item_cannot_be_verified(client: TestClient):
    """已退款子项必须拒绝核销，避免“退款后继续使用”的资损漏洞。"""
    order_id = _create_order_with_items(
        backend.OrderStatus.PAID,
        [("门票", backend.VerifyStatus.REFUNDED)],
        total_amount=Decimal("80.00"),
    )
    with backend.SessionLocal() as db:
        record = db.scalar(
            select(backend.VerifyRecord).where(
                backend.VerifyRecord.order_id == order_id
            )
        )
        record_id = int(record.id)

    response = client.post(f"/api/verify-records/{record_id}/verify")
    assert response.status_code == 409, response.text
    assert "已退款" in response.json()["detail"]


def test_query_split_record_and_list(client: TestClient):
    order_id = _create_order(backend.OrderStatus.PAID, verified=True)
    split = _signed_request(client, "POST", _split_path(order_id))
    assert split.status_code == 200, split.text

    detail_path = _split_path(order_id)
    detail = _signed_request(client, "GET", detail_path)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["split_record_id"] > 0
    assert body["order_id"] == order_id
    assert body["order_status"] == "split"
    assert len(body["items"]) == 1
    assert body["items"][0]["amount"] == "100.00"
    assert body["split_amount"] == "100.00"
    assert body["platform_retention"] == "5.00"
    assert body["merchant_income_total"] == "95.00"
    assert body["merchants"][0]["merchant_income"] == "95.00"

    list_path = f"/api/split-records?order_id={order_id}&page=1&page_size=10"
    listing = _signed_request(client, "GET", list_path)
    assert listing.status_code == 200, listing.text
    page = listing.json()
    assert page["total"] == 1
    assert page["page"] == 1
    assert page["items"][0]["split_record_id"] == body["split_record_id"]
    assert page["items"][0]["order_status"] == "split"

    # 订单列表可直接按 split 状态筛选，状态对调用方可见。
    orders = client.get("/api/orders?status=split")
    assert orders.status_code == 200, orders.text
    assert any(order["id"] == order_id for order in orders.json())

    # 资金流水查询同样受 HMAC 签名保护。
    assert client.get(detail_path).status_code == 403


OLD_ORDERS_DDL = """
CREATE TABLE orders (
    id INTEGER NOT NULL,
    order_no VARCHAR(32) NOT NULL,
    total_amount NUMERIC(10, 2) NOT NULL,
    status VARCHAR(16) DEFAULT 'unpaid' NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_order_total_amount_non_negative CHECK (total_amount >= 0),
    CONSTRAINT orderstatus CHECK (status IN ('unpaid', 'paid', 'refunded'))
);
"""


def test_order_status_migration(tmp_path: Path):
    """模拟旧库：迁移后允许 split，保留数据与外键，并可回填历史流水状态。"""
    db_path = tmp_path / "old_schema.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        OLD_ORDERS_DDL
        + """
        CREATE UNIQUE INDEX ix_orders_order_no ON orders (order_no);
        CREATE INDEX ix_orders_status ON orders (status);
        CREATE TABLE split_records (
            id INTEGER NOT NULL PRIMARY KEY,
            order_id INTEGER NOT NULL UNIQUE
                REFERENCES orders(id) ON DELETE CASCADE
        );
        INSERT INTO orders (id, order_no, total_amount, status)
            VALUES (1, 'OLD-1', 100, 'paid');
        INSERT INTO split_records (id, order_id) VALUES (1, 1);
        """
    )
    conn.close()

    old_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        backend._migrate_order_status_split(old_engine)
        backend._backfill_split_order_status(old_engine)

        with old_engine.begin() as db_conn:
            ddl = db_conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='orders'"
            ).scalar()
            assert ddl is not None and "'split'" in ddl
            assert (
                db_conn.exec_driver_sql(
                    "SELECT status FROM orders WHERE id = 1"
                ).scalar()
                == "split"
            )
            db_conn.exec_driver_sql(
                "INSERT INTO orders (order_no, total_amount, status) "
                "VALUES ('NEW-1', 10, 'split')"
            )
            fk_rows = list(
                db_conn.exec_driver_sql("PRAGMA foreign_key_list(split_records)")
            )
            assert any(row[2] == "orders" for row in fk_rows)

        # 迁移幂等：再次执行不应报错或重复建表。
        backend._migrate_order_status_split(old_engine)
    finally:
        old_engine.dispose()


OLD_VERIFY_RECORDS_DDL = """
CREATE TABLE verify_records (
    id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    merchant_id INTEGER NOT NULL,
    item_name VARCHAR(120) NOT NULL,
    verify_status VARCHAR(16) DEFAULT 'unverified' NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_verify_order_merchant_item
        UNIQUE (order_id, merchant_id, item_name),
    CONSTRAINT verifystatus
        CHECK (verify_status IN ('unverified', 'verified'))
);
"""


def test_verify_status_refunded_migration(tmp_path: Path):
    """旧库 verify_records 迁移后允许 refunded，保留数据与索引。"""
    db_path = tmp_path / "old_verify.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        OLD_VERIFY_RECORDS_DDL
        + """
        CREATE INDEX ix_verify_records_order_id ON verify_records (order_id);
        INSERT INTO verify_records
            (id, order_id, merchant_id, item_name, verify_status)
            VALUES (1, 1, 1, '门票', 'verified');
        """
    )
    conn.close()

    old_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        backend._migrate_verify_status_refunded(old_engine)

        with old_engine.begin() as db_conn:
            ddl = db_conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='verify_records'"
            ).scalar()
            assert ddl is not None and "'refunded'" in ddl
            assert (
                db_conn.exec_driver_sql(
                    "SELECT verify_status FROM verify_records WHERE id = 1"
                ).scalar()
                == "verified"
            )
            db_conn.exec_driver_sql(
                "INSERT INTO verify_records "
                "(order_id, merchant_id, item_name, verify_status) "
                "VALUES (2, 2, '咖啡', 'refunded')"
            )
            indexes = {
                row[0]
                for row in db_conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='verify_records'"
                )
            }
            assert "ix_verify_records_order_id" in indexes

        # 迁移幂等。
        backend._migrate_verify_status_refunded(old_engine)
    finally:
        old_engine.dispose()


def test_split_amount_column_migration(tmp_path: Path):
    """旧分账流水增加 split_amount，并按历史口径回填为订单总额。"""
    db_path = tmp_path / "old_split.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE split_records (
            id INTEGER NOT NULL PRIMARY KEY,
            order_id INTEGER NOT NULL UNIQUE,
            total_amount NUMERIC(10, 2) NOT NULL,
            platform_retention NUMERIC(10, 2) NOT NULL,
            merchant_income_total NUMERIC(10, 2) NOT NULL,
            item_count INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
        );
        INSERT INTO split_records
            (id, order_id, total_amount, platform_retention,
             merchant_income_total, item_count)
            VALUES (1, 1, 200.00, 8.00, 192.00, 3);
        """
    )
    conn.close()

    old_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        backend._migrate_split_amount_column(old_engine)

        with old_engine.begin() as db_conn:
            columns = {
                row[1]
                for row in db_conn.exec_driver_sql(
                    "PRAGMA table_info(split_records)"
                )
            }
            assert "split_amount" in columns
            assert (
                str(
                    db_conn.exec_driver_sql(
                        "SELECT split_amount FROM split_records WHERE id = 1"
                    ).scalar()
                )
                == "200"
            )

        # 迁移幂等。
        backend._migrate_split_amount_column(old_engine)
    finally:
        old_engine.dispose()
