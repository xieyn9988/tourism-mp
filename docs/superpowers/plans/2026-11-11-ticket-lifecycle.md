# 门票使用生命周期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现“门票核销开始、离园/闭园结束”的使用生命周期，并让分账、退款、核销严格受生命周期与园区退款配置约束。

**Architecture:** 生命周期为订单级派生状态，不落库：`Order.entered_at` / `Order.exited_at` 记录入园与出口扫码，`ParkSettings` 单行表保存运营配置，`evaluate_lifecycle()` 纯函数根据订单字段、子项终态、配置与当前时间算出 `not_applicable / not_started / active / completed`。分账、退款、核销、离园四个入口共用这一判定；前端增加离园岗位、生命周期展示与运营配置面板。

**Tech Stack:** FastAPI、SQLAlchemy 2.0、SQLite、Pydantic v2、Gradio、pytest、requests。

**Spec:** `docs/superpowers/specs/2026-11-11-ticket-lifecycle-design.md`（执行前先读，所有行为以该文档为准）

## Global Constraints

- 时区固定为 UTC+8（北京时间）；数据库存储 naive UTC；闭园时间来自环境变量 `PARK_CLOSE_TIME`，默认 `18:00`。
- 默认园区配置：`refund_after_lifecycle = false`，`refund_window_hours = 72`；小时数允许 0–8760。
- `leave_at = min(exited_at, closing_at(entered_at))`；`closing_at` 取入园当天的闭园时间。
- 生命周期结束条件（满足任一）：
  1. 全部子项为 `verified` 或 `refunded`；
  2. 存在 `unverified` 子项、允许离园后退款、且 `now >= leave_at + refund_window_hours`；
  3. 存在 `unverified` 子项、不允许离园后退款、且 `now >= leave_at`。
- 分账只在 `completed` 或 `not_applicable` 时允许；退款只在 `can_refund` 为 true 时允许；核销只在 `can_verify` 为 true 时允许。
- 生命周期结束后再次核销必须返回 409，文案包含“本次游园已结束，如需再次入园请重新购票”。
- 所有新增资金/配置写接口继续使用现有 HMAC-SHA256 签名；离园与只读生命周期/配置接口不签名。
- 本项目不是 git 仓库：所有任务省略 `git add/commit` 步骤，以“测试通过”作为检查点，不要执行 git 命令。
- 测试使用临时 SQLite 数据库，禁止修改 `tourism.db`。

## Review Focus

- **时区换算**：闭园 18:00 必须换算为 UTC 10:00；错误地按本地时间当 UTC 会让 `leave_at` 偏移 8 小时。Task 1 的边界测试锁定 `10:00 UTC`。
- **边界相等**：`now == leave_at`、`now == refund_deadline` 必须算“已结束”，不能用 `>`。Task 1 在截止时刻精确断言 `completed`。
- **离园后窗口内仍可退款但不可核销**：`active + post_leave_refund_window` 时 `can_refund=True`、`can_verify=False`。Task 6 在离园后先验核销被拒、再退款成功。
- **全部子项终态立即结束**：退款掉最后一个未核销子项后，无需等待离园或窗口，分账立即可执行。Task 6 的“离园后退款→立即分账”覆盖。
- **签名覆盖原始请求体**：`PUT /api/park-settings` 必须对实际发送的 JSON 字节签名；测试客户端重新序列化会破坏签名。Task 2 用 `content=` 发送原始字节，Task 7 断言 header 与 body 一致。

---

### Task 1: 时间工具与生命周期纯函数

**Files:**
- Modify: `main.py`（导入、时间配置、枚举、纯函数）
- Create: `conftest.py`
- Modify: `test_split_guard.py`（数据库路径改为复用 conftest）
- Create: `test_lifecycle.py`

**Interfaces:**
- Produces:
  - `PARK_TZ: timezone`、`PARK_CLOSE_TIME: datetime.time`、`DEFAULT_PARK_CLOSE_TIME = "18:00"`
  - `_utcnow_naive() -> datetime`
  - `_today_closing_at(now: datetime) -> datetime`（返回 naive UTC）
  - `_format_park_time(value: datetime) -> str`（UTC+8，`%Y-%m-%d %H:%M`）
  - `LifecycleStatus(str, PyEnum)`：`NOT_APPLICABLE="not_applicable"`、`NOT_STARTED="not_started"`、`ACTIVE="active"`、`COMPLETED="completed"`
  - `LifecycleReason(str, PyEnum)`：`NOT_APPLICABLE`、`NOT_ENTERED`、`PARK_CLOSED`、`IN_PARK`、`POST_LEAVE_REFUND_WINDOW`、`ALL_ITEMS_SETTLED`、`REFUND_WINDOW_EXPIRED`、`POST_LEAVE_REFUND_DISABLED`（值均为对应 snake_case）
  - `@dataclass(frozen=True) LifecycleSnapshot`，字段：
    `status, reason, entered_at, exited_at, leave_at, refund_deadline, can_verify, can_refund, can_split, message`
  - `evaluate_lifecycle(*, entered_at: datetime | None, exited_at: datetime | None, has_ticket: bool, has_pending_items: bool, refund_after_lifecycle: bool, refund_window_hours: int, now: datetime | None = None) -> LifecycleSnapshot`

- [ ] **Step 1: 创建共享测试数据库配置**

`conftest.py`：

```python
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
```

- [ ] **Step 2: 让 `test_split_guard.py` 复用 conftest**

把文件顶部的数据库初始化（`import tempfile`、`TEST_DB = Path(tempfile.gettempdir()) / ...`、`os.environ["DATABASE_URL"] = ...`）替换为：

```python
from pathlib import Path

TEST_DB = Path(os.environ["DATABASE_URL"].removeprefix("sqlite:///"))
```

删除 `import tempfile`。把 `client` fixture 末尾的 `backend.engine.dispose()` 与 `TEST_DB.unlink()` 删除（清理已由 conftest 负责）：

```python
@pytest.fixture(scope="module")
def client():
    with TestClient(backend.app) as test_client:
        yield test_client
```

- [ ] **Step 3: 写纯函数失败测试**

`test_lifecycle.py`：

```python
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
```

- [ ] **Step 4: 运行测试确认失败**

Run: `pytest -q test_lifecycle.py`
Expected: FAIL，`AttributeError: module 'main' has no attribute 'evaluate_lifecycle'`

- [ ] **Step 5: 实现时间工具与生命周期纯函数**

在 `main.py` 的导入中增加 `time as dt_time, timedelta`：

```python
from datetime import datetime, time as dt_time, timedelta, timezone
```

在数据库配置区（`SessionLocal` 之后、`Base` 之前）加入：

