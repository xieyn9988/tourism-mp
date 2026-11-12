# 商户营业时间与动态闭园校验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把写死的闭园时间改为商户级数据库配置，核销时按门票商户的 `close_time` 拦截，默认 `23:59:59`。

**Architecture:** `merchants` 增加 `open_time` / `close_time` 字符串列（`HH:MM` 或 `HH:MM:SS`），老 SQLite 库启动时 ALTER TABLE 补列。生命周期与核销统一通过「订单门票所属商户」解析闭园时刻（北京时间转 naive UTC），找不到时回退默认值。新增 PATCH 接口维护营业时间。

**Tech Stack:** Python 3.10+、FastAPI、SQLAlchemy 2.0、SQLite、Pydantic v2、pytest。

**Spec:** `docs/superpowers/specs/2026-11-11-merchant-business-hours-design.md`

## Global Constraints

- 默认营业时间：`open_time = '08:00'`，`close_time = '23:59:59'`（用户明确指定后者）。
- 字段类型 `String(8)`，接受 `HH:MM` 与 `HH:MM:SS`；非法值不得导致程序崩溃。
- 配置值按北京时间（UTC+8）解释，数据库存储仍是 naive UTC。
- 只有 `now >= 当日闭园时刻` 才拦截；400/409 均用 409，detail 必须含「闭园」。
- `open_time` 只存储/展示，不做开园拦截。
- 不修改 Gradio 运营后台 UI。
- 沿用项目现有迁移模式（`schema_migrations` + 幂等 ALTER TABLE）。

## Review Focus

- 数据库里的 `close_time` 被手工改坏（如 `abc`、`25:00`）时，核销应回退 `23:59:59` 而不是 500。
- 订单没有门票记录或门票商户已不存在时，应回退默认闭园时间，核销不崩。
- `23:59:59` 北京时间必须换算为当日 `15:59:59` UTC，不能按 `23:59` 丢秒或偏移 8 小时。
- 老库迁移必须保留历史商户数据且可重复启动；新库由 `create_all` 直接建列。
- PATCH 接口必须拒绝 `25:00` / `abc`，且未知商户返回 404。

---

### Task 1: Merchant 字段、时间解析与数据库迁移

**Files:**
- Modify: `main.py`（常量区约 250 行；`Merchant` 约 590 行；迁移区约 2498 行；`initialize_database` 约 2578 行）
- Test: `test_lifecycle.py`

**Interfaces:**
- Produces:
  - `DEFAULT_OPEN_TIME: str = "08:00"`
  - `DEFAULT_CLOSE_TIME: str = "23:59:59"`
  - `DEFAULT_CLOSE_TIME_VALUE: dt_time`（`23:59:59`）
  - `_parse_clock_time(raw: str | None) -> dt_time | None`
  - `Merchant.open_time: str` / `Merchant.close_time: str`
  - `MERCHANT_BUSINESS_HOURS_MIGRATION = "20261111_05_merchant_business_hours"`
  - `_migrate_merchant_business_hours(bind: Engine) -> None`

- [ ] **Step 1: 写失败测试**

在 `test_lifecycle.py` 末尾追加（文件顶部把 `from datetime import datetime, timedelta` 改为 `from datetime import datetime, time as dt_time, timedelta`）：