```python
PARK_TZ = timezone(timedelta(hours=8))
DEFAULT_PARK_CLOSE_TIME = "18:00"


def _parse_park_close_time(raw: str) -> dt_time:
    try:
        hour_text, minute_text = raw.strip().split(":", 1)
        parsed = dt_time(int(hour_text), int(minute_text))
    except (AttributeError, TypeError, ValueError):
        logger.warning("PARK_CLOSE_TIME 格式无效：%r，回退到 %s", raw, DEFAULT_PARK_CLOSE_TIME)
        return dt_time(18, 0)
    return parsed


PARK_CLOSE_TIME = _parse_park_close_time(
    os.getenv("PARK_CLOSE_TIME", DEFAULT_PARK_CLOSE_TIME)
)


def _utcnow_naive() -> datetime:
    """当前 UTC 时间，去掉 tzinfo，与 SQLite 中存储的 naive UTC 一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _to_park_local(value: datetime) -> datetime:
    return _as_utc_naive(value).replace(tzinfo=timezone.utc).astimezone(PARK_TZ)


def _today_closing_at(now: datetime) -> datetime:
    """返回 now 所在北京日期的闭园时刻，转成 naive UTC。"""
    local = _to_park_local(now)
    closing_local = local.replace(
        hour=PARK_CLOSE_TIME.hour,
        minute=PARK_CLOSE_TIME.minute,
        second=0,
        microsecond=0,
    )
    return closing_local.astimezone(timezone.utc).replace(tzinfo=None)


def _format_park_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return _to_park_local(value).strftime("%Y-%m-%d %H:%M")
```

在枚举区（`VerifyPost` 之后）加入 `LifecycleStatus`、`LifecycleReason`。在 `VerifyPost` 之后、ORM 模型区之前加入 `LifecycleSnapshot` dataclass，并 import `dataclass`：

```python
from dataclasses import dataclass
```

在 `_format_park_time` 之后实现 `evaluate_lifecycle`。逻辑按下表，所有比较都用 `>=` 判定结束：

| 条件（按顺序） | status | reason | can_verify | can_refund | can_split | 其他 |
| --- | --- | --- | --- | --- | --- | --- |
| `not has_ticket` | `not_applicable` | `not_applicable` | True | `has_pending_items` | True | `leave_at=None` |
| `entered_at is None` 且 `now < today_closing` | `not_started` | `not_entered` | True | `has_pending_items` | False | `leave_at=None` |
| `entered_at is None` 且 `now >= today_closing` | `not_started` | `park_closed` | False | `has_pending_items` | False | `leave_at=None` |
| `not has_pending_items` | `completed` | `all_items_settled` | False | False | True | `leave_at=min(...)` |
| 允许离园后退款、`now >= deadline` | `completed` | `refund_window_expired` | False | False | True | `refund_deadline=leave_at+window` |
| 允许离园后退款、`now < deadline`、`now >= leave_at` | `active` | `post_leave_refund_window` | False | True | False | `refund_deadline=...` |
| 允许离园后退款、`now < leave_at` | `active` | `in_park` | True | True | False | `refund_deadline=...` |
| 不允许离园后退款、`now >= leave_at` | `completed` | `post_leave_refund_disabled` | False | False | True | `refund_deadline=None` |
| 不允许离园后退款、`now < leave_at` | `active` | `in_park` | True | True | False | `refund_deadline=None` |

`leave_at` 计算：

```python
closing_at = _today_closing_at(entered_at)
leave_at = closing_at if exited_at is None else min(_as_utc_naive(exited_at), closing_at)
```

固定文案（测试与前端会依赖其中的关键词）：

- `not_applicable`：`"订单不含门票，不适用游园生命周期，可直接按已核销项目分账。"`
- `not_entered`：`"门票尚未核销，生命周期未开始；核销门票后开始计算游园时间。"`
- `park_closed`：`f"景区已闭园（{_format_park_time(closing)}），今日无法检票入园，请明日再来。"`
- `in_park`：`"游客在园内，生命周期进行中；离园或全部子项终态后方可分账。"`
- `post_leave_refund_window`：`f"游客已离园或景区已闭园；未核销项目可退款至 {_format_park_time(deadline)}，退款窗口结束或全部子项终态后方可分账。"`
- `all_items_settled`：`"生命周期已结束：全部子项均已核销或已退款，可以进行分账。"`
- `refund_window_expired`：`f"生命周期已结束：离园后退款窗口已于 {_format_park_time(deadline)} 到期。"`
- `post_leave_refund_disabled`：`"生命周期已结束：游客已离园或景区已闭园，且园区配置为离园后不再退款。"`

`now` 缺省时取 `_utcnow_naive()`；传入值先经 `_as_utc_naive()`。

- [ ] **Step 6: 运行新测试与旧测试**

Run: `pytest -q test_lifecycle.py test_split_guard.py`
Expected: 新增纯函数测试全部 PASS；`test_split_guard.py` 仍全部 PASS（此任务未改业务接口）。

---

### Task 2: 园区配置表、读取与签名更新接口

**Files:**
- Modify: `main.py`（模型、schema、seed、接口）
- Modify: `test_lifecycle.py`（新增配置测试）

**Interfaces:**
- Consumes: `Signature`（现有 `RequireSignature`）、`DbSession`
- Produces:
  - `PARK_SETTINGS_ID = 1`、`DEFAULT_REFUND_WINDOW_HOURS = 72`
  - `ParkSettings` ORM：`id, refund_after_lifecycle, refund_window_hours`
  - `_load_park_settings(db: Session) -> ParkSettings`
  - `ParkSettingsOut`、`ParkSettingsUpdate`
  - `GET /api/park-settings`、`PUT /api/park-settings`

- [ ] **Step 1: 写失败测试**

在 `test_lifecycle.py` 追加（在文件顶部补 `import json`；`datetime` 已在 Task 1 导入，本任务不需要 `timedelta`）：

```python
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
```

同文件加入 `client` fixture（模块级）：

```python
@pytest.fixture(scope="module")
def client():
    with TestClient(backend.app) as test_client:
        yield test_client
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest -q test_lifecycle.py::test_park_settings_update_requires_signature_and_validates`
Expected: FAIL，404 或 `AttributeError`（接口/模型尚不存在）

- [ ] **Step 3: 实现模型、schema、seed、接口**

在 `VerifyPost` 枚举之后、`Merchant` 之前加入：

```python
PARK_SETTINGS_ID = 1
DEFAULT_REFUND_WINDOW_HOURS = 72


class ParkSettings(TimestampMixin, Base):
    """园区级运营配置（单行，id 固定为 1）。"""

    __tablename__ = "park_settings"
    __table_args__ = (
        CheckConstraint(
            "refund_window_hours >= 0 AND refund_window_hours <= 8760",
            name="ck_park_settings_refund_window_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    refund_after_lifecycle: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
        comment="离园/闭园后是否仍允许未核销子项退款",
    )
    refund_window_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_REFUND_WINDOW_HOURS,
        server_default=str(DEFAULT_REFUND_WINDOW_HOURS),
        comment="离园后退款窗口小时数",
    )
```

在 `OrderOut` 附近加入：

```python
class ParkSettingsOut(BaseModel):
    refund_after_lifecycle: bool
    refund_window_hours: int


class ParkSettingsUpdate(BaseModel):
    refund_after_lifecycle: bool
    refund_window_hours: int = Field(ge=0, le=8760)
```

在 `_load_order_with_records` 附近加入：

```python
def _load_park_settings(db: Session) -> ParkSettings:
    settings = db.get(ParkSettings, PARK_SETTINGS_ID)
    if settings is not None:
        return settings

    settings = ParkSettings(id=PARK_SETTINGS_ID)
    db.add(settings)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        settings = db.get(ParkSettings, PARK_SETTINGS_ID)
        if settings is None:
            raise
    return settings


def _to_park_settings_out(settings: ParkSettings) -> ParkSettingsOut:
    return ParkSettingsOut(
        refund_after_lifecycle=bool(settings.refund_after_lifecycle),
        refund_window_hours=int(settings.refund_window_hours),
    )
```

在 `seed_data()` 的 `db.commit()` 之前加入：

```python
if db.get(ParkSettings, PARK_SETTINGS_ID) is None:
    db.add(ParkSettings(id=PARK_SETTINGS_ID))
```

在 API 区 `health` 之后加入：

```python
@app.get(
    "/api/park-settings",
    response_model=ParkSettingsOut,
    tags=["园区配置"],
    summary="读取园区退款配置",
)
def get_park_settings(db: DbSession) -> ParkSettingsOut:
    return _to_park_settings_out(_load_park_settings(db))


@app.put(
    "/api/park-settings",
    response_model=ParkSettingsOut,
    tags=["园区配置"],
    summary="更新园区退款配置（需 HMAC 签名）",
)
def update_park_settings(
    payload: ParkSettingsUpdate,
    db: DbSession,
    _: RequireSignature,
) -> ParkSettingsOut:
    settings = _load_park_settings(db)
    settings.refund_after_lifecycle = payload.refund_after_lifecycle
    settings.refund_window_hours = payload.refund_window_hours
    db.commit()
    db.refresh(settings)
    logger.info(
        "园区配置已更新：refund_after_lifecycle=%s, refund_window_hours=%s",
        settings.refund_after_lifecycle,
        settings.refund_window_hours,
    )
    return _to_park_settings_out(settings)
```

- [ ] **Step 4: 运行测试**

Run: `pytest -q test_lifecycle.py::test_park_settings_update_requires_signature_and_validates test_split_guard.py`
Expected: PASS

---

### Task 3: 订单生命周期字段、迁移与历史回填

**Files:**
- Modify: `main.py`（Order 模型、OrderOut、迁移、initialize）
- Modify: `test_lifecycle.py`（迁移/回填测试）

**Interfaces:**
- Produces:
  - `Order.entered_at: datetime | None`、`Order.exited_at: datetime | None`
  - `ORDER_LIFECYCLE_COLUMNS_MIGRATION = "20261111_04_orders_lifecycle_columns"`
  - `_migrate_order_lifecycle_columns(bind: Engine) -> None`
  - `_backfill_order_entry_times(bind: Engine) -> None`
  - `OrderOut.entered_at` / `OrderOut.exited_at`

- [ ] **Step 1: 写失败测试**

在 `test_lifecycle.py` 追加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest -q test_lifecycle.py::test_order_lifecycle_columns_migration test_lifecycle.py::test_backfill_entry_time_from_verified_ticket`
Expected: FAIL，`AttributeError: ... _migrate_order_lifecycle_columns`

- [ ] **Step 3: 实现字段、迁移、回填**

在 `Order` 模型 `status` 字段之后加入：

```python
entered_at: Mapped[Optional[datetime]] = mapped_column(
    DateTime, nullable=True, comment="首次门票核销（入园）时间，naive UTC"
)
exited_at: Mapped[Optional[datetime]] = mapped_column(
    DateTime, nullable=True, comment="出口扫码离园时间，naive UTC；为空表示未扫码离园"
)
```

在 `OrderOut` 中加入：

```python
entered_at: datetime | None = None
exited_at: datetime | None = None
```

在 `_migrate_split_amount_column` 之后加入：

```python
ORDER_LIFECYCLE_COLUMNS_MIGRATION = "20261111_04_orders_lifecycle_columns"


def _migrate_order_lifecycle_columns(bind: Engine) -> None:
    """为旧库 orders 表补充 entered_at / exited_at（SQLite ALTER TABLE）。"""
    if bind.dialect.name != "sqlite":
        return
    with bind.begin() as conn:
        _ensure_schema_migrations_table(conn)
        has_orders = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
        ).first()
        if has_orders is None:
            return
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(orders)")
        }
        if "entered_at" not in columns:
            conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN entered_at DATETIME")
        if "exited_at" not in columns:
            conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN exited_at DATETIME")
        _record_migration(conn, ORDER_LIFECYCLE_COLUMNS_MIGRATION)
        logger.info("orders 表已补充生命周期字段 entered_at / exited_at")


def _backfill_order_entry_times(bind: Engine) -> None:
    """历史订单若已核销门票但缺少 entered_at，用最早一次门票核销时间回填。"""
    if bind.dialect.name != "sqlite":
        return
    with bind.begin() as conn:
        has_orders = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
        ).first()
        has_records = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='verify_records'"
        ).first()
        if has_orders is None or has_records is None:
            return
        result = conn.exec_driver_sql(
            """
            UPDATE orders
            SET entered_at = (
                SELECT MIN(v.updated_at) FROM verify_records v
                WHERE v.order_id = orders.id
                  AND v.verify_status = 'verified'
                  AND v.item_name LIKE '%门票%'
            )
            WHERE entered_at IS NULL
              AND EXISTS (
                SELECT 1 FROM verify_records v
                WHERE v.order_id = orders.id
                  AND v.verify_status = 'verified'
                  AND v.item_name LIKE '%门票%'
              )
            """
        )
        if result.rowcount and result.rowcount > 0:
            logger.info("已回填 %s 笔历史订单的入园时间", result.rowcount)
```

在 `initialize_database()` 中按此顺序调用：

```python
_migrate_order_status_split(engine)
_migrate_verify_status_refunded(engine)
_migrate_split_amount_column(engine)
_migrate_order_lifecycle_columns(engine)
Base.metadata.create_all(bind=engine)
_backfill_split_order_status(engine)
_backfill_order_entry_times(engine)
with SessionLocal() as db:
    seed_data(db)
```

- [ ] **Step 4: 运行测试**

Run: `pytest -q test_lifecycle.py test_split_guard.py`
Expected: PASS（含两个新迁移测试）

---

### Task 4: 核销入园与生命周期门槛

**Files:**
- Modify: `main.py`（`_order_lifecycle`、`_verify_blocked_message`、`verify_record`）
- Modify: `test_lifecycle.py`（核销测试）

**Interfaces:**
- Consumes: `evaluate_lifecycle`、`_load_park_settings`、`Order.entered_at`
- Produces:
  - `_order_lifecycle(order: Order, settings: ParkSettings, *, now: datetime | None = None) -> LifecycleSnapshot`
  - `_verify_blocked_message(snapshot: LifecycleSnapshot) -> str`
  - 核销成功时写入 `entered_at`

- [ ] **Step 1: 写失败测试**

在 `test_lifecycle.py` 顶部补 `import uuid` 与 `from datetime import timedelta`，并追加测试辅助：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest -q test_lifecycle.py::test_ticket_verify_starts_lifecycle test_lifecycle.py::test_ticket_verify_blocked_after_closing test_lifecycle.py::test_verify_blocked_after_visit_ends`
Expected: FAIL（`entered_at` 未被写入、核销未被生命周期拦截，或 `_order_lifecycle` 不存在）

- [ ] **Step 3: 实现订单级判定与核销改造**