```python
OLD_MERCHANTS_DDL = """
CREATE TABLE merchants (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    split_rate NUMERIC(5, 4) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
);
"""


def test_parse_clock_time_supports_hh_mm_and_hh_mm_ss():
    assert backend._parse_clock_time("08:00") == dt_time(8, 0)
    assert backend._parse_clock_time("23:59:59") == dt_time(23, 59, 59)
    assert backend._parse_clock_time(" 18:30 ") == dt_time(18, 30)
    assert backend._parse_clock_time("abc") is None
    assert backend._parse_clock_time("25:00") is None
    assert backend._parse_clock_time(None) is None


def test_merchant_business_hours_migration(tmp_path: Path):
    db_path = tmp_path / "old_merchants.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        OLD_MERCHANTS_DDL
        + """
        INSERT INTO merchants (id, name, split_rate)
            VALUES (1, 'OLD-MERCHANT', 0.05);
        """
    )
    conn.close()

    old_engine = backend.create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        backend._migrate_merchant_business_hours(old_engine)
        with old_engine.begin() as db_conn:
            columns = {
                row[1]
                for row in db_conn.exec_driver_sql("PRAGMA table_info(merchants)")
            }
            assert {"open_time", "close_time"} <= columns
            row = db_conn.exec_driver_sql(
                "SELECT name, open_time, close_time FROM merchants WHERE id = 1"
            ).one()
            assert tuple(row) == ("OLD-MERCHANT", "08:00", "23:59:59")

        backend._migrate_merchant_business_hours(old_engine)
        with old_engine.begin() as db_conn:
            versions = {
                row[0]
                for row in db_conn.exec_driver_sql(
                    "SELECT version FROM schema_migrations"
                )
            }
            assert backend.MERCHANT_BUSINESS_HOURS_MIGRATION in versions
    finally:
        old_engine.dispose()


def test_seeded_merchants_have_default_business_hours(client):
    with backend.SessionLocal() as db:
        merchants = db.scalars(backend.select(backend.Merchant)).all()
    assert merchants
    for merchant in merchants:
        assert merchant.open_time == "08:00"
        assert merchant.close_time == "23:59:59"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest -q test_lifecycle.py -k "clock_time or business_hours or default_business_hours"`
Expected: FAIL（`AttributeError: module 'main' has no attribute '_parse_clock_time'` 或 `MERCHANT_BUSINESS_HOURS_MIGRATION`）。

- [ ] **Step 3: 实现常量与解析函数**

在 `main.py` 的 `PARK_TZ` 附近新增（暂时保留旧的 `PARK_CLOSE_TIME`，Task 2 删除）：

```python
DEFAULT_OPEN_TIME = "08:00"
DEFAULT_CLOSE_TIME = "23:59:59"
DEFAULT_CLOSE_TIME_VALUE = dt_time(23, 59, 59)


def _parse_clock_time(raw: str | None) -> dt_time | None:
    """解析 HH:MM 或 HH:MM:SS；格式/范围非法时返回 None。"""
    if not raw:
        return None
    parts = raw.strip().split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return dt_time(hour, minute, second)
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: 给 Merchant 加字段**

在 `Merchant.split_rate` 之后增加：

```python
open_time: Mapped[str] = mapped_column(
    String(8), nullable=False, default=DEFAULT_OPEN_TIME,
    server_default=DEFAULT_OPEN_TIME,
    comment="营业开始时间，北京时间 HH:MM 或 HH:MM:SS",
)
close_time: Mapped[str] = mapped_column(
    String(8), nullable=False, default=DEFAULT_CLOSE_TIME,
    server_default=DEFAULT_CLOSE_TIME,
    comment="闭园时间，北京时间 HH:MM 或 HH:MM:SS",
)
```

- [ ] **Step 5: 实现迁移并注册**

在 `_migrate_order_lifecycle_columns` 附近新增：

```python
MERCHANT_BUSINESS_HOURS_MIGRATION = "20261111_05_merchant_business_hours"


def _migrate_merchant_business_hours(bind: Engine) -> None:
    """为旧库 merchants 表补充 open_time / close_time（SQLite ALTER TABLE）。"""
    if bind.dialect.name != "sqlite":
        logger.info("非 SQLite 数据库，请使用 Alembic 执行 merchants 营业时间迁移")
        return
    with bind.begin() as conn:
        _ensure_schema_migrations_table(conn)
        has_merchants = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='merchants'"
        ).first()
        if has_merchants is None:
            return
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(merchants)")
        }
        if "open_time" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE merchants ADD COLUMN open_time VARCHAR(8) "
                "NOT NULL DEFAULT '08:00'"
            )
        if "close_time" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE merchants ADD COLUMN close_time VARCHAR(8) "
                "NOT NULL DEFAULT '23:59:59'"
            )
        _record_migration(conn, MERCHANT_BUSINESS_HOURS_MIGRATION)
        logger.info("merchants 表已补充营业时间字段 open_time / close_time")