在 `_to_park_settings_out` 附近加入：

```python
def _order_lifecycle(
    order: Order,
    settings: ParkSettings,
    *,
    now: datetime | None = None,
) -> LifecycleSnapshot:
    records = list(order.verify_records)
    return evaluate_lifecycle(
        entered_at=order.entered_at,
        exited_at=order.exited_at,
        has_ticket=any("门票" in record.item_name for record in records),
        has_pending_items=any(
            record.verify_status == VerifyStatus.UNVERIFIED
            for record in records
        ),
        refund_after_lifecycle=bool(settings.refund_after_lifecycle),
        refund_window_hours=int(settings.refund_window_hours),
        now=now,
    )


def _verify_blocked_message(snapshot: LifecycleSnapshot) -> str:
    if snapshot.status == LifecycleStatus.NOT_STARTED:
        return snapshot.message
    return "本次游园已结束，如需再次入园请重新购票。"
```

改造 `verify_record()`：在选出 `record` 之后、写 `VERIFIED` 之前插入：

```python
    target_is_ticket = "门票" in record.item_name
    if not target_is_ticket:
        ticket_records = [
            item for item in order.verify_records if "门票" in item.item_name
        ]
        if ticket_records and not any(
            item.verify_status == VerifyStatus.VERIFIED
            for item in ticket_records
        ):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="门票未核销，请先去检票口",
            )

    settings = _load_park_settings(db)
    lifecycle = _order_lifecycle(order, settings)
    if not lifecycle.can_verify:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=_verify_blocked_message(lifecycle),
        )

    record.verify_status = VerifyStatus.VERIFIED
    if target_is_ticket and order.entered_at is None:
        order.entered_at = _utcnow_naive()
    db.commit()
```

删除原来的 `record.verify_status = VerifyStatus.VERIFIED` / `db.commit()` 两行，避免重复提交。保留后面的 `db.refresh(record)` 与日志。

- [ ] **Step 4: 运行测试**

Run: `pytest -q test_lifecycle.py::test_ticket_verify_starts_lifecycle test_lifecycle.py::test_ticket_verify_blocked_after_closing test_lifecycle.py::test_verify_blocked_after_visit_ends test_split_guard.py`
Expected: PASS

---

### Task 5: 离园登记与生命周期查询接口

**Files:**
- Modify: `main.py`（`LifecycleOut`、`_to_lifecycle_out`、两个接口）
- Modify: `test_lifecycle.py`（离园/查询测试）

**Interfaces:**
- Consumes: `_order_lifecycle`、`_load_park_settings`、`_load_order_with_records`
- Produces:
  - `LifecycleOut`（字段见 spec 7.1）
  - `_to_lifecycle_out(order, settings, snapshot) -> LifecycleOut`
  - `GET /api/order/{order_id}/lifecycle`
  - `POST /api/verify-records/{record_id}/exit`

- [ ] **Step 1: 写失败测试**

在 `test_lifecycle.py` 追加：

```python
def _exit_path(record_id: int) -> str:
    return f"/api/verify-records/{record_id}/exit"


def test_exit_requires_entry(client):
    order_id = _make_order(
        [("门票", backend.VerifyStatus.UNVERIFIED)],
        total_amount=Decimal("80.00"),
        lifecycle="not_started",
    )
    response = client.post(_exit_path(_record_id(order_id, "门票")))
    assert response.status_code == 409, response.text
    assert "尚未入园" in response.json()["detail"]


def test_exit_is_idempotent_and_completes_lifecycle(client, frozen_now):
    _set_park_settings(False, 72)
    order_id = _make_order(
        [
            ("门票", backend.VerifyStatus.VERIFIED),
            ("咖啡", backend.VerifyStatus.VERIFIED),
            ("越野车", backend.VerifyStatus.UNVERIFIED),
        ],
        lifecycle="active",
    )
    path = _exit_path(_record_id(order_id, "门票"))

    first = client.post(path)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "completed"
    assert body["reason"] == "post_leave_refund_disabled"
    assert body["exited_at"].startswith("2026-11-11T04:00:00")
    assert body["can_split"] is True
    assert body["can_refund"] is False

    second = client.post(path)
    assert second.status_code == 200, second.text
    assert second.json()["exited_at"] == body["exited_at"]


def test_post_leave_refund_window_reports_deadline(client, frozen_now):
    _set_park_settings(True, 72)
    order_id = _make_order(
        [("门票", backend.VerifyStatus.VERIFIED), ("咖啡", backend.VerifyStatus.UNVERIFIED)],
        total_amount=Decimal("120.00"),
        lifecycle="active",
    )

    response = client.post(_exit_path(_record_id(order_id, "门票")))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["reason"] == "post_leave_refund_window"
    assert body["refund_deadline"].startswith("2026-11-14T04:00:00")
    assert body["can_refund"] is True
    assert body["can_verify"] is False
    assert body["can_split"] is False

    queried = client.get(f"/api/order/{order_id}/lifecycle")
    assert queried.status_code == 200
    assert queried.json() == body
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest -q test_lifecycle.py -k "exit or leave_refund_window"`
Expected: FAIL，404 或 `AttributeError`

- [ ] **Step 3: 实现响应模型与接口**

在 `ParkSettingsOut` 附近加入：

```python
class LifecycleOut(BaseModel):
    order_id: int
    status: LifecycleStatus
    reason: LifecycleReason
    entered_at: datetime | None = None
    exited_at: datetime | None = None
    leave_at: datetime | None = None
    refund_deadline: datetime | None = None
    refund_after_lifecycle: bool
    refund_window_hours: int
    can_verify: bool
    can_refund: bool
    can_split: bool
    message: str
```

在 `_verify_blocked_message` 之后加入：

```python
def _to_lifecycle_out(
    order: Order,
    settings: ParkSettings,
    snapshot: LifecycleSnapshot,
) -> LifecycleOut:
    return LifecycleOut(
        order_id=order.id,
        status=snapshot.status,
        reason=snapshot.reason,
        entered_at=snapshot.entered_at,
        exited_at=snapshot.exited_at,
        leave_at=snapshot.leave_at,
        refund_deadline=snapshot.refund_deadline,
        refund_after_lifecycle=bool(settings.refund_after_lifecycle),
        refund_window_hours=int(settings.refund_window_hours),
        can_verify=snapshot.can_verify,
        can_refund=snapshot.can_refund,
        can_split=snapshot.can_split,
        message=snapshot.message,
    )
```

在核销接口之后加入（位置在 `split_order` 之前）：

```python
@app.get(
    "/api/order/{order_id}/lifecycle",
    response_model=LifecycleOut,
    tags=["生命周期"],
    summary="查询订单游园生命周期",
)
def get_order_lifecycle(order_id: int, db: DbSession) -> LifecycleOut:
    order = _load_order_with_records(db, order_id)
    settings = _load_park_settings(db)
    snapshot = _order_lifecycle(order, settings)
    return _to_lifecycle_out(order, settings, snapshot)


@app.post(
    "/api/verify-records/{record_id}/exit",
    response_model=LifecycleOut,
    tags=["生命周期"],
    summary="出口扫码离园登记（幂等）",
)
def register_exit(record_id: int, db: DbSession) -> LifecycleOut:
    seed = db.get(VerifyRecord, record_id)
    if seed is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="核销记录不存在",
        )
    order = _load_order_with_records(db, seed.order_id)
    settings = _load_park_settings(db)

    if order.entered_at is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="门票尚未核销，游客尚未入园，无法办理离园登记",
        )

    if order.exited_at is None:
        order.exited_at = _utcnow_naive()
        db.commit()
        logger.info("离园登记：order_id=%s, exited_at=%s", order.id, order.exited_at)

    snapshot = _order_lifecycle(order, settings)
    return _to_lifecycle_out(order, settings, snapshot)
```

- [ ] **Step 4: 运行测试**

Run: `pytest -q test_lifecycle.py test_split_guard.py`
Expected: PASS

---

### Task 6: 分账/退款生命周期门槛与既有测试适配

**Files:**
- Modify: `main.py`（`split_order`、`refund_order`）
- Modify: `test_split_guard.py`（helper、两个既有测试、新增门槛测试）

**Interfaces:**
- Consumes: `_order_lifecycle`、`_load_park_settings`、`LifecycleSnapshot`
- Produces: 分账与退款在生命周期未满足时返回 409；`_create_order_with_items` 支持 `lifecycle` 参数

- [ ] **Step 1: 改造 `test_split_guard.py` 的订单辅助函数**

把 `_create_order` 与 `_create_order_with_items` 改为：

```python
def _create_order(
    status: backend.OrderStatus,
    *,
    verified: bool = False,
    item_name: str = "门票",
    total_amount: Decimal = Decimal("100.00"),
    lifecycle: str = "completed",
) -> int:
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
        lifecycle=lifecycle,
    )


def _create_order_with_items(
    status: backend.OrderStatus,
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
            for merchant in db.scalars(select(backend.Merchant)).all()
        }
        assert merchants, "seed_data 已初始化商户"

        order = backend.Order(
            order_no=f"TEST-{status.value}-{uuid.uuid4().hex[:12]}",
            total_amount=total_amount,
            status=status,
            entered_at=entered_at,
            exited_at=exited_at,
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


def _set_park_settings(
    refund_after_lifecycle: bool,
    refund_window_hours: int = 72,
) -> None:
    with backend.SessionLocal() as db:
        settings = backend._load_park_settings(db)
        settings.refund_after_lifecycle = refund_after_lifecycle
        settings.refund_window_hours = refund_window_hours
        db.commit()


@pytest.fixture(autouse=True)
def _reset_park_settings(client):
    """每个测试都从默认配置开始，避免测试间互相污染。"""
    _set_park_settings(False, 72)
    yield


@pytest.fixture
def frozen_now(monkeypatch):
    fixed = datetime(2026, 11, 11, 4, 0, 0)
    monkeypatch.setattr(backend, "_utcnow_naive", lambda: fixed)
    return fixed
```

文件顶部补充 `from datetime import datetime, timedelta`。

- [ ] **Step 2: 更新两个既有测试的生命周期前置条件**

`test_partial_refund_then_split_verified_items`：在创建订单前加 `_set_park_settings(True, 72)`，订单参数加 `lifecycle="active"`，并接收 `frozen_now`。

```python
def test_partial_refund_then_split_verified_items(client: TestClient, frozen_now):
    _set_park_settings(True, 72)
    order_id = _create_order_with_items(
        backend.OrderStatus.PAID,
        [
            ("门票", backend.VerifyStatus.VERIFIED),
            ("咖啡", backend.VerifyStatus.VERIFIED),
            ("越野车", backend.VerifyStatus.UNVERIFIED),
        ],
        lifecycle="active",
    )
    # 其余断言保持不变
```

`test_all_unverified_refund_marks_refunded_and_blocks_split`：接收 `frozen_now`，加 `_set_park_settings(True, 72)`，并改为 `_create_order(..., lifecycle="active")`。

- [ ] **Step 3: 写新的门槛失败测试**

在 `test_split_guard.py` 追加：

```python
def test_split_blocked_while_in_park(client: TestClient, frozen_now):
    _set_park_settings(False, 72)
    order_id = _create_order_with_items(
        backend.OrderStatus.PAID,
        [
            ("门票", backend.VerifyStatus.VERIFIED),
            ("咖啡", backend.VerifyStatus.VERIFIED),
            ("越野车", backend.VerifyStatus.UNVERIFIED),
        ],
        lifecycle="active",
    )
    response = _signed_request(client, "POST", _split_path(order_id))
    assert response.status_code == 409, response.text
    assert "生命周期" in response.json()["detail"]
    assert _count_split_records(order_id) == 0


def test_post_leave_refund_then_all_items_terminal_allows_split(
    client: TestClient, frozen_now
):
    _set_park_settings(True, 72)
    order_id = _create_order_with_items(
        backend.OrderStatus.PAID,
        [("门票", backend.VerifyStatus.VERIFIED), ("咖啡", backend.VerifyStatus.UNVERIFIED)],
        total_amount=Decimal("120.00"),
        lifecycle="active",
    )
    with backend.SessionLocal() as db:
        records = {
            record.item_name: int(record.id)
            for record in db.scalars(
                select(backend.VerifyRecord).where(
                    backend.VerifyRecord.order_id == order_id
                )
            )
        }

    exit_response = client.post(
        f"/api/verify-records/{records['门票']}/exit"
    )
    assert exit_response.status_code == 200, exit_response.text

    blocked = client.post(
        f"/api/verify-records/{records['咖啡']}/verify",
        params={"post": "coffee"},
    )
    assert blocked.status_code == 409, blocked.text
    assert "重新购票" in blocked.json()["detail"]

    refund = _signed_request(client, "POST", _refund_path(order_id))
    assert refund.status_code == 200, refund.text
    assert refund.json()["refund_amount"] == "36.00"

    lifecycle = client.get(f"/api/order/{order_id}/lifecycle").json()
    assert lifecycle["status"] == "completed"
    assert lifecycle["reason"] == "all_items_settled"

    split = _signed_request(client, "POST", _split_path(order_id))
    assert split.status_code == 200, split.text
    assert split.json()["split_amount"] == "80.00"


def test_refund_blocked_after_lifecycle_completed(client: TestClient, frozen_now):
    _set_park_settings(False, 72)
    order_id = _create_order_with_items(
        backend.OrderStatus.PAID,
        [("门票", backend.VerifyStatus.VERIFIED), ("咖啡", backend.VerifyStatus.UNVERIFIED)],
        lifecycle="completed",
    )
    response = _signed_request(client, "POST", _refund_path(order_id))
    assert response.status_code == 409, response.text
    assert "生命周期已结束" in response.json()["detail"]


def test_refund_blocked_after_refund_window_expires(client: TestClient, frozen_now):
    _set_park_settings(True, 0)
    order_id = _create_order_with_items(
        backend.OrderStatus.PAID,
        [("门票", backend.VerifyStatus.VERIFIED), ("咖啡", backend.VerifyStatus.UNVERIFIED)],
        lifecycle="post_leave",
    )
    response = _signed_request(client, "POST", _refund_path(order_id))
    assert response.status_code == 409, response.text
    assert "退款窗口" in response.json()["detail"]


def test_non_ticket_order_splits_without_lifecycle(client: TestClient):
    _set_park_settings(False, 72)
    order_id = _create_order_with_items(
        backend.OrderStatus.PAID,
        [("咖啡", backend.VerifyStatus.VERIFIED)],
        total_amount=Decimal("40.00"),
        lifecycle="not_started",
    )
    response = _signed_request(client, "POST", _split_path(order_id))
    assert response.status_code == 200, response.text
    assert response.json()["split_amount"] == "40.00"
```