```

在 `initialize_database()` 的 `_migrate_order_lifecycle_columns(engine)` 之后加入：

```python
_migrate_merchant_business_hours(engine)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest -q test_lifecycle.py -k "clock_time or business_hours or default_business_hours"`
Expected: PASS（3 passed）。

- [ ] **Step 7: 提交**

```bash
git add main.py test_lifecycle.py
git commit -m "feat(merchant): add open/close time fields and migration"
```

---

### Task 2: 核销与生命周期按商户闭园时间判断

**Files:**
- Modify: `main.py`（时间工具约 250-300 行；`evaluate_lifecycle` 约 302 行；`_order_lifecycle` 约 1323 行；`verify_record` 约 2795 行；其余 4 处 `_order_lifecycle` 调用点：2912、2943、3000、3257）
- Test: `test_lifecycle.py`、`test_verify_scope.py`

**Interfaces:**
- Consumes: Task 1 的 `_parse_clock_time`、`DEFAULT_CLOSE_TIME_VALUE`、`Merchant.close_time`。
- Produces:
  - `_today_closing_at(now: datetime, close_time: dt_time) -> datetime`
  - `evaluate_lifecycle(..., close_time: dt_time | None = None)`
  - `_order_lifecycle(order, settings, *, now=None, close_time=None)`
  - `_resolve_order_close_time(db: Session, order: Order) -> dt_time`
  - 核销接口闭园 detail：`f"景区已闭园（{_format_park_time(closing_at)}），今日无法核销，请明日再来。"`

- [ ] **Step 1: 更新旧测试并写新的失败测试**

在 `test_lifecycle.py`：

1. `_eval` 的 `params` 增加 `close_time=dt_time(18, 0)`（保留原有用例对 18:00 的假设）。
2. 新增辅助函数：

```python
def _set_ticket_merchant_close_time(value: str) -> str:
    with backend.SessionLocal() as db:
        merchant = db.scalar(
            backend.select(backend.Merchant).where(
                backend.Merchant.name == "青山景区票务"
            )
        )
        previous = merchant.close_time
        merchant.close_time = value
        db.commit()
        return previous
```

3. 修改 `test_ticket_verify_blocked_after_closing`：调用接口前执行
   `previous = _set_ticket_merchant_close_time("18:00")`，用 `try/finally` 恢复 `previous`。
4. 在 `test_verify_scope.py` 中也给 `_make_order` 涉及的商户保持默认值，无需改参数；若该文件有依赖 18:00 的用例，同样在用例内显式设置并在 `finally` 恢复。
5. 新增：

```python
def test_verify_allowed_before_merchant_close_time(client, monkeypatch):
    previous = _set_ticket_merchant_close_time("12:30")
    try:
        monkeypatch.setattr(
            backend, "_utcnow_naive", lambda: datetime(2026, 11, 11, 4, 0, 0)
        )  # 北京 12:00，未到 12:30
        order_id = _make_order(
            [("门票", backend.VerifyStatus.UNVERIFIED)],
            total_amount=Decimal("80.00"),
            lifecycle="not_started",
        )
        ticket_id = _record_id(order_id, "门票")
        verify = client.post(
            f"/api/verify-records/{ticket_id}/verify", params={"post": "gate"}
        )
        assert verify.status_code == 200, verify.text
    finally:
        _set_ticket_merchant_close_time(previous)


def test_verify_blocked_after_merchant_close_time(client, monkeypatch):
    previous = _set_ticket_merchant_close_time("12:00")
    try:
        monkeypatch.setattr(
            backend, "_utcnow_naive", lambda: datetime(2026, 11, 11, 4, 0, 1)
        )  # 北京 12:00:01，已过 12:00
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
    finally:
        _set_ticket_merchant_close_time(previous)