- [ ] **Step 4: 运行确认失败**

Run: `pytest -q test_split_guard.py -k "split_blocked or post_leave or refund_blocked or non_ticket"`
Expected: FAIL（分账/退款尚未接生命周期门槛）

- [ ] **Step 5: 在分账与退款接口接入门槛**

`split_order`：在 `if not order.verify_records:` 之后、`verified_items = [...]` 之前插入：

```python
    settings = _load_park_settings(db)
    lifecycle = _order_lifecycle(order, settings)
    if not lifecycle.can_split:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=lifecycle.message,
        )
```

`refund_order`：在 `if not order.verify_records:` 之后、`result = _build_refund_result(db, order)` 之前插入：

```python
    settings = _load_park_settings(db)
    lifecycle = _order_lifecycle(order, settings)
    if not lifecycle.can_refund:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=lifecycle.message,
        )
```

- [ ] **Step 6: 运行全部后端测试**

Run: `pytest -q test_lifecycle.py test_split_guard.py`
Expected: PASS（含原 11 个分账测试与新生命周期测试）

---

### Task 7: Gradio 前端（离园岗位、生命周期展示、园区配置）

**Files:**
- Modify: `app_scan.py`
- Create: `test_scan_ui.py`

**Interfaces:**
- Consumes: `GET /api/park-settings`、`PUT /api/park-settings`、`GET /api/order/{id}/lifecycle`、`POST /api/verify-records/{id}/exit`
- Produces:
  - `EXIT_POST_LABEL = "景区出口"`、`SCAN_POSTS`
  - `_signed_request(method, path, body=b"") -> requests.Response`
  - `_fetch_order_lifecycle(order_id) -> dict | None`
  - `_render_lifecycle_html(data: dict) -> str`
  - `load_park_settings()`、`save_park_settings(allow, hours)`
  - `_submit_exit_request(seed_record_id, *, origin="")`

- [ ] **Step 1: 写前端失败测试**

`test_scan_ui.py`：

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app_scan
import main as backend


def test_exit_post_config_is_routable():
    assert app_scan._post_config("景区出口")["key"] == "exit"


def test_render_lifecycle_html_completed_shows_split_ready():
    html = app_scan._render_lifecycle_html(
        {
            "status": "completed",
            "message": "生命周期已结束：全部子项均已核销或已退款，可以进行分账。",
            "entered_at": "2026-11-11T02:00:00",
            "exited_at": "2026-11-11T04:00:00",
            "leave_at": "2026-11-11T04:00:00",
            "refund_deadline": None,
            "can_split": True,
            "can_refund": False,
        }
    )
    assert "已结束" in html
    assert "可分账" in html
    assert "不可退款" in html
    assert "生命周期已结束" in html


def test_render_lifecycle_html_active_shows_waiting():
    html = app_scan._render_lifecycle_html(
        {
            "status": "active",
            "message": "游客在园内，生命周期进行中。",
            "entered_at": "2026-11-11T02:00:00",
            "exited_at": None,
            "leave_at": "2026-11-11T10:00:00",
            "refund_deadline": None,
            "can_split": False,
            "can_refund": True,
        }
    )
    assert "游园中" in html
    assert "分账待生命周期结束" in html
    assert "可退款" in html


def test_signed_request_signs_exact_body(monkeypatch):
    fixed_ts = 1762819200
    monkeypatch.setattr(
        app_scan,
        "time",
        SimpleNamespace(time=lambda: fixed_ts),
    )
    captured = {}

    class FakeResponse:
        status_code = 200
        content = b"{}"

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["data"] = kwargs["data"]
        return FakeResponse()

    monkeypatch.setattr(app_scan.requests, "request", fake_request)

    body = b'{"refund_after_lifecycle":true,"refund_window_hours":24}'
    response = app_scan._signed_request("PUT", "/api/park-settings", body)

    assert response.status_code == 200
    expected = backend.build_signature_headers(
        "PUT", "/api/park-settings", body=body, timestamp=fixed_ts
    )
    assert captured["headers"]["X-Signature"] == expected["X-Signature"]
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["data"] == body
    assert captured["method"] == "PUT"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest -q test_scan_ui.py`
Expected: FAIL，`_post_config("景区出口")` 回退到检票员、`_render_lifecycle_html` / `_signed_request` 不存在

- [ ] **Step 3: 改造签名调用与岗位配置**

在 `VERIFY_POSTS` 后加入：

```python
EXIT_POST_LABEL = "景区出口"
EXIT_POST_CONFIG = {"key": "exit", "item": "离园", "icon": "🚪"}
SCAN_POSTS: dict[str, dict[str, str]] = {
    **VERIFY_POSTS,
    EXIT_POST_LABEL: EXIT_POST_CONFIG,
}
```

把 `_post_config` 改为从 `SCAN_POSTS` 取：

```python
def _post_config(post_label: Any) -> dict[str, str]:
    label = str(post_label or "").strip()
    return SCAN_POSTS.get(label, SCAN_POSTS[DEFAULT_VERIFY_POST])
```

把 `_post_signed` 替换为通用签名请求：

```python
def _signed_request(
    method: str,
    path: str,
    body: bytes = b"",
) -> requests.Response:
    headers = _build_signature_headers(method, path, body)
    if body:
        headers["Content-Type"] = "application/json"
    logger.info("调用签名接口：%s %s", method.upper(), path)
    return requests.request(
        method.upper(),
        f"{API_BASE_URL}{path}",
        headers=headers,
        data=body,
        timeout=REQUEST_TIMEOUT,
    )


def _post_signed(path: str) -> requests.Response:
    return _signed_request("POST", path)
```

- [ ] **Step 4: 增加生命周期读取与渲染**

导入行改为：

```python
from datetime import datetime, timedelta, timezone
```

在 `_render_order_status` 之前加入：

```python
LIFECYCLE_STATUS_META = {
    "not_applicable": ("不适用", "is-na"),
    "not_started": ("未入园", "is-not-started"),
    "active": ("游园中", "is-active"),
    "completed": ("已结束", "is-completed"),
}


def _local_time_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")


def _fetch_order_lifecycle(order_id: int | None) -> dict[str, Any] | None:
    if order_id is None:
        return None
    try:
        response = requests.get(
            f"{API_BASE_URL}/api/order/{int(order_id)}/lifecycle",
            timeout=min(REQUEST_TIMEOUT, 2),
        )
    except (requests.RequestException, TypeError, ValueError):
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _render_lifecycle_html(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    status = str(data.get("status") or "")
    label, css_class = LIFECYCLE_STATUS_META.get(
        status, ("状态未知", "is-na")
    )
    chips = []
    if data.get("can_split"):
        chips.append('<span class="lifecycle-chip is-ok">可分账</span>')
    else:
        chips.append(
            '<span class="lifecycle-chip is-wait">分账待生命周期结束</span>'
        )
    if data.get("can_refund"):
        chips.append('<span class="lifecycle-chip is-ok">可退款</span>')
    else:
        chips.append('<span class="lifecycle-chip is-muted">不可退款</span>')

    meta_parts = []
    entered = _local_time_label(data.get("entered_at"))
    exited = _local_time_label(data.get("exited_at"))
    leave_at = _local_time_label(data.get("leave_at"))
    deadline = _local_time_label(data.get("refund_deadline"))
    if entered:
        meta_parts.append(f"入园 {entered}")
    if exited:
        meta_parts.append(f"离园 {exited}")
    elif leave_at:
        meta_parts.append(f"闭园 {leave_at}")
    if deadline:
        meta_parts.append(f"退款截止 {deadline}")

    message = html.escape(str(data.get("message") or ""))
    meta_html = (
        f'<div class="lifecycle-meta">{" · ".join(meta_parts)}</div>'
        if meta_parts
        else ""
    )
    return (
        '<div class="lifecycle-panel">'
        '<div class="lifecycle-head">'
        f'<span class="lifecycle-badge {css_class}">{label}</span>'
        f'{"".join(chips)}'
        "</div>"
        f'<div class="lifecycle-message">{message}</div>'
        f"{meta_html}"
        "</div>"
    )
```

在 `_render_order_status` 的返回拼接中，把 `f'<p class="status-tip">{tip}</p>'` 后追加生命周期区块：

```python
    lifecycle_html = _render_lifecycle_html(_fetch_order_lifecycle(order_id))
    ...
    return (
        '<div class="status-panel">'
        '<div class="status-panel-head">'
        '<span class="status-panel-title">📋 我的订单状态</span>'
        f"{order_no_html}"
        "</div>"
        f'<div class="status-items">{items_html}</div>'
        f'<p class="status-tip">{tip}</p>'
        f"{lifecycle_html}"
        "</div>"
    )
```

- [ ] **Step 5: 增加离园请求与路由**

在 `_submit_verify_request` 之前加入：

```python
def _submit_exit_request(
    seed_record_id: int,
    *,
    origin: str = "",
) -> tuple[str, str]:
    exit_url = f"{API_BASE_URL}/api/verify-records/{seed_record_id}/exit"
    logger.info("调用离园接口：POST %s（来源：%s）", exit_url, origin or "出口扫码")
    try:
        response = requests.post(exit_url, timeout=REQUEST_TIMEOUT)
    except requests.ConnectionError:
        return _verification_result(
            "error", "离园登记失败",
            f"无法连接后端（{html.escape(API_BASE_URL)}），请联系现场技术人员。",
            "❌ 离园登记失败，请检查网络后重试",
        )
    except requests.Timeout:
        return _verification_result(
            "error", "离园登记失败",
            f"后端在 {REQUEST_TIMEOUT:g} 秒内没有响应，请稍后重试。",
            "❌ 离园登记失败，请稍后重试",
        )
    except requests.RequestException:
        return _verification_result(
            "error", "离园登记失败", "离园请求异常，请稍后重试。",
            "❌ 离园登记失败，请稍后重试",
        )

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message") or "离园登记成功。")
        detail = html.escape(message)
        if body.get("can_split"):
            detail += "<br><b>生命周期已结束，可以进行分账。</b>"
        else:
            detail += "<br>生命周期尚未结束，暂不可分账。"
        return _verification_result(
            "success", "离园登记成功", detail, "👋 离园登记成功"
        )

    detail_text = _response_detail(response)
    if response.status_code == 409:
        return _verification_result(
            "warning", "尚未入园，无法离园",
            html.escape(detail_text) or "请先核销门票入园。",
            "⚠️ 尚未入园，无法离园",
        )
    if response.status_code == 404:
        return _verification_result(
            "error", "未找到这张门票",
            "请让游客重新出示购票二维码，或到售票处核验订单。",
            "❌ 未找到这张门票",
        )
    return _verification_result(
        "error", "离园登记失败", "离园系统暂时不可用，请稍后重试。",
        "❌ 离园登记失败，请稍后重试",
    )
```

在 `_submit_verify_request` 开头、`config = _post_config(post_label)` 之后加入路由：

```python
    if config["key"] == "exit":
        return _submit_exit_request(seed_record_id, origin=origin)
```

- [ ] **Step 6: 增加园区配置读写函数**

在 `request_split` 之后加入：

```python
def load_park_settings() -> tuple[Any, Any, str]:
    path = "/api/park-settings"
    try:
        response = requests.get(f"{API_BASE_URL}{path}", timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return (
            gr.update(),
            gr.update(),
            _status_card("warning", "无法读取园区配置", html.escape(str(exc))),
        )
    if response.status_code != 200:
        return (
            gr.update(),
            gr.update(),
            _render_request_error(
                f"读取园区配置失败（HTTP {response.status_code}）",
                _response_detail(response) or "后端未返回错误信息。",
                path,
            ),
        )
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return (
            gr.update(),
            gr.update(),
            _status_card("error", "园区配置格式错误", "后端返回内容无法解析。"),
        )
    allow = bool(data.get("refund_after_lifecycle"))
    hours = int(data.get("refund_window_hours", 72))
    return (
        gr.update(value=allow),
        gr.update(value=hours),
        _status_card(
            "info",
            "园区配置已加载",
            f"离园后退款：{'允许' if allow else '不允许'} · "
            f"退款窗口：{hours} 小时",
        ),
    )


def save_park_settings(allow: Any, hours: Any) -> str:
    try:
        hours_value = int(float(hours))
    except (TypeError, ValueError):
        return _status_card(
            "error", "配置无效", "退款窗口必须为 0–8760 的整数小时。"
        )
    if not 0 <= hours_value <= 8760:
        return _status_card(
            "error", "配置无效", "退款窗口必须为 0–8760 的整数小时。"
        )

    path = "/api/park-settings"
    body = json.dumps(
        {
            "refund_after_lifecycle": bool(allow),
            "refund_window_hours": hours_value,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        response = _signed_request("PUT", path, body)
    except requests.ConnectionError:
        return _render_request_error(
            "无法连接后端", f"请确认已启动 python main.py（{API_BASE_URL}）。", path
        )
    except requests.Timeout:
        return _render_request_error(
            "请求超时", f"后端在 {REQUEST_TIMEOUT:g} 秒内没有响应。", path
        )
    except requests.RequestException as exc:
        return _render_request_error("请求异常", str(exc), path)

    if response.status_code == 200:
        return _status_card(
            "success",
            "园区配置已保存",
            f"离园后退款：{'允许' if allow else '不允许'} · "
            f"退款窗口：{hours_value} 小时",
        )
    return _render_request_error(
        f"保存失败（HTTP {response.status_code}）",
        _response_detail(response) or "后端未返回错误信息。",
        path,
    )
```

- [ ] **Step 7: 更新 Gradio 界面与事件绑定**

商家核销 Tab：

- 岗位下拉的 `choices` 改为 `choices=list(SCAN_POSTS.keys())`。
- Markdown 增加一行：`· 景区出口：登记离园；离园后按园区配置决定未核销项目是否还能退款`。
- 说明文案改为“系统只处理该岗位对应的项目”。

运营后台 Tab，在退款/分账按钮之后、`action_result` 之前加入：

```python
                gr.Markdown(
                    "### 🏞️ 游园生命周期与退款规则\n"
                    "分账只在生命周期结束后执行；离园或闭园后，"
                    "未核销项目是否还能退款由下面的园区配置决定。"
                )
                with gr.Row():
                    refund_after_lifecycle = gr.Checkbox(
                        label="离园后仍允许退款",
                        value=False,
                        info="关闭时，离园/闭园后未核销项目立即不可退款",
                    )
                    refund_window_hours = gr.Number(
                        label="离园后退款窗口（小时）",
                        value=72,
                        precision=0,
                        minimum=0,
                        maximum=8760,
                        info="从离园/闭园时间起算，默认 72 小时",
                    )
                settings_button = gr.Button(
                    "💾 保存园区配置",
                    variant="secondary",
                    size="lg",
                )
                settings_result = gr.HTML(
                    value="",
                    padding=False,
                    container=False,
                )
```

事件绑定区加入：

```python
    settings_button.click(
        save_park_settings,
        inputs=[refund_after_lifecycle, refund_window_hours],
        outputs=settings_result,
        api_name="save_park_settings",
    )
    demo.load(
        load_park_settings,
        inputs=None,
        outputs=[refund_after_lifecycle, refund_window_hours, settings_result],
    )
```

游客订单状态区域已有 `refresh_button` / `status_timer` 调用 `_render_order_status`，无需新增绑定。

CSS 中追加（放在现有 `.status-panel` 样式附近）：

```css
.lifecycle-panel { margin-top: 12px; padding: 12px 14px; border-radius: 12px; background: #f8fafc; border: 1px solid #e2e8f0; }
.lifecycle-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.lifecycle-badge { font-size: 12px; font-weight: 700; padding: 3px 10px; border-radius: 999px; }
.lifecycle-badge.is-active { background: #ecfdf5; color: #047857; }
.lifecycle-badge.is-completed { background: #eff6ff; color: #1d4ed8; }
.lifecycle-badge.is-not-started { background: #fffbeb; color: #b45309; }
.lifecycle-badge.is-na { background: #f1f5f9; color: #475569; }
.lifecycle-chip { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #f1f5f9; color: #475569; }
.lifecycle-chip.is-ok { background: #ecfdf5; color: #047857; }
.lifecycle-chip.is-wait { background: #fffbeb; color: #b45309; }
.lifecycle-chip.is-muted { background: #f1f5f9; color: #64748b; }
.lifecycle-message { margin-top: 8px; font-size: 13px; color: #334155; line-height: 1.5; }
.lifecycle-meta { margin-top: 6px; font-size: 12px; color: #64748b; }
```

- [ ] **Step 8: 更新运营后台/游客提示文案**

- 运营后台说明：`"分账仅结算“已核销”子项；只有生命周期结束后才能分账。离园后是否允许退款由园区配置决定。"`
- `_QR_NOTE_HTML` 增加：`"离园时请在景区出口扫码登记；再次入园需重新购票。"`
- `_QR_SCAN_TIP_HTML` 保持现有门票前置提示。

- [ ] **Step 9: 运行前端与后端测试**

Run: `pytest -q test_scan_ui.py test_lifecycle.py test_split_guard.py`
Expected: PASS

---

### Task 8: 文档、状态机说明与全量回归

**Files:**
- Modify: `main.py`（模块 docstring 的业务说明与状态机）
- Modify: `docs/superpowers/specs/2026-11-11-ticket-lifecycle-design.md`（如实现中调整了字段名/文案，同步更新）

**Interfaces:**
- Consumes: 前 7 个任务的全部产出
- Produces: 与代码一致的使用说明

- [ ] **Step 1: 更新 `main.py` 模块 docstring**

在“4. 智能退款”之后新增一节：

```text
5. 门票使用生命周期（实现分账 / 退款 / 核销的业务前置条件）
   - 门票核销即入园，记录 orders.entered_at；
   - 出口扫码记录 orders.exited_at；闭园时间由 PARK_CLOSE_TIME 控制（默认 18:00）；
   - leave_at = min(exited_at, 入园当天闭园时间)；
   - 全部子项终态（verified / refunded）立即结束；
   - 仍有未核销子项时：若允许离园后退款，则在 leave_at + refund_window_hours
     后结束；若不允许，则在 leave_at 结束后立即结束；
   - 生命周期结束（completed）或订单不含门票（not_applicable）才允许分账；
   - 结束后不可退款、不可再次核销；再次入园必须重新购票；
   - 查询：GET /api/order/{order_id}/lifecycle；
   - 离园：POST /api/verify-records/{record_id}/exit（幂等、无需签名）；
   - 运营配置：GET /api/park-settings；PUT /api/park-settings（需签名）。
```

把“5. 一键重置演示环境”改为“6. 一键重置演示环境”。

在“资金操作状态机”表格下补充生命周期表：

```text
生命周期        可分账  可退款  可核销  说明
not_applicable   ✓       ✓*      ✓      订单不含门票
not_started      ✗       ✓*      ✓(门票) 未入园；闭园后不可检票
active(园内)      ✗       ✓       ✓      游园中
active(离园窗口)  ✗       ✓       ✗      已离园/闭园，等待退款窗口结束
completed        ✓       ✗       ✗      生命周期结束，必须重新购票才能再入园

* 可退款还要求订单为 paid、至少一个未核销子项，且未分账。
```

在“数据库迁移与多实例扩展性”中补充：

```text
- 另以 ALTER TABLE 为 orders 增加 entered_at / exited_at，并对已核销门票的
  历史订单用最早核销时间回填 entered_at；park_settings 为新建单行配置表。
```

- [ ] **Step 2: 同步 spec 中的实现细节**

检查 spec 的接口字段名与文案是否与代码一致；只修改不一致处，不改变已确认的业务规则。

- [ ] **Step 3: 全量测试**

Run: `pytest -q`
Expected: 全部 PASS，输出包含 `test_api.py` 之外的测试；`test_api.py` 是手动联调脚本，不参与 pytest。

- [ ] **Step 4: 手工冒烟（按顺序执行，确认页面行为）**

1. 运行 `python main.py`，再运行 `python app_scan.py`。
2. 重置演示环境，生成新订单。
3. 游客端订单状态显示“未入园”。
4. 商家端选择“景区检票员”核销门票，游客端刷新后显示“游园中 / 分账待生命周期结束 / 可退款”。
5. 运营后台点击“执行分账”，应返回 409，提示生命周期未结束。
6. 商家端选择“景区出口”，扫描同一张票，显示“离园登记成功”；默认配置下订单状态变为“已结束 / 可分账 / 不可退款”。
7. 运营后台将“离园后退款窗口”设为 72 小时并开启“离园后仍允许退款”，保存成功；重新重置演示、核销门票、离园后，订单状态应为“游园中（离园退款窗口）”，可退款、不可分账。
8. 在窗口内退款未核销项目后，订单应变为“已结束 / 可分账”，随后分账成功。