def test_invalid_merchant_close_time_falls_back_to_default(client, monkeypatch):
    previous = _set_ticket_merchant_close_time("not-a-time")
    try:
        monkeypatch.setattr(
            backend, "_utcnow_naive", lambda: datetime(2026, 11, 11, 4, 0, 0)
        )  # 北京 12:00，默认 23:59:59 未闭园
        order_id = _make_order(
            [("门票", backend.VerifyStatus.UNVERIFIED)],
            total_amount=Decimal("80.00"),
            lifecycle="not_started",
        )
        ticket_id = _record_id(order_id, "门票")
        verify = client.post(
            f"/api/verify-records/{ticket_id}/verify", params={"post": "gate"}
        )
        assert verify.status_code == 200, verify.text
    finally:
        _set_ticket_merchant_close_time(previous)


def test_close_time_falls_back_without_ticket_record(client, monkeypatch):
    monkeypatch.setattr(
        backend, "_utcnow_naive", lambda: datetime(2026, 11, 11, 4, 0, 0)
    )
    order_id = _make_order(
        [("咖啡", backend.VerifyStatus.UNVERIFIED)],
        total_amount=Decimal("40.00"),
        lifecycle="not_started",
    )
    coffee_id = _record_id(order_id, "咖啡")
    verify = client.post(
        f"/api/verify-records/{coffee_id}/verify", params={"post": "coffee"}
    )
    assert verify.status_code == 200, verify.text


def test_default_close_time_keeps_seconds_and_utc_offset():
    snapshot = _eval(
        entered_at=datetime(2026, 11, 11, 2, 0, 0),
        close_time=None,
    )
    # 北京 23:59:59 -> UTC 15:59:59，秒不能被截断。
    assert snapshot.leave_at == datetime(2026, 11, 11, 15, 59, 59)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest -q test_lifecycle.py -k "before_merchant_close or after_merchant_close or invalid_merchant_close"`
Expected: FAIL（核销仍按旧的 18:00 / 默认逻辑，或 `_resolve_order_close_time` 不存在）。

- [ ] **Step 3: 删除写死的闭园时间，改为参数传入**

在 `main.py` 中：

1. 删除 `DEFAULT_PARK_CLOSE_TIME`、`_parse_park_close_time`、`PARK_CLOSE_TIME`。
2. `_today_closing_at` 改为：

```python
def _today_closing_at(now: datetime, close_time: dt_time) -> datetime:
    """返回 now 所在北京日期的闭园时刻，转成 naive UTC。"""
    local = _to_park_local(now)
    closing_local = local.replace(
        hour=close_time.hour,
        minute=close_time.minute,
        second=close_time.second,
        microsecond=0,
    )
    return closing_local.astimezone(timezone.utc).replace(tzinfo=None)
```

3. `evaluate_lifecycle` 签名增加 `close_time: dt_time | None = None`；函数开头：

```python
closing_time = close_time or DEFAULT_CLOSE_TIME_VALUE
```

4. 把函数内两处 `_today_closing_at(...)` 改为 `_today_closing_at(now_value, closing_time)` 和 `_today_closing_at(entered_value, closing_time)`。

- [ ] **Step 4: 解析订单门票商户的闭园时间**

在 `_order_lifecycle` 之前新增：

```python
def _resolve_order_close_time(db: Session, order: Order) -> dt_time:
    """整单闭园时间取订单门票所属商户；缺失/非法时回退默认 23:59:59。"""
    ticket = next(
        (record for record in order.verify_records if "门票" in record.item_name),
        None,
    )
    merchant = db.get(Merchant, ticket.merchant_id) if ticket is not None else None
    parsed = _parse_clock_time(merchant.close_time) if merchant is not None else None
    return parsed or DEFAULT_CLOSE_TIME_VALUE
```

给 `_order_lifecycle` 增加关键字参数并透传：

```python
def _order_lifecycle(
    order: Order,
    settings: ParkSettings,
    *,
    now: datetime | None = None,
    close_time: dt_time | None = None,
) -> LifecycleSnapshot:
    ...
    return evaluate_lifecycle(
        ...
        now=now,
        close_time=close_time,
    )
```

- [ ] **Step 5: 所有调用点传入动态闭园时间**

把 5 处 `_order_lifecycle(order, settings)` 改为：

```python
_order_lifecycle(
    order,
    settings,
    close_time=_resolve_order_close_time(db, order),
)
```

涉及：`verify_record`（约 2880）、`get_order_lifecycle`（约 2912）、`exit`（约 2943）、分账（约 3000）、退款（约 3257）。`test_lifecycle.py:438` 的调用不传 `close_time` 也可保持原断言（默认 23:59:59 时 12:00 仍为 ACTIVE）。

- [ ] **Step 6: 在核销接口加重闭园检查**

在 `verify_record` 中，订单状态校验（`order.status != OrderStatus.PAID` 之后）加入：

```python
now_value = _utcnow_naive()
closing_at = _today_closing_at(
    now_value, _resolve_order_close_time(db, order)
)
if now_value >= closing_at:
    raise HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail=(
            f"景区已闭园（{_format_park_time(closing_at)}），"
            "今日无法核销，请明日再来。"
        ),
    )
```

- [ ] **Step 7: 运行相关测试确认通过**

Run: `pytest -q test_lifecycle.py test_verify_scope.py test_scan_ui.py`
Expected: PASS。若 `test_scan_ui.py` 的 18:00 文案断言失败，只需确认该用例是伪造响应（`_fake_verify_conflict`），不应改动实现。

- [ ] **Step 8: 提交**

```bash
git add main.py test_lifecycle.py test_verify_scope.py
git commit -m "feat(lifecycle): use per-merchant close time for verification"
```

---

### Task 3: 营业时间查询/修改接口与文档

**Files:**
- Modify: `main.py`（`MerchantOut` 约 897 行；Pydantic 输入模型约 942 行；商户路由约 2719 行；模块头说明约 82 行）
- Modify: `docs/superpowers/specs/2026-11-11-ticket-lifecycle-design.md`（闭园时间描述）
- Test: `test_lifecycle.py` 或新建 `test_merchant_hours.py`

**Interfaces:**
- Consumes: Task 1 的字段与 `_parse_clock_time`；Task 2 的核销拦截。
- Produces:
  - `MerchantOut.open_time: str` / `MerchantOut.close_time: str`
  - `MerchantHoursUpdate(BaseModel)`：`open_time: str`、`close_time: str`
  - `PATCH /api/merchants/{merchant_id}/business-hours` → `MerchantOut`

- [ ] **Step 1: 写失败测试**

新建 `test_merchant_hours.py`：

```python
"""商户营业时间查询 / 修改测试。运行：pytest -q test_merchant_hours.py"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main as backend


@pytest.fixture(scope="module")
def client():
    with TestClient(backend.app) as test_client:
        yield test_client


def _ticket_merchant_id(client) -> int:
    merchants = client.get("/api/merchants").json()
    return next(m["id"] for m in merchants if m["name"] == "青山景区票务")


def test_merchant_list_exposes_business_hours(client):
    merchant = next(
        m for m in client.get("/api/merchants").json()
        if m["name"] == "青山景区票务"
    )
    assert merchant["open_time"] == "08:00"
    assert merchant["close_time"] == "23:59:59"


def test_patch_business_hours_updates_merchant(client):
    merchant_id = _ticket_merchant_id(client)
    original = client.get("/api/merchants").json()
    previous = next(m for m in original if m["id"] == merchant_id)
    try:
        response = client.patch(
            f"/api/merchants/{merchant_id}/business-hours",
            json={"open_time": "07:30", "close_time": "21:00"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["open_time"] == "07:30"
        assert response.json()["close_time"] == "21:00"
        refreshed = next(
            m for m in client.get("/api/merchants").json()
            if m["id"] == merchant_id
        )
        assert refreshed["close_time"] == "21:00"
    finally:
        client.patch(
            f"/api/merchants/{merchant_id}/business-hours",
            json={
                "open_time": previous["open_time"],
                "close_time": previous["close_time"],
            },
        )


@pytest.mark.parametrize("value", ["25:00", "abc", "12:60"])
def test_patch_business_hours_rejects_invalid_time(client, value):
    merchant_id = _ticket_merchant_id(client)
    response = client.patch(
        f"/api/merchants/{merchant_id}/business-hours",
        json={"open_time": value, "close_time": "22:00"},
    )
    assert response.status_code == 422, response.text


def test_patch_business_hours_unknown_merchant(client):
    response = client.patch(
        "/api/merchants/999999/business-hours",
        json={"open_time": "08:00", "close_time": "22:00"},
    )
    assert response.status_code == 404, response.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest -q test_merchant_hours.py`
Expected: FAIL（`/api/merchants` 响应缺字段、PATCH 返回 405）。

- [ ] **Step 3: 扩展 MerchantOut 并新增输入模型**

`MerchantOut` 增加：

```python
open_time: str
close_time: str
```

在 Pydantic 模型区新增（并在顶部 `from pydantic import ...` 中补充 `field_validator`）：

```python
class MerchantHoursUpdate(BaseModel):
    open_time: str = Field(max_length=8)
    close_time: str = Field(max_length=8)

    @field_validator("open_time", "close_time")
    @classmethod
    def _valid_clock(cls, value: str) -> str:
        if _parse_clock_time(value) is None:
            raise ValueError("时间格式应为 HH:MM 或 HH:MM:SS")
        return value.strip()
```

- [ ] **Step 4: 新增 PATCH 接口**

在 `list_merchants` 之后：

```python
@app.patch(
    "/api/merchants/{merchant_id}/business-hours",
    response_model=MerchantOut,
    tags=["商户"],
    summary="更新商户营业时间",
)
def update_merchant_business_hours(
    merchant_id: int,
    payload: MerchantHoursUpdate,
    db: DbSession,
    _: RequireSignature,
):
    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="商户不存在",
        )
    merchant.open_time = payload.open_time
    merchant.close_time = payload.close_time
    db.commit()
    db.refresh(merchant)
    return merchant
```

- [ ] **Step 5: 更新文档**

1. `main.py` 第 82 行附近改为：
   `- 出口扫码记录 orders.exited_at；闭园时间取订单门票商户的 merchants.close_time（默认 23:59:59）；`
2. `docs/superpowers/specs/2026-11-11-ticket-lifecycle-design.md`：
   - 第 25 行「闭园时间」改为「订单门票商户的 `merchants.close_time`（默认 `23:59:59`）」；
   - 第 202 行非目标改为「闭园时间由商户表配置，通过 `PATCH /api/merchants/{id}/business-hours` 修改」。

- [ ] **Step 6: 运行全量测试**

Run: `pytest -q`
Expected: PASS（全部通过；允许既有测试数量增长）。

- [ ] **Step 7: 提交**

```bash
git add main.py test_merchant_hours.py docs/superpowers/specs/2026-11-11-ticket-lifecycle-design.md
git commit -m "feat(merchant): expose and update business hours"
```

---

## 验收清单

- [ ] 默认商户 `open_time='08:00'`、`close_time='23:59:59'`，23:59:59 前核销正常。
- [ ] 把门票商户 `close_time` 改成已过去的时间（如 `12:00`）后，核销 409 且 detail 含「闭园」。
- [ ] 老 `tourism.db` 启动后自动补两列，历史数据保留。
- [ ] `PATCH /api/merchants/{id}/business-hours` 可修改并立即生效。
- [ ] `pytest -q` 全绿。
