"""
文旅小程序后端底座（FastAPI + SQLAlchemy 2.0 + SQLite）
=======================================================

运行前安装依赖（Python 3.10+）：
    pip install fastapi "uvicorn[standard]" "sqlalchemy>=2.0"

启动方式：
    python main.py
    # 或者：uvicorn main:app --reload

启动时会自动：
    1. 创建 tourism.db（SQLite 文件）；
    2. 执行幂等结构迁移（orders.status 增加 split，见 schema_migrations）；
    3. 按模型自动建表；
    4. 幂等写入演示商户 / 商品 / 订单 / 核销记录。

表关联关系：
    Merchant  1 ── N Product        products.merchant_id   -> merchants.id
    Order     1 ── N VerifyRecord   verify_records.order_id -> orders.id
    Merchant  1 ── N VerifyRecord   verify_records.merchant_id -> merchants.id
    Order     1 ── 1 SplitRecord    split_records.order_id -> orders.id（唯一）
    SplitRecord 1 ── N SplitItemRecord  split_record_items.split_record_id

    说明：Order 与 Merchant 之间没有直接外键。一笔订单可能跨商户履约
    （例如“门票+咖啡+越野车套餐”），因此由 VerifyRecord 逐项记录“这一项由哪个
    商户核销”，分账也按 VerifyRecord 上的商户 split_rate 计算。

企业级核心接口
==============
1. 接口签名验证（依赖项 require_signature）
   受保护接口必须携带：
       X-Timestamp: Unix 秒级时间戳，例如 "1762819200"
       X-Signature: HMAC-SHA256 十六进制签名（大小写均可，可带 "sha256=" 前缀）

   签名原文（canonical string，字段之间用换行符分隔）：
       {timestamp}\n{METHOD}\n{path}\n{sha256_hex(body)}

   其中：
       - timestamp 为 X-Timestamp 原值；
       - METHOD 为大写 HTTP 方法；
       - path 为请求路径（如有 query string，需一并包含，例如 /api/x?a=1）；
       - body 为原始请求体字节（无请求体时为空字节串，其 SHA-256 仍参与计算）。

   signature = HMAC-SHA256(secret, canonical).hexdigest()

   密钥来自环境变量 API_SIGNING_SECRET；未设置时使用仅供演示的默认值。
   时间戳允许偏差由 API_SIGNATURE_TTL_SECONDS 控制（默认 300 秒），
   超出窗口或签名不一致均返回 403。

   生成签名的示例：
       from main import build_signature_headers
       headers = build_signature_headers("POST", f"/api/order/{order_id}/split")
       requests.post(url, headers=headers)

2. 分账引擎：POST /api/order/{order_id}/split
   - 支持「部分核销 + 部分退款」订单：只对“已核销”子项分账；
   - 未核销 / 已退款子项不参与分账，金额、平台抽成、商户实得均记为 0；
   - 已支付、部分退款后的订单均可分账；未支付、已分账订单拒绝；
   - 至少需要一个已核销子项，否则没有可分账金额；
   - 按各商户 split_rate（门票 5%、咖啡 10%、越野车 15%）计算平台留存与商户实得；
   - 成功后订单状态原子迁移为 split（已分账），并写入 split_records +
     split_record_items 快照；同一订单只允许分账一次。

3. 分账查询（均需 HMAC 签名）
   - GET /api/order/{order_id}/split：查询单笔订单的分账流水与项目快照；
   - GET /api/split-records：分页查询分账流水，支持 order_id / order_no 筛选。

4. 智能退款：POST /api/order/{order_id}/refund
   - 已核销项目（门票 / 咖啡 / 越野车）一律标记为“拒绝退款”，退款 0 元；
   - 未核销项目按原始单价退款，统一扣除 10% 手续费，退款后标记为已退款；
   - 全部未核销：订单全额退款，整体扣除 10% 手续费；
   - 部分核销：仅退未核销子项，已核销子项退款 0 元且仍可参与分账；
   - 全部已核销：返回 409，提示不可退款；
   - 响应 items 为逐项明细（项目 / 核销状态 / 退款金额 / 原因）；
   - 已分账订单禁止退款；
   - 只要本次产生退款，订单状态原子迁移为 refunded，防止重复退款；
   - 部分退款后，已核销子项仍可调用分账接口单独结算。

5. 门票使用生命周期（实现分账 / 退款 / 核销的业务前置条件）
   - 门票核销即入园，记录 orders.entered_at；
   - 出口扫码记录 orders.exited_at；闭园时间取订单门票商户的 merchants.close_time（默认 23:59:59）；
   - leave_at = min(exited_at, 入园当天闭园时间)；
   - 全部子项终态（verified / refunded）立即结束；
   - 仍有未核销子项时：若允许离园后退款，则在 leave_at + refund_window_hours
     后结束；若不允许，则在 leave_at 结束后立即结束；
   - 生命周期结束（completed）或订单不含门票（not_applicable）才允许分账；
   - 结束后不可退款、不可再次核销；再次入园必须重新购票；
   - 查询：GET /api/order/{order_id}/lifecycle；
   - 离园：POST /api/verify-records/{record_id}/exit（幂等、无需签名）；
   - 运营配置：GET /api/park-settings；PUT /api/park-settings（需签名）。

6. 离园结算引擎：POST /api/order/{order_id}/exit-settlement（需签名）
   - 适用于已入园、尚未分账的订单，解决“已离园但一直卡在部分核销”；
   - 未登记离园时自动补登 exited_at；
   - 将所有 unverified 子项强制置为 expired（不可再用、不可退）；
   - 已核销金额按商户 split_rate 分账给商户；
   - 已过期金额全额留在平台（forfeited_amount / platform_retention）；
   - 订单原子迁移为 split，并写入唯一分账流水快照。

7. 一键重置演示环境：POST /api/demo/reset（需 HMAC 签名）
   - 清空 orders / verify_records / split_records 历史数据；
   - 自动创建一张 paid + 三项均 unverified 的测试订单；
   - 返回新订单 ID、订单号与门票核销记录 ID，供演示直接扫码。

资金操作状态机
==============
允许「部分退款 + 部分核销」：退款只退未核销子项，已核销子项仍可分账。

    订单状态       可分账  可退款  可核销  说明
    unpaid          ✗       ✗       ✗      未支付
    paid            ✓*      ✓*      ✓      已支付、未分账
    split           ✗(重复)  ✗       ✓      已分账，资金已分配
    refunded        ✓*      ✗(重复)  ✗      部分退款，已核销子项待分账

    * 分账只对“已核销”子项生效（未核销 / 已退款子项记 0），且至少需要
      一个已核销子项；退款要求至少一个未核销子项。分账通过条件 UPDATE
      （paid / refunded → split）完成原子状态迁移，同一订单只会生成一条
      分账流水；多实例部署下由条件 UPDATE + split_records 唯一约束兜底。

游园生命周期状态机
==================

    生命周期        可分账  可退款  可核销  说明
    not_applicable   ✓       ✓*      ✓      订单不含门票
    not_started      ✗       ✓*      ✓(门票) 未入园；闭园后不可检票
    active(园内)      ✗       ✓       ✓      游园中
    active(离园窗口)  ✗       ✓       ✗      已离园/闭园，等待退款窗口结束
    completed        ✓       ✗       ✗      生命周期结束，必须重新购票才能再入园

    * 可退款还要求订单为 paid、至少一个未核销子项，且未分账。

    子项终态：verified（已核销）/ refunded（已退款）/ expired（离园结算过期）。
    过期子项不可核销、不可退款，金额计入平台留存；离园结算后订单不再存
    在 unverified 子项。

数据库迁移与多实例扩展性
========================
- SQLite 不支持修改 CHECK 约束，启动时通过 schema_migrations 执行一次性
  表重建迁移，为 orders.status 增加 split、为 verify_records.verify_status
  增加 refunded；另以 ALTER TABLE 为 split_records 增加 split_amount。
  迁移幂等且保留历史数据。
- 另以 ALTER TABLE 为 orders 增加 entered_at / exited_at，并对已核销门票的
  历史订单用最早核销时间回填 entered_at；park_settings 为新建单行配置表。
- 资金动作的状态迁移使用条件 UPDATE + 唯一约束，不依赖进程内锁，
  可直接扩展到多实例部署。
- 若切换 PostgreSQL / MySQL，orders.status 的枚举变更应交给 Alembic
  或 DBA 工单执行；业务层的条件 UPDATE 逻辑无需修改。

金额精度
========
所有金额运算统一使用 Decimal 与“分”级量化（ROUND_HALF_UP）。响应 JSON 中
的金额字段以两位小数字符串返回（例如 "76.00"），避免浮点数精度丢失。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum as PyEnum
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    func,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("tourism-mp")


# ===========================================================================
# 一、数据库连接配置
# ===========================================================================

BASE_DIR = Path(__file__).resolve().parent

# 默认使用项目目录下的 tourism.db；也可通过环境变量 DATABASE_URL 切换数据库。
# 注意 SQLite 路径要使用正斜杠，避免 Windows 反斜杠转义问题。
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{(BASE_DIR / 'tourism.db').as_posix()}",
)

engine = create_engine(
    DATABASE_URL,
    # SQLite 默认禁止跨线程复用连接，FastAPI 的多线程执行需要关闭此限制。
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    echo=os.getenv("SQL_ECHO", "0") == "1",  # SQL_ECHO=1 可打印 SQL，便于排查
    future=True,
)


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """SQLite 默认不校验外键，必须为每条连接显式打开 PRAGMA。"""
    if not DATABASE_URL.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# autoflush=False：避免查询时意外触发未完成的写入；
# expire_on_commit=False：commit 后对象仍可直接读取，便于返回响应。
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


# ===========================================================================
# 游园生命周期时间工具
# ===========================================================================

PARK_TZ = timezone(timedelta(hours=8))
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


def _utcnow_naive() -> datetime:
    """当前 UTC 时间，去掉 tzinfo，与 SQLite 中存储的 naive UTC 一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _to_park_local(value: datetime) -> datetime:
    return _as_utc_naive(value).replace(tzinfo=timezone.utc).astimezone(PARK_TZ)


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


def _format_park_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return _to_park_local(value).strftime("%Y-%m-%d %H:%M")


def _compute_leave_at(
    entered_at: datetime | None,
    exited_at: datetime | None,
    close_time: dt_time | None = None,
) -> datetime | None:
    """
    计算游客的“离园时刻”（naive UTC）。

    leave_at = min(实际扫码离园时间, 入园当天闭园时刻)。
    未入园返回 None；已入园但未扫码离园时，以闭园时刻作为离园时间。
    该函数同时被生命周期判定与退款优先级校验复用，避免两处口径漂移。
    """
    if entered_at is None:
        return None
    entered_value = _as_utc_naive(entered_at)
    closing_at = _today_closing_at(
        entered_value, close_time or DEFAULT_CLOSE_TIME_VALUE
    )
    if exited_at is None:
        return closing_at
    return min(_as_utc_naive(exited_at), closing_at)


def evaluate_lifecycle(
    *,
    entered_at: datetime | None,
    exited_at: datetime | None,
    has_ticket: bool,
    has_pending_items: bool,
    refund_after_lifecycle: bool,
    refund_window_hours: int,
    now: datetime | None = None,
    close_time: dt_time | None = None,
) -> LifecycleSnapshot:
    """根据订单字段、子项状态、园区配置与当前时间计算生命周期快照。

    leave_at = min(exited_at, 入园当天的闭园时刻)，全部使用 naive UTC 比较。
    close_time 来自订单门票商户，缺失时回退 23:59:59。
    """
    now_value = _utcnow_naive() if now is None else _as_utc_naive(now)
    closing_time = close_time or DEFAULT_CLOSE_TIME_VALUE

    if not has_ticket:
        return LifecycleSnapshot(
            status=LifecycleStatus.NOT_APPLICABLE,
            reason=LifecycleReason.NOT_APPLICABLE,
            entered_at=None,
            exited_at=None,
            leave_at=None,
            refund_deadline=None,
            can_verify=True,
            can_refund=has_pending_items,
            can_split=True,
            message="订单不含门票，不适用游园生命周期，可直接按已核销项目分账。",
        )

    if entered_at is None:
        today_closing = _today_closing_at(now_value, closing_time)
        if now_value < today_closing:
            return LifecycleSnapshot(
                status=LifecycleStatus.NOT_STARTED,
                reason=LifecycleReason.NOT_ENTERED,
                entered_at=None,
                exited_at=None,
                leave_at=None,
                refund_deadline=None,
                can_verify=True,
                can_refund=has_pending_items,
                can_split=False,
                message="门票尚未核销，生命周期未开始；核销门票后开始计算游园时间。",
            )
        return LifecycleSnapshot(
            status=LifecycleStatus.NOT_STARTED,
            reason=LifecycleReason.PARK_CLOSED,
            entered_at=None,
            exited_at=None,
            leave_at=None,
            refund_deadline=None,
            can_verify=False,
            can_refund=has_pending_items,
            can_split=False,
            message=(
                f"景区已闭园（{_format_park_time(today_closing)}），"
                "今日无法检票入园，请明日再来。"
            ),
        )

    entered_value = _as_utc_naive(entered_at)
    exited_value = None if exited_at is None else _as_utc_naive(exited_at)
    leave_at = _compute_leave_at(entered_value, exited_value, closing_time)

    if not has_pending_items:
        return LifecycleSnapshot(
            status=LifecycleStatus.COMPLETED,
            reason=LifecycleReason.ALL_ITEMS_SETTLED,
            entered_at=entered_value,
            exited_at=exited_value,
            leave_at=leave_at,
            refund_deadline=None,
            can_verify=False,
            can_refund=False,
            can_split=True,
            message=(
                "生命周期已结束：全部子项均已核销、已退款或已过期，可以进行分账。"
            ),
        )

    if refund_after_lifecycle:
        deadline = leave_at + timedelta(hours=refund_window_hours)
        if now_value >= deadline:
            return LifecycleSnapshot(
                status=LifecycleStatus.COMPLETED,
                reason=LifecycleReason.REFUND_WINDOW_EXPIRED,
                entered_at=entered_value,
                exited_at=exited_value,
                leave_at=leave_at,
                refund_deadline=deadline,
                can_verify=False,
                can_refund=False,
                can_split=True,
                message=(
                    "生命周期已结束：离园后退款窗口已于 "
                    f"{_format_park_time(deadline)} 到期。"
                ),
            )
        if now_value >= leave_at:
            return LifecycleSnapshot(
                status=LifecycleStatus.ACTIVE,
                reason=LifecycleReason.POST_LEAVE_REFUND_WINDOW,
                entered_at=entered_value,
                exited_at=exited_value,
                leave_at=leave_at,
                refund_deadline=deadline,
                can_verify=False,
                can_refund=True,
                can_split=False,
                message=(
                    "游客已离园或景区已闭园；未核销项目可退款至 "
                    f"{_format_park_time(deadline)}，"
                    "退款窗口结束或全部子项终态后方可分账。"
                ),
            )
        return LifecycleSnapshot(
            status=LifecycleStatus.ACTIVE,
            reason=LifecycleReason.IN_PARK,
            entered_at=entered_value,
            exited_at=exited_value,
            leave_at=leave_at,
            refund_deadline=deadline,
            can_verify=True,
            can_refund=True,
            can_split=False,
            message="游客在园内，生命周期进行中；离园或全部子项终态后方可分账。",
        )

    if now_value >= leave_at:
        return LifecycleSnapshot(
            status=LifecycleStatus.COMPLETED,
            reason=LifecycleReason.POST_LEAVE_REFUND_DISABLED,
            entered_at=entered_value,
            exited_at=exited_value,
            leave_at=leave_at,
            refund_deadline=None,
            can_verify=False,
            can_refund=False,
            can_split=True,
            message="生命周期已结束：游客已离园或景区已闭园，且园区配置为离园后不再退款。",
        )
    return LifecycleSnapshot(
        status=LifecycleStatus.ACTIVE,
        reason=LifecycleReason.IN_PARK,
        entered_at=entered_value,
        exited_at=exited_value,
        leave_at=leave_at,
        refund_deadline=None,
        can_verify=True,
        can_refund=True,
        can_split=False,
        message="游客在园内，生命周期进行中；离园或全部子项终态后方可分账。",
    )


def get_db():
    """FastAPI 依赖：每个请求创建一个 Session，请求结束后自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 路由函数中可直接写 db: DbSession，省去重复的 Depends(get_db)
DbSession = Annotated[Session, Depends(get_db)]


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class TimestampMixin:
    """公共时间戳字段：创建时间、更新时间（均由数据库生成）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )


# ===========================================================================
# 二、枚举定义
# ===========================================================================

class OrderStatus(str, PyEnum):
    """订单状态。数据库与 API 均使用英文编码，中文含义见注释。"""

    UNPAID = "unpaid"      # 未支付
    PAID = "paid"          # 已支付（未分账）
    SPLIT = "split"        # 已分账
    REFUNDED = "refunded"  # 已退款


class VerifyStatus(str, PyEnum):
    """核销 / 退款 / 过期状态。"""

    UNVERIFIED = "unverified"  # 未核销
    VERIFIED = "verified"      # 已核销
    REFUNDED = "refunded"      # 未核销项目已退款，不再可核销
    # 离园结算时，仍未被使用、也未退款的子项会被强制置为过期。
    # 过期项目永久不可核销、不可退款，对应金额留在平台侧。
    EXPIRED = "expired"        # 已过期（离园结算后作废）


class VerifyPost(str, PyEnum):
    """现场核销岗位。不同岗位只允许核销自己负责的项目。"""

    GATE = "gate"        # 景区检票员：核销门票
    COFFEE = "coffee"    # 咖啡店员：核销精品咖啡
    OFFROAD = "offroad"  # 越野车安全员：核销越野车体验


class LifecycleStatus(str, PyEnum):
    """订单的游园生命周期状态（派生状态，不落库）。"""

    NOT_APPLICABLE = "not_applicable"  # 订单不含门票
    NOT_STARTED = "not_started"        # 含门票但尚未入园
    ACTIVE = "active"                  # 已入园，游园进行中
    COMPLETED = "completed"            # 生命周期已结束，允许分账


class LifecycleReason(str, PyEnum):
    """生命周期状态的具体原因，用于前端文案与排查。"""

    NOT_APPLICABLE = "not_applicable"
    NOT_ENTERED = "not_entered"
    PARK_CLOSED = "park_closed"
    IN_PARK = "in_park"
    POST_LEAVE_REFUND_WINDOW = "post_leave_refund_window"
    ALL_ITEMS_SETTLED = "all_items_settled"
    REFUND_WINDOW_EXPIRED = "refund_window_expired"
    POST_LEAVE_REFUND_DISABLED = "post_leave_refund_disabled"


@dataclass(frozen=True)
class LifecycleSnapshot:
    """一次生命周期判定的完整快照。"""

    status: LifecycleStatus
    reason: LifecycleReason
    entered_at: datetime | None
    exited_at: datetime | None
    leave_at: datetime | None
    refund_deadline: datetime | None
    can_verify: bool
    can_refund: bool
    can_split: bool
    message: str


# ===========================================================================
# 三、ORM 模型（业务表 + 分账流水 / 项目快照）
# ===========================================================================

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


class Merchant(TimestampMixin, Base):
    """商户表。split_rate = 0.1 表示平台抽成 10%。"""

    __tablename__ = "merchants"
    __table_args__ = (
        CheckConstraint(
            "split_rate >= 0 AND split_rate <= 1",
            name="ck_merchant_split_rate_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True, comment="商户名称"
    )
    # 使用 Numeric 避免浮点误差；0.05 表示平台抽成 5%。
    split_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        nullable=False,
        default=Decimal("0"),
        comment="平台抽成比例，0 ~ 1",
    )
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

    # 一对多：一个商户拥有多个商品；删除商户时级联删除其商品。
    products: Mapped[list["Product"]] = relationship(
        back_populates="merchant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # 一对多：一个商户会产生多条核销记录。
    # 财务流水不允许随商户删除，数据库端使用 RESTRICT 保护。
    verify_records: Mapped[list["VerifyRecord"]] = relationship(
        back_populates="merchant",
        passive_deletes=True,
    )


class Product(TimestampMixin, Base):
    """商品 / 套餐表。is_combo=True 表示组合套餐。"""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_product_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 外键：商品归属的商户；商户删除时商品一并删除。
    merchant_id: Mapped[int] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属商户 ID",
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, comment="商品名称")
    price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        comment="售价（元）",
    )
    is_combo: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否组合套餐",
    )

    merchant: Mapped["Merchant"] = relationship(back_populates="products")


class Order(TimestampMixin, Base):
    """订单表。平台视角的交易主单，不直接绑定单个商户。"""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "total_amount >= 0",
            name="ck_order_total_amount_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 业务订单号：对外展示、对账使用，避免暴露自增主键。
    order_no: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True, comment="业务订单号"
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        comment="订单总金额（元）",
    )
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(
            OrderStatus,
            native_enum=False,          # SQLite 不支持原生 ENUM，用 VARCHAR + CHECK
            create_constraint=True,
            length=16,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
        default=OrderStatus.UNPAID,
        server_default=OrderStatus.UNPAID.value,
        index=True,
        comment="订单状态：unpaid/paid/split/refunded",
    )
    entered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="首次门票核销（入园）时间，naive UTC"
    )
    exited_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="出口扫码离园时间，naive UTC；为空表示未扫码离园"
    )

    # 一对多：一笔订单包含多条核销项；删除订单时级联删除核销记录。
    verify_records: Mapped[list["VerifyRecord"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="VerifyRecord.id",
    )
    # 一对一：分账流水（最多一条）。订单删除时一并删除。
    split_record: Mapped[Optional["SplitRecord"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class VerifyRecord(TimestampMixin, Base):
    """核销记录表。一行代表订单中的一个可核销项目。"""

    __tablename__ = "verify_records"
    __table_args__ = (
        # 同一订单、同一商户下的同名项目只允许一条记录，防止重复铺记录。
        UniqueConstraint(
            "order_id",
            "merchant_id",
            "item_name",
            name="uq_verify_order_merchant_item",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 外键：所属订单；订单删除时核销记录一并删除。
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="订单 ID",
    )
    # 外键：实际履约/核销的商户；已有核销流水的商户不允许被删除。
    merchant_id: Mapped[int] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="核销商户 ID",
    )
    item_name: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="核销项目名称，如：门票、咖啡"
    )
    verify_status: Mapped[VerifyStatus] = mapped_column(
        SAEnum(
            VerifyStatus,
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
            validate_strings=True,
        ),
        nullable=False,
        default=VerifyStatus.UNVERIFIED,
        server_default=VerifyStatus.UNVERIFIED.value,
        index=True,
        comment="核销状态：unverified/verified/refunded/expired",
    )

    order: Mapped["Order"] = relationship(back_populates="verify_records")
    merchant: Mapped["Merchant"] = relationship(back_populates="verify_records")


class SplitRecord(TimestampMixin, Base):
    """
    分账流水表。每笔订单最多只有一条成功分账流水。

    order_id 上的唯一约束是防重复分账的最后一道防线：
    即使并发请求同时通过了应用层检查，数据库也只会接受一条记录。
    """

    __tablename__ = "split_records"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_split_records_order_id"),
        CheckConstraint(
            "total_amount >= 0",
            name="ck_split_records_total_amount_non_negative",
        ),
        CheckConstraint(
            "split_amount >= 0",
            name="ck_split_records_split_amount_non_negative",
        ),
        CheckConstraint(
            "platform_retention >= 0",
            name="ck_split_records_platform_retention_non_negative",
        ),
        CheckConstraint(
            "merchant_income_total >= 0",
            name="ck_split_records_merchant_income_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="订单 ID（唯一，防止重复分账）",
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, comment="订单总额（元）"
    )
    split_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
        comment="实际参与分账的金额（已核销项目合计，元）",
    )
    platform_retention: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, comment="平台留存（元）"
    )
    merchant_income_total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, comment="商户实得合计（元）"
    )
    item_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="分账项目数"
    )

    order: Mapped["Order"] = relationship(back_populates="split_record")
    # 分账时的项目快照：商户名、抽成比例、金额均固化，后续商户改价不影响历史对账。
    items: Mapped[list["SplitItemRecord"]] = relationship(
        back_populates="split_record",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SplitItemRecord.id",
    )


class SplitItemRecord(TimestampMixin, Base):
    """分账项目快照。每行对应分账时订单中的一个核销项目。"""

    __tablename__ = "split_record_items"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_split_item_amount_non_negative"),
        CheckConstraint(
            "platform_commission >= 0",
            name="ck_split_item_commission_non_negative",
        ),
        CheckConstraint(
            "merchant_income >= 0",
            name="ck_split_item_income_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    split_record_id: Mapped[int] = mapped_column(
        ForeignKey("split_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="分账流水 ID",
    )
    # 快照字段：不设外键，避免核销记录变动影响历史分账数据。
    verify_record_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="核销记录 ID（快照）"
    )
    item_name: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="项目名称（快照）"
    )
    merchant_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="商户 ID（快照）"
    )
    merchant_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="商户名称（快照）"
    )
    verify_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="核销状态（快照）"
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, comment="项目金额（元）"
    )
    split_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, comment="平台抽成比例（快照）"
    )
    platform_commission: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, comment="平台抽成（元）"
    )
    merchant_income: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, comment="商户实得（元）"
    )

    split_record: Mapped["SplitRecord"] = relationship(back_populates="items")


# ===========================================================================
# 四、Pydantic 响应模型
# ===========================================================================

class MerchantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    split_rate: float
    open_time: str
    close_time: str
    created_at: datetime


class MerchantHoursUpdate(BaseModel):
    open_time: str = Field(max_length=8)
    close_time: str = Field(max_length=8)

    @field_validator("open_time", "close_time")
    @classmethod
    def _valid_clock(cls, value: str) -> str:
        if _parse_clock_time(value) is None:
            raise ValueError("时间格式应为 HH:MM 或 HH:MM:SS")
        return value.strip()


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    merchant_id: int
    name: str
    price: float
    is_combo: bool
    created_at: datetime


class VerifyRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    merchant_id: int
    item_name: str
    verify_status: VerifyStatus
    created_at: datetime
    updated_at: datetime


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: str
    total_amount: float
    status: OrderStatus
    created_at: datetime
    entered_at: datetime | None = None
    exited_at: datetime | None = None
    verify_records: list[VerifyRecordOut] = Field(default_factory=list)


class ParkSettingsOut(BaseModel):
    refund_after_lifecycle: bool
    refund_window_hours: int


class ParkSettingsUpdate(BaseModel):
    refund_after_lifecycle: bool
    refund_window_hours: int = Field(ge=0, le=8760)


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


# ===========================================================================
# 五、接口签名验证（HMAC-SHA256）
# ===========================================================================

DEFAULT_SIGNING_SECRET = "tourism-mp-demo-secret"
SIGNING_SECRET = os.getenv("API_SIGNING_SECRET", DEFAULT_SIGNING_SECRET)
SIGNATURE_TTL_SECONDS = int(os.getenv("API_SIGNATURE_TTL_SECONDS", "300"))

if SIGNING_SECRET == DEFAULT_SIGNING_SECRET:
    logger.warning(
        "当前使用默认签名密钥，仅适用于本地演示；"
        "生产环境请设置环境变量 API_SIGNING_SECRET。"
    )


def _body_sha256(body: bytes) -> str:
    """请求体摘要：空请求体同样参与签名，防止 body 被篡改。"""
    return hashlib.sha256(body).hexdigest()


def _canonical_string(timestamp: str, method: str, path: str, body: bytes) -> str:
    """构造签名原文，客户端与服务端必须完全一致。"""
    return f"{timestamp}\n{method.upper()}\n{path}\n{_body_sha256(body)}"


def _sign(secret: str, canonical: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signature_headers(
    method: str,
    path: str,
    body: bytes | str = b"",
    *,
    secret: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    """
    生成调用受保护接口所需的签名请求头（便于调试、联调与自动化测试）。

    path 需包含 query string（如有），例如 "/api/order/1/split?dry_run=1"。
    """
    if isinstance(body, str):
        body = body.encode("utf-8")

    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    canonical = _canonical_string(ts, method, path, body)
    signature = _sign(secret or SIGNING_SECRET, canonical)
    return {"X-Timestamp": ts, "X-Signature": signature}


async def require_signature(
    request: Request,
    x_timestamp: Annotated[
        str | None,
        Header(alias="X-Timestamp", description="Unix 秒级时间戳"),
    ] = None,
    x_signature: Annotated[
        str | None,
        Header(alias="X-Signature", description="HMAC-SHA256 十六进制签名"),
    ] = None,
) -> None:
    """
    FastAPI 依赖：校验 X-Timestamp + X-Signature。

    任何失败（缺少请求头、时间戳非法/过期、签名不匹配）统一返回 403，
    且不向客户端泄露期望签名。
    """
    if not x_timestamp or not x_signature:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="签名验证失败：缺少 X-Timestamp 或 X-Signature 请求头",
        )

    try:
        timestamp_value = int(x_timestamp)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="签名验证失败：X-Timestamp 必须为 Unix 秒级整数时间戳",
        )

    now = int(time.time())
    if abs(now - timestamp_value) > SIGNATURE_TTL_SECONDS:
        logger.warning(
            "签名时间戳超出允许窗口：path=%s, timestamp=%s, now=%s, ttl=%s",
            request.url.path,
            timestamp_value,
            now,
            SIGNATURE_TTL_SECONDS,
        )
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=f"签名验证失败：X-Timestamp 已过期（允许 ±{SIGNATURE_TTL_SECONDS} 秒）",
        )

    # Starlette 会缓存请求体，依赖中读取不会影响后续处理。
    body = await request.body()
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"

    expected = _sign(
        SIGNING_SECRET,
        _canonical_string(x_timestamp, request.method, path, body),
    )

    provided = x_signature.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    provided = provided.strip().lower()

    if not hmac.compare_digest(expected, provided):
        logger.warning(
            "签名不匹配：method=%s, path=%s",
            request.method,
            path,
        )
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="签名验证失败：X-Signature 不匹配",
        )


# 受保护接口直接把该类型写进函数参数即可：_: RequireSignature
RequireSignature = Annotated[None, Depends(require_signature)]


# ===========================================================================
# 六、Decimal 金额工具与分账 / 退款计算
# ===========================================================================

MONEY_QUANT = Decimal("0.01")
RATE_QUANT = Decimal("0.0001")
ZERO = Decimal("0.00")
# 所有未核销项目的退款手续费统一为 10%。
REFUND_FEE_RATE = Decimal("0.10")
# 兼容旧代码中使用的常量名（语义已扩展为“所有项目统一费率”）。
COFFEE_FEE_RATE = REFUND_FEE_RATE


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    """统一转换为 Decimal；float 先转字符串，避免二进制浮点误差。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value: Decimal | int | float | str) -> Decimal:
    """金额量化为两位小数（四舍五入）。所有对外金额都应经过此函数。"""
    return _to_decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _format_money(value: Decimal) -> str:
    """JSON 中以固定两位小数字符串输出金额，彻底避免浮点精度问题。"""
    return format(_money(value), "f")


def _format_rate(value: Decimal) -> str:
    """抽成比例规范化输出，例如 0.0500 -> "0.05"。"""
    return format(
        _to_decimal(value).quantize(RATE_QUANT, rounding=ROUND_HALF_UP).normalize(),
        "f",
    )


# 金额字段：Python 中始终是 Decimal，JSON 中序列化为两位小数字符串。
Money = Annotated[
    Decimal,
    PlainSerializer(_format_money, return_type=str, when_used="json"),
]
Rate = Annotated[
    Decimal,
    PlainSerializer(_format_rate, return_type=str, when_used="json"),
]


class SplitItemOut(BaseModel):
    """分账明细：订单中的单个核销项目。"""

    verify_record_id: int
    item_name: str
    merchant_id: int
    merchant_name: str
    verify_status: VerifyStatus
    # 只有已核销项目会进入分账池；未核销 / 已退款项目的金额固定为 0。
    split_eligible: bool
    amount: Money
    split_rate: Rate
    platform_commission: Money
    merchant_income: Money


class SplitMerchantOut(BaseModel):
    """分账汇总：按商户聚合。"""

    merchant_id: int
    merchant_name: str
    split_rate: Rate
    item_names: list[str] = Field(default_factory=list)
    gross_amount: Money
    platform_commission: Money
    merchant_income: Money


class SplitResultOut(BaseModel):
    """订单分账结果。"""

    order_id: int
    order_no: str
    order_status: OrderStatus
    # 订单总额：订单原始成交金额。
    total_amount: Money
    # 分账基数：仅已核销项目的金额合计（未核销 / 已退款项目记 0）。
    split_amount: Money
    # 平台留存 = 已核销项目的抽成 + 已过期项目的全额留存。
    platform_retention: Money
    merchant_income_total: Money
    # 离园结算后未使用（expired）子项的金额合计，这部分钱全部留在平台。
    forfeited_amount: Money = ZERO
    merchants: list[SplitMerchantOut] = Field(default_factory=list)
    items: list[SplitItemOut] = Field(default_factory=list)


class SplitRecordSummaryOut(BaseModel):
    """分账流水摘要（列表查询使用）。"""

    split_record_id: int
    order_id: int
    order_no: str
    order_status: OrderStatus
    total_amount: Money
    split_amount: Money
    platform_retention: Money
    merchant_income_total: Money
    item_count: int
    created_at: datetime


class SplitRecordOut(SplitResultOut):
    """分账流水详情（含落库快照）。"""

    split_record_id: int
    created_at: datetime


class SplitRecordPageOut(BaseModel):
    """分账流水分页结果。"""

    total: int
    page: int
    page_size: int
    items: list[SplitRecordSummaryOut] = Field(default_factory=list)


class ExitSettlementOut(SplitResultOut):
    """离园结算结果：过期处理 + 一次性分账。"""

    exited_at: datetime | None = None
    expired_item_count: int = 0
    expired_amount: Money = ZERO
    message: str = ""


class RefundItemOut(BaseModel):
    """智能退款明细：每个核销项目的处理结论。"""

    verify_record_id: int
    item_name: str
    merchant_id: int
    merchant_name: str
    verify_status: VerifyStatus
    # 前端可直接展示的中文状态与处理结论，避免各端重复映射。
    status_label: str
    result_label: str
    refundable: bool
    gross_amount: Money
    fee_rate: Rate
    fee_amount: Money
    refund_amount: Money
    reason: str


class RefundResultOut(BaseModel):
    """智能退款结果。"""

    order_id: int
    order_no: str
    order_status: OrderStatus
    refunded: bool
    refunded_item_count: int
    rejected_item_count: int
    refund_gross_amount: Money
    fee_amount: Money
    refund_amount: Money
    items: list[RefundItemOut] = Field(default_factory=list)
    message: str


class DemoResetItemOut(BaseModel):
    """重置演示环境后，新订单中的单个核销项目。"""

    verify_record_id: int
    item_name: str
    merchant_id: int
    merchant_name: str
    verify_status: VerifyStatus


class DemoResetResultOut(BaseModel):
    """一键重置演示环境的结果。"""

    order_id: int
    order_no: str
    # 门票核销记录 ID：前端可用它生成新的电子票二维码（TK-xxxx）。
    ticket_record_id: int
    total_amount: Money
    order_status: OrderStatus
    items: list[DemoResetItemOut] = Field(default_factory=list)
    message: str


def _load_order_with_records(db: Session, order_id: int) -> Order:
    """加载订单及其核销记录、核销商户，避免响应阶段的 N+1 查询。"""
    order = db.scalar(
        select(Order)
        .options(
            selectinload(Order.verify_records).selectinload(VerifyRecord.merchant)
        )
        .where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="订单不存在",
        )
    return order


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


def _resolve_order_close_time(db: Session, order: Order) -> dt_time:
    """整单闭园时间取订单门票所属商户；缺失/非法时回退默认 23:59:59。"""
    ticket = next(
        (record for record in order.verify_records if "门票" in record.item_name),
        None,
    )
    merchant = db.get(Merchant, ticket.merchant_id) if ticket is not None else None
    parsed = _parse_clock_time(merchant.close_time) if merchant is not None else None
    return parsed or DEFAULT_CLOSE_TIME_VALUE


def _order_lifecycle(
    order: Order,
    settings: ParkSettings,
    *,
    now: datetime | None = None,
    close_time: dt_time | None = None,
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
        close_time=close_time,
    )


def _verify_blocked_message(snapshot: LifecycleSnapshot) -> str:
    if snapshot.status == LifecycleStatus.NOT_STARTED:
        return snapshot.message
    return "本次游园已结束，如需再次入园请重新购票。"


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


def _refund_policy_block_reason(
    order: Order,
    settings: ParkSettings,
    close_time: dt_time | None = None,
    now: datetime | None = None,
) -> str | None:
    """
    园区退款政策的最高优先级校验。

    只要园区配置为“离园后不允许退款”，游客一旦离园（或到达闭园时刻），
    未核销项目就立即不可退款。该判断必须先于任何退款窗口 / 生命周期
    分支执行，确保历史配置或脏数据不会让 72 小时窗口“复活”。
    """
    if bool(settings.refund_after_lifecycle):
        return None
    if order.entered_at is None:
        # 尚未入园：离园政策不生效，仍按未使用订单处理。
        return None

    now_value = _utcnow_naive() if now is None else _as_utc_naive(now)
    leave_at = _compute_leave_at(order.entered_at, order.exited_at, close_time)
    if leave_at is not None and now_value >= leave_at:
        return (
            "园区规定离园后不允许退款：游客已离园或景区已闭园，"
            "未核销项目不可退款。"
        )
    return None


# 岗位 -> 该岗位负责核销的项目关键字（与现场业务口径保持一致）。
VERIFY_POST_ITEM_KEYWORDS: dict[VerifyPost, str] = {
    VerifyPost.GATE: "门票",
    VerifyPost.COFFEE: "咖啡",
    VerifyPost.OFFROAD: "越野车",
}


def _is_combo_record(item_name: str) -> bool:
    """历史订单可能把套餐写成单条组合记录（如“门票+咖啡”），需要特殊兼容。"""
    return "门票" in item_name and (
        "咖啡" in item_name or "越野车" in item_name
    )


def _select_record_for_post(order: Order, post: VerifyPost) -> VerifyRecord:
    """
    按现场岗位从订单中选出本次应核销的记录。

    业务规则：
      1. 景区检票员只核销“门票”；
      2. 咖啡店员只核销“咖啡”，越野车安全员只核销“越野车”；
      3. 非检票岗位必须先完成门票核销，否则提示游客先去检票口；
      4. 对应项目已核销时按重复扫码处理，绝不顺带核销其他项目。
    """
    # 只允许使用当前订单自己的核销记录，防止其他订单 / 历史脏数据影响判断。
    records = [r for r in order.verify_records if r.order_id == order.id]
    if not records:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="订单没有可核销项目",
        )

    keyword = VERIFY_POST_ITEM_KEYWORDS[post]

    # 门票是所有后续项目的前置条件。订单本身不含门票时不做限制
    # （例如未来单独售卖咖啡券），含门票但未核销则明确拦下。
    if post is not VerifyPost.GATE:
        ticket_records = [r for r in records if "门票" in r.item_name]
        if ticket_records and not any(
            r.verify_status == VerifyStatus.VERIFIED for r in ticket_records
        ):
            if any(
                r.verify_status == VerifyStatus.REFUNDED for r in ticket_records
            ):
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail="门票已退款，无法核销其他项目",
                )
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="门票未核销，请先去检票口",
            )

    candidates = [r for r in records if keyword in r.item_name]
    if not candidates:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"该订单不含「{keyword}」项目",
        )

    # 纯单项记录优先于历史组合记录，再按创建顺序取第一条。
    candidates.sort(key=lambda r: (1 if _is_combo_record(r.item_name) else 0, r.id))
    pending = [r for r in candidates if r.verify_status == VerifyStatus.UNVERIFIED]
    if pending:
        target = pending[0]
    else:
        # 没有待核销记录时，优先按“已核销”给出重复扫码提示；
        # 若全部已退款，则明确提示不可核销。
        target = next(
            (
                r
                for r in candidates
                if r.verify_status == VerifyStatus.VERIFIED
            ),
            candidates[0],
        )

    if target.verify_status == VerifyStatus.VERIFIED:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="该票已核销，请勿重复扫码！",
        )
    if target.verify_status == VerifyStatus.REFUNDED:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="该票已退款，不可再核销",
        )
    if target.verify_status == VerifyStatus.EXPIRED:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="本次游园已结束，未使用项目已过期，如需游玩请重新购票",
        )
    return target


def _resolve_reference_price(item_name: str, products: list[Product]) -> Decimal | None:
    """
    推断某个核销项目的参考单价：

    1. 优先取同商户下商品名完全一致的价格；
    2. 其次取名称互相包含的商品（如“门票”匹配“单人门票”），并优先纯单项
       商品，避免“门票”误匹配到“门票+咖啡+越野车套餐”的高价；
    3. 找不到则返回 None，由分配算法把订单剩余金额分给它。
    """
    for product in products:
        if product.name == item_name:
            return _to_decimal(product.price)

    containment_matches = [
        product
        for product in products
        if item_name in product.name or product.name in item_name
    ]
    containment_matches.sort(
        key=lambda product: (1 if product.is_combo else 0, product.id)
    )
    if containment_matches:
        return _to_decimal(containment_matches[0].price)
    return None


def _distribute_remainder(amounts: dict[int, Decimal], target: Decimal) -> None:
    """把量化产生的分币差额补到最后一项，保证分项金额之和严格等于订单总额。"""
    if not amounts:
        return
    diff = _money(target - sum(amounts.values(), ZERO))
    if diff == ZERO:
        return
    last_id = next(reversed(amounts))
    amounts[last_id] = _money(amounts[last_id] + diff)


def _equal_split(record_ids: list[int], target: Decimal) -> dict[int, Decimal]:
    """按项数均分；使用 ROUND_DOWN 确保不会超额，余数补到最后一项。"""
    if not record_ids:
        return {}
    base = (target / Decimal(len(record_ids))).quantize(
        MONEY_QUANT, rounding=ROUND_DOWN
    )
    result = {record_id: base for record_id in record_ids}
    _distribute_remainder(result, target)
    return result


def _proportional_split(
    weights: dict[int, Decimal], target: Decimal
) -> dict[int, Decimal]:
    """按参考单价比例分摊订单总额，同样保证分项之和等于 target。"""
    total_weight = sum(weights.values(), ZERO)
    if total_weight <= ZERO:
        return _equal_split(list(weights), target)

    result: dict[int, Decimal] = {}
    for record_id, weight in weights.items():
        result[record_id] = (target * weight / total_weight).quantize(
            MONEY_QUANT, rounding=ROUND_DOWN
        )
    _distribute_remainder(result, target)
    return result


def allocate_item_amounts(db: Session, order: Order) -> dict[int, Decimal]:
    """
    将订单总额分配给各核销项目（现有表结构没有“项目金额”字段，因此按规则推导）。

    规则：
      - 只有一个项目：该项目承接订单全额；
      - 多个项目且都能匹配到商品参考价：按参考价比例分摊（参考价之和与
        订单总额不一致时以订单总额为准）；
      - 部分项目能匹配到参考价：先按参考价分配，余额由其余项目均分；
      - 都匹配不到：均分。

    返回值 key 为 verify_records.id，所有金额均为两位小数 Decimal，
    且各项之和严格等于订单总额。
    """
    records = list(order.verify_records)
    if not records:
        return {}

    total = _money(order.total_amount)
    if len(records) == 1:
        return {records[0].id: total}

    merchant_ids = {record.merchant_id for record in records}
    products_by_merchant: dict[int, list[Product]] = defaultdict(list)
    products = db.scalars(
        select(Product).where(Product.merchant_id.in_(merchant_ids))
    ).all()
    for product in products:
        products_by_merchant[product.merchant_id].append(product)

    references: dict[int, Decimal | None] = {
        record.id: _resolve_reference_price(
            record.item_name,
            products_by_merchant.get(record.merchant_id, []),
        )
        for record in records
    }
    known = {
        record_id: price
        for record_id, price in references.items()
        if price is not None and price > ZERO
    }
    unknown_ids = [
        record.id
        for record in records
        if references[record.id] is None or references[record.id] <= ZERO
    ]
    known_total = sum(known.values(), ZERO)

    if not known:
        # 没有任何参考价：均分。
        allocated = _equal_split([record.id for record in records], total)
    elif unknown_ids:
        if known_total >= total:
            # 参考价之和已超过订单总额，按比例压缩，未知项为 0。
            scaled = _proportional_split(known, total)
            allocated = {
                record.id: scaled.get(record.id, ZERO) for record in records
            }
        else:
            # 已知项按参考价，剩余金额由未知项均分。
            allocated = {
                record_id: _money(price) for record_id, price in known.items()
            }
            residual = _money(total - sum(allocated.values(), ZERO))
            allocated.update(_equal_split(unknown_ids, residual))
    else:
        # 所有项目都有参考价。
        if known_total == total:
            allocated = {
                record_id: _money(price) for record_id, price in known.items()
            }
        else:
            allocated = _proportional_split(known, total)

    # 统一按核销记录顺序输出，并补齐最后的分币差额。
    ordered = {
        record.id: _money(allocated.get(record.id, ZERO)) for record in records
    }
    _distribute_remainder(ordered, total)
    return ordered


def _aggregate_split_merchants(items: list[SplitItemOut]) -> list[SplitMerchantOut]:
    """把项目级分账明细按商户聚合，供实时计算与历史流水查询共用。

    只有进入分账池（已核销且金额大于 0）的项目才参与商户聚合：
    未核销 / 已退款 / 已过期项目虽然保留在项目快照中，但都不产生商户结算。
    """
    merchant_buckets: dict[int, dict] = {}

    for item in items:
        if not item.split_eligible or item.amount <= ZERO:
            continue
        bucket = merchant_buckets.get(item.merchant_id)
        if bucket is None:
            bucket = {
                "merchant_id": item.merchant_id,
                "merchant_name": item.merchant_name,
                "split_rate": item.split_rate,
                "item_names": [],
                "gross_amount": ZERO,
                "platform_commission": ZERO,
                "merchant_income": ZERO,
            }
            merchant_buckets[item.merchant_id] = bucket

        bucket["item_names"].append(item.item_name)
        bucket["gross_amount"] += item.amount
        bucket["platform_commission"] += item.platform_commission
        bucket["merchant_income"] += item.merchant_income

    return [
        SplitMerchantOut(
            merchant_id=bucket["merchant_id"],
            merchant_name=bucket["merchant_name"],
            split_rate=bucket["split_rate"],
            item_names=bucket["item_names"],
            gross_amount=_money(bucket["gross_amount"]),
            platform_commission=_money(bucket["platform_commission"]),
            merchant_income=_money(bucket["merchant_income"]),
        )
        for bucket in merchant_buckets.values()
    ]


def _build_split_result(db: Session, order: Order) -> SplitResultOut:
    """
    计算订单分账，支持“部分核销 + 部分退款”的订单。

    分账池只包含状态为“已核销”的子项：
      - 已核销：按项目分摊金额参与分账，平台按商户 split_rate 抽成，
        商户实得 = 项目金额 - 平台抽成；
      - 已过期（expired）：离园结算时强制作废的未使用金额，不参与商户
        分账，全额计入平台留存（platform_commission = 项目金额）；
      - 未核销 / 已退款：项目金额、平台抽成、商户实得全部记为 0，
        仍保留在项目快照中供对账审计。

    因此：platform_retention = Σ已核销抽成 + Σ已过期金额；
          split_amount        = Σ已核销项目金额；
          forfeited_amount    = Σ已过期项目金额（平台留存的一部分）。
    """
    amounts = allocate_item_amounts(db, order)

    items: list[SplitItemOut] = []
    for record in order.verify_records:
        rate = _to_decimal(record.merchant.split_rate)
        allocated = _money(amounts[record.id])

        if record.verify_status == VerifyStatus.VERIFIED:
            # 已核销：正常进入分账池。
            split_eligible = True
            amount = allocated
            commission = _money(amount * rate)
            merchant_income = _money(amount - commission)
        elif record.verify_status == VerifyStatus.EXPIRED:
            # 已过期：未使用的钱不退给游客，也不分给商户，整笔留在平台。
            split_eligible = False
            amount = allocated
            commission = amount
            merchant_income = ZERO
        else:
            # 未核销 / 已退款：不参与分账，金额记 0。
            split_eligible = False
            amount = ZERO
            commission = ZERO
            merchant_income = ZERO

        items.append(
            SplitItemOut(
                verify_record_id=record.id,
                item_name=record.item_name,
                merchant_id=record.merchant_id,
                merchant_name=record.merchant.name,
                verify_status=record.verify_status,
                split_eligible=split_eligible,
                amount=amount,
                split_rate=rate,
                platform_commission=commission,
                merchant_income=merchant_income,
            )
        )

    # 分账基数只统计真正进入分账池的已核销项目；过期金额单独计入平台留存。
    split_amount = _money(
        sum((item.amount for item in items if item.split_eligible), ZERO)
    )
    forfeited_amount = _money(
        sum(
            (
                item.amount
                for item in items
                if item.verify_status == VerifyStatus.EXPIRED
            ),
            ZERO,
        )
    )
    platform_retention = _money(sum((item.platform_commission for item in items), ZERO))
    merchant_income_total = _money(
        sum((item.merchant_income for item in items), ZERO)
    )

    return SplitResultOut(
        order_id=order.id,
        order_no=order.order_no,
        order_status=order.status,
        total_amount=_money(order.total_amount),
        split_amount=split_amount,
        platform_retention=platform_retention,
        merchant_income_total=merchant_income_total,
        forfeited_amount=forfeited_amount,
        merchants=_aggregate_split_merchants(items),
        items=items,
    )


def _to_split_record_out(record: SplitRecord) -> SplitRecordOut:
    """把落库的分账流水还原为与实时计算一致的响应结构。"""
    items = [
        SplitItemOut(
            verify_record_id=item.verify_record_id,
            item_name=item.item_name,
            merchant_id=item.merchant_id,
            merchant_name=item.merchant_name,
            verify_status=VerifyStatus(item.verify_status),
            split_eligible=VerifyStatus(item.verify_status) == VerifyStatus.VERIFIED,
            amount=_money(item.amount),
            split_rate=_to_decimal(item.split_rate),
            platform_commission=_money(item.platform_commission),
            merchant_income=_money(item.merchant_income),
        )
        for item in record.items
    ]

    forfeited_amount = _money(
        sum(
            (
                item.amount
                for item in items
                if item.verify_status == VerifyStatus.EXPIRED
            ),
            ZERO,
        )
    )

    return SplitRecordOut(
        split_record_id=record.id,
        order_id=record.order_id,
        order_no=record.order.order_no,
        order_status=record.order.status,
        total_amount=_money(record.total_amount),
        split_amount=_money(record.split_amount),
        platform_retention=_money(record.platform_retention),
        merchant_income_total=_money(record.merchant_income_total),
        forfeited_amount=forfeited_amount,
        merchants=_aggregate_split_merchants(items),
        items=items,
        created_at=record.created_at,
    )


def _to_split_record_summary(record: SplitRecord) -> SplitRecordSummaryOut:
    return SplitRecordSummaryOut(
        split_record_id=record.id,
        order_id=record.order_id,
        order_no=record.order.order_no,
        order_status=record.order.status,
        total_amount=_money(record.total_amount),
        split_amount=_money(record.split_amount),
        platform_retention=_money(record.platform_retention),
        merchant_income_total=_money(record.merchant_income_total),
        item_count=record.item_count,
        created_at=record.created_at,
    )


def _refund_fee_rate(_item_name: str) -> Decimal:
    """
    退款手续费策略：所有品类的未核销项目统一收取 10% 手续费。

    门票 / 咖啡 / 越野车不再区分费率；已核销项目无论品类都不可退款。
    item_name 参数保留，便于未来按品类扩展差异化费率。
    """
    return REFUND_FEE_RATE


def _build_refund_result(db: Session, order: Order) -> RefundResultOut:
    """
    逐个核销项目判断是否可退，并计算手续费与实退金额。

    核心规则：
      - 未核销：按项目原始单价退款，扣除 10% 手续费，退款后状态标记为已退款；
      - 已核销：拒绝退款，退款金额固定为 0（仍可参与后续分账）；
      - 已退款：幂等展示，退款金额固定为 0；
      - 已过期：拒绝退款，金额已在离园结算时留在平台；
      - 全部已核销 / 已退款 / 已过期：refunded=False，由接口返回 409。
    """
    amounts = allocate_item_amounts(db, order)
    items: list[RefundItemOut] = []

    for record in order.verify_records:
        gross_amount = amounts[record.id]

        if record.verify_status == VerifyStatus.VERIFIED:
            fee_rate = ZERO
            refundable = False
            fee_amount = ZERO
            refund_amount = ZERO
            status_label = "已用"
            result_label = "拒绝退款"
            reason = (
                f"「{record.item_name}」已核销（已用），拒绝退款，退款 0 元"
            )
        elif record.verify_status == VerifyStatus.REFUNDED:
            fee_rate = ZERO
            refundable = False
            fee_amount = ZERO
            refund_amount = ZERO
            status_label = "已退款"
            result_label = "已退款"
            reason = (
                f"「{record.item_name}」此前已退款，本次不重复退款，退款 0 元"
            )
        elif record.verify_status == VerifyStatus.EXPIRED:
            # 离园结算后，未使用子项已作废，金额已按规则留在平台；
            # 过期项目不可退款，避免结算后再次产生资金动作。
            fee_rate = ZERO
            refundable = False
            fee_amount = ZERO
            refund_amount = ZERO
            status_label = "已过期"
            result_label = "不可退款"
            reason = (
                f"「{record.item_name}」已在离园结算时过期，不可退款，退款 0 元"
            )
        else:
            fee_rate = _refund_fee_rate(record.item_name)
            refundable = True
            fee_amount = _money(gross_amount * fee_rate)
            refund_amount = _money(gross_amount - fee_amount)
            status_label = "未用"
            result_label = "已退款"
            reason = (
                f"「{record.item_name}」未核销（未用），按原价 "
                f"{_format_money(gross_amount)} 元退款，扣除 10% 手续费 "
                f"{_format_money(fee_amount)} 元，实退 "
                f"{_format_money(refund_amount)} 元"
            )

        items.append(
            RefundItemOut(
                verify_record_id=record.id,
                item_name=record.item_name,
                merchant_id=record.merchant_id,
                merchant_name=record.merchant.name,
                verify_status=record.verify_status,
                status_label=status_label,
                result_label=result_label,
                refundable=refundable,
                gross_amount=gross_amount,
                fee_rate=fee_rate,
                fee_amount=fee_amount,
                refund_amount=refund_amount,
                reason=reason,
            )
        )

    refunded_items = [item for item in items if item.refundable]
    rejected_items = [item for item in items if not item.refundable]
    refund_gross_amount = _money(
        sum((item.gross_amount for item in refunded_items), ZERO)
    )
    fee_amount = _money(sum((item.fee_amount for item in refunded_items), ZERO))
    refund_amount = _money(sum((item.refund_amount for item in refunded_items), ZERO))
    refunded = bool(refunded_items)

    if not refunded:
        message = (
            "所有项目均已核销、已退款或已过期，不可再退款，各项退款 0 元。"
        )
    elif not rejected_items:
        message = (
            f"全部项目均未核销：按原价 {_format_money(refund_gross_amount)} 元"
            f"退款，统一扣除 10% 手续费 {_format_money(fee_amount)} 元，"
            f"实退 {_format_money(refund_amount)} 元。"
        )
    else:
        message = (
            f"部分退款：{len(refunded_items)} 项未核销已退款，"
            f"实退 {_format_money(refund_amount)} 元"
            f"（手续费 {_format_money(fee_amount)} 元）；"
            f"{len(rejected_items)} 项已核销或已退款，退款 0 元。"
        )

    return RefundResultOut(
        order_id=order.id,
        order_no=order.order_no,
        # 只要产生退款，订单即置为 refunded；具体被退款的子项由
        # verify_records.verify_status = refunded 单独记录。
        order_status=OrderStatus.REFUNDED if refunded else order.status,
        refunded=refunded,
        refunded_item_count=len(refunded_items),
        rejected_item_count=len(rejected_items),
        refund_gross_amount=refund_gross_amount,
        fee_amount=fee_amount,
        refund_amount=refund_amount,
        items=items,
        message=message,
    )


# ===========================================================================
# 七、测试数据初始化（幂等：重复启动不会产生重复数据）
# ===========================================================================

DEMO_ORDER_NO = "DEMO20260101000001"
DEMO_ORDER_AMOUNT = Decimal("200.00")
DEMO_ITEMS: tuple[tuple[str, str], ...] = (
    ("门票", "青山景区票务"),
    ("咖啡", "云顶咖啡"),
    ("越野车", "极速越野"),
)


def _get_or_create_merchant(
    db: Session, name: str, split_rate: Decimal
) -> Merchant:
    """按名称查找商户，不存在则创建；已存在时同步演示抽成比例。"""
    merchant = db.scalar(select(Merchant).where(Merchant.name == name))
    if merchant is None:
        merchant = Merchant(name=name, split_rate=split_rate)
        db.add(merchant)
        db.flush()  # 立即拿到自增主键
        logger.info("初始化商户：%s（抽成 %s）", name, split_rate)
    elif _to_decimal(merchant.split_rate) != split_rate:
        logger.info(
            "同步商户抽成：%s（%s -> %s）",
            name,
            merchant.split_rate,
            split_rate,
        )
        merchant.split_rate = split_rate
        db.flush()
    return merchant


def _get_or_create_product(
    db: Session,
    merchant: Merchant,
    name: str,
    price: Decimal,
    is_combo: bool,
    *,
    sync_price: bool = False,
) -> Product:
    """按“商户 + 商品名”查找商品，不存在则创建。"""
    product = db.scalar(
        select(Product).where(
            Product.merchant_id == merchant.id,
            Product.name == name,
        )
    )
    if product is None:
        product = Product(
            merchant_id=merchant.id,
            name=name,
            price=price,
            is_combo=is_combo,
        )
        db.add(product)
        db.flush()
        logger.info("初始化商品：%s（%.2f 元）", name, price)
    elif sync_price and (
        _to_decimal(product.price) != price or product.is_combo != is_combo
    ):
        logger.info(
            "同步商品：%s（%s 元 -> %s 元）",
            name,
            product.price,
            price,
        )
        product.price = price
        product.is_combo = is_combo
        db.flush()
    return product


DEMO_COMBO_NAME = "门票+咖啡+越野车套餐"
LEGACY_COMBO_NAME = "门票+咖啡套餐"


def _upgrade_demo_combo_product(db: Session, ticket_merchant: Merchant) -> Product:
    """把旧版“门票+咖啡套餐”升级为三项套餐，保持历史数据可用。"""
    combo = db.scalar(
        select(Product).where(
            Product.merchant_id == ticket_merchant.id,
            Product.name == DEMO_COMBO_NAME,
        )
    )
    legacy = db.scalar(
        select(Product).where(
            Product.merchant_id == ticket_merchant.id,
            Product.name == LEGACY_COMBO_NAME,
        )
    )
    if legacy is not None:
        if combo is not None and combo.id != legacy.id:
            # 已经存在新套餐时移除旧记录，避免名称包含关系导致参考价重复匹配。
            db.delete(legacy)
            db.flush()
        else:
            legacy.name = DEMO_COMBO_NAME
            legacy.price = DEMO_ORDER_AMOUNT
            legacy.is_combo = True
            db.flush()

    return _get_or_create_product(
        db,
        ticket_merchant,
        DEMO_COMBO_NAME,
        DEMO_ORDER_AMOUNT,
        is_combo=True,
        sync_price=True,
    )


def _upgrade_demo_order(
    db: Session,
    order: Order,
    item_merchants: dict[str, Merchant],
) -> None:
    """
    幂等升级演示订单：

    - 仅处理已支付订单，避免篡改已退款 / 已分账的历史流水；
    - 订单总额同步为 200 元；
    - 补齐缺失的门票 / 咖啡 / 越野车核销记录（已存在则不重复创建）；
    - 纯单项记录归属到对应商户，兼容旧版套餐记录的归属。
    """
    if order.status != OrderStatus.PAID:
        return

    if _to_decimal(order.total_amount) != DEMO_ORDER_AMOUNT:
        order.total_amount = DEMO_ORDER_AMOUNT

    covered: set[str] = set()
    for record in order.verify_records:
        matches = [
            name for name in item_merchants if name in record.item_name
        ]
        covered.update(matches)
        # 组合记录可能同时命中多个关键词，保持原商户不做拆分；
        # 纯单项记录则同步到对应的业务商户。
        if len(matches) == 1:
            record.merchant_id = item_merchants[matches[0]].id

    for item_name, merchant in item_merchants.items():
        if item_name in covered:
            continue
        db.add(
            VerifyRecord(
                order_id=order.id,
                merchant_id=merchant.id,
                item_name=item_name,
                verify_status=VerifyStatus.UNVERIFIED,
            )
        )
        logger.info(
            "演示订单 %s 补充项目：%s（%s）",
            order.order_no,
            item_name,
            merchant.name,
        )

    db.flush()


def seed_data(db: Session) -> None:
    """
    写入演示数据：
      - 景区票务商户：青山景区票务，平台抽成 5%
      - 咖啡商户：云顶咖啡，平台抽成 10%
      - 越野车商户：极速越野，平台抽成 15%
      - 商品：单人门票 80 元、精品咖啡 40 元、越野车体验 80 元，
        以及“门票+咖啡+越野车套餐”200 元（80 + 40 + 80）
      - 一张已支付的演示订单，包含门票 / 咖啡 / 越野车三条核销记录，
        三者分别归属各自商户，扫码核销时按岗位逐项完成。
    """
    ticket_merchant = _get_or_create_merchant(
        db, "青山景区票务", Decimal("0.05")
    )
    cafe_merchant = _get_or_create_merchant(db, "云顶咖啡", Decimal("0.10"))
    offroad_merchant = _get_or_create_merchant(
        db, "极速越野", Decimal("0.15")
    )

    _get_or_create_product(
        db,
        ticket_merchant,
        "单人门票",
        Decimal("80.00"),
        is_combo=False,
        sync_price=True,
    )
    _get_or_create_product(
        db,
        cafe_merchant,
        "精品咖啡",
        Decimal("40.00"),
        is_combo=False,
        sync_price=True,
    )
    _get_or_create_product(
        db,
        offroad_merchant,
        "越野车体验",
        Decimal("80.00"),
        is_combo=False,
        sync_price=True,
    )
    _upgrade_demo_combo_product(db, ticket_merchant)

    merchants_by_name = {
        "青山景区票务": ticket_merchant,
        "云顶咖啡": cafe_merchant,
        "极速越野": offroad_merchant,
    }
    item_merchants = {
        item_name: merchants_by_name[merchant_name]
        for item_name, merchant_name in DEMO_ITEMS
    }

    # 演示订单：一笔套餐订单跨三个商户履约，核销时分别计入各自商户。
    order = db.scalar(select(Order).where(Order.order_no == DEMO_ORDER_NO))
    if order is None:
        order = Order(
            order_no=DEMO_ORDER_NO,
            total_amount=DEMO_ORDER_AMOUNT,
            status=OrderStatus.PAID,
        )
        db.add(order)
        db.flush()
        logger.info("初始化演示订单：%s", DEMO_ORDER_NO)

    _upgrade_demo_order(db, order, item_merchants)
    if db.get(ParkSettings, PARK_SETTINGS_ID) is None:
        db.add(ParkSettings(id=PARK_SETTINGS_ID))
    db.commit()


# 演示环境重置使用的商户 / 商品口径，与 seed_data 保持一致。
DEMO_MERCHANT_SPECS: tuple[tuple[str, Decimal], ...] = (
    ("青山景区票务", Decimal("0.05")),
    ("云顶咖啡", Decimal("0.10")),
    ("极速越野", Decimal("0.15")),
)
DEMO_PRODUCT_SPECS: tuple[tuple[str, str, Decimal], ...] = (
    ("青山景区票务", "单人门票", Decimal("80.00")),
    ("云顶咖啡", "精品咖啡", Decimal("40.00")),
    ("极速越野", "越野车体验", Decimal("80.00")),
)


def _generate_reset_order_no() -> str:
    """生成 32 字符以内、可读且足够唯一的新演示订单号。"""
    return (
        f"DEMO{datetime.now():%Y%m%d%H%M%S%f}"
        f"{secrets.randbelow(1000):03d}"
    )


def _clear_order_scoped_data(db: Session) -> dict[str, int]:
    """
    物理清空所有订单相关表（DELETE，不是软删除）。

    按外键依赖顺序先删子表再删主表，兼容未开启外键级联的数据库；
    返回各表实际删除行数，便于日志确认“没有任何残留”。
    """
    deleted: dict[str, int] = {}
    for model in (SplitItemRecord, SplitRecord, VerifyRecord, Order):
        result = db.execute(delete(model))
        deleted[model.__tablename__] = int(result.rowcount or 0)
    db.flush()
    return deleted


def reset_demo_environment(db: Session) -> DemoResetResultOut:
    """
    一键重置演示环境。

    执行顺序：
      1. 按外键依赖顺序物理清空分账明细、分账流水、核销记录与订单；
      2. 幂等补齐演示商户 / 商品，保证退款金额仍能按原始单价计算；
      3. 创建一张 paid 状态、门票 + 咖啡 + 越野车均 unverified 的新订单。

    所有写操作在同一事务内完成；任一步失败都会由调用方回滚，
    不会出现“历史数据已删、新订单却没建出来”的半成品状态。
    """
    deleted = _clear_order_scoped_data(db)
    logger.info("重置演示环境：已清空历史数据 %s", deleted)

    # 2. 确保演示商户与商品存在，保证 allocate_item_amounts 能取到原始单价。
    merchants_by_name: dict[str, Merchant] = {}
    for merchant_name, split_rate in DEMO_MERCHANT_SPECS:
        merchants_by_name[merchant_name] = _get_or_create_merchant(
            db, merchant_name, split_rate
        )
    for merchant_name, product_name, price in DEMO_PRODUCT_SPECS:
        _get_or_create_product(
            db,
            merchants_by_name[merchant_name],
            product_name,
            price,
            is_combo=False,
            sync_price=True,
        )
    _upgrade_demo_combo_product(db, merchants_by_name["青山景区票务"])

    # 3. 创建全新的已支付订单，三项均未核销。
    order = Order(
        order_no=_generate_reset_order_no(),
        total_amount=DEMO_ORDER_AMOUNT,
        status=OrderStatus.PAID,
    )
    db.add(order)
    db.flush()

    records: list[VerifyRecord] = []
    merchant_name_by_item = dict(DEMO_ITEMS)
    for item_name, _merchant_name in DEMO_ITEMS:
        merchant = merchants_by_name[_merchant_name]
        record = VerifyRecord(
            order_id=order.id,
            merchant_id=merchant.id,
            item_name=item_name,
            verify_status=VerifyStatus.UNVERIFIED,
        )
        db.add(record)
        records.append(record)
    db.flush()

    items = [
        DemoResetItemOut(
            verify_record_id=record.id,
            item_name=record.item_name,
            merchant_id=record.merchant_id,
            merchant_name=merchants_by_name[
                merchant_name_by_item[record.item_name]
            ].name,
            verify_status=record.verify_status,
        )
        for record in records
    ]
    result = DemoResetResultOut(
        order_id=order.id,
        order_no=order.order_no,
        ticket_record_id=records[0].id,
        total_amount=_money(order.total_amount),
        order_status=OrderStatus.PAID,
        items=items,
        message=(
            f"演示环境已重置，新订单 ID：{order.id}，"
            f"订单号：{order.order_no}，三项均未核销，可直接开始测试。"
        ),
    )
    db.commit()

    logger.info(
        "演示环境已重置：order_id=%s, order_no=%s, ticket_record_id=%s",
        result.order_id,
        result.order_no,
        result.ticket_record_id,
    )
    return result


# ===========================================================================
# 八、数据库结构迁移（SQLite）
# ===========================================================================

ORDER_STATUS_SPLIT_MIGRATION = "20261111_01_orders_status_split"
SCHEMA_MIGRATIONS_TABLE = "schema_migrations"


def _ensure_schema_migrations_table(conn) -> None:
    conn.exec_driver_sql(
        f"CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} ("
        "version VARCHAR(64) NOT NULL PRIMARY KEY, "
        "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )


def _record_migration(conn, version: str) -> None:
    _ensure_schema_migrations_table(conn)
    conn.exec_driver_sql(
        f"INSERT OR IGNORE INTO {SCHEMA_MIGRATIONS_TABLE}(version) VALUES (?)",
        (version,),
    )


def _migrate_order_status_split(bind: Engine) -> None:
    """
    为 orders.status 的 CHECK 约束增加 split 值。

    SQLite 不支持 ALTER CHECK，采用“新表 → 拷贝 → 替换”的原地重建方式。
    迁移通过 schema_migrations 记录版本，可重复执行；非 SQLite 数据库
    应使用 Alembic / DBA 工单完成同等变更。
    """
    if bind.dialect.name != "sqlite":
        logger.info("非 SQLite 数据库，请使用 Alembic 执行 orders.status 迁移")
        return

    # PRAGMA foreign_keys 不能在事务内切换，使用 AUTOCOMMIT 连接。
    with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        row = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'"
        ).first()
        if row is None:
            # 全新数据库由 create_all 按最新模型建表。
            return

        table_sql = str(row[0] or "")
        if "'split'" in table_sql:
            _record_migration(conn, ORDER_STATUS_SPLIT_MIGRATION)
            return

        columns = "id, order_no, total_amount, status, created_at, updated_at"

        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                # 拿到写锁后二次确认，避免多进程同时启动时重复迁移。
                locked_row = conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='orders'"
                ).first()
                locked_sql = str(locked_row[0] or "") if locked_row else ""
                if not locked_sql:
                    conn.exec_driver_sql("COMMIT")
                    return
                if "'split'" in locked_sql:
                    _record_migration(conn, ORDER_STATUS_SPLIT_MIGRATION)
                    conn.exec_driver_sql("COMMIT")
                    return

                new_sql, replaced = re.subn(
                    r"CHECK \(status IN \([^)]*\)\)",
                    "CHECK (status IN ('unpaid', 'paid', 'split', 'refunded'))",
                    locked_sql,
                    count=1,
                )
                if replaced != 1:
                    raise RuntimeError("无法识别 orders.status 检查约束，迁移中止")
                new_sql = new_sql.replace(
                    "CREATE TABLE orders", "CREATE TABLE orders_new", 1
                )

                conn.exec_driver_sql(new_sql)
                conn.exec_driver_sql(
                    f"INSERT INTO orders_new ({columns}) SELECT {columns} FROM orders"
                )
                conn.exec_driver_sql("DROP TABLE orders")
                conn.exec_driver_sql("ALTER TABLE orders_new RENAME TO orders")
                conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_orders_order_no "
                    "ON orders (order_no)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders (status)"
                )
                _record_migration(conn, ORDER_STATUS_SPLIT_MIGRATION)
                conn.exec_driver_sql("COMMIT")
            except Exception:
                conn.exec_driver_sql("ROLLBACK")
                raise
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")

        logger.info("数据库迁移完成：orders.status 新增 split 状态")


VERIFY_STATUS_REFUNDED_MIGRATION = "20261111_02_verify_status_refunded"
SPLIT_AMOUNT_COLUMN_MIGRATION = "20261111_03_split_records_split_amount"
VERIFY_STATUS_EXPIRED_MIGRATION = "20261111_06_verify_status_expired"


def _migrate_verify_status_refunded(bind: Engine) -> None:
    """
    为 verify_records.verify_status 的 CHECK 约束增加 refunded 值。

    退款接口会把“未核销且已退款”的子项标记为 refunded，使其既不能再次
    退款，也不能被现场核销。SQLite 不支持 ALTER CHECK，因此与 orders 的
    split 迁移一样，采用“新表 → 拷贝 → 替换”的原地重建方式。
    """
    if bind.dialect.name != "sqlite":
        logger.info(
            "非 SQLite 数据库，请使用 Alembic 执行 verify_records 状态迁移"
        )
        return

    with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        row = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='verify_records'"
        ).first()
        if row is None:
            # 全新数据库由 create_all 按最新模型建表。
            return

        table_sql = str(row[0] or "")
        if "'refunded'" in table_sql:
            _record_migration(conn, VERIFY_STATUS_REFUNDED_MIGRATION)
            return

        columns = (
            "id, order_id, merchant_id, item_name, verify_status, "
            "created_at, updated_at"
        )

        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                locked_row = conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='verify_records'"
                ).first()
                locked_sql = str(locked_row[0] or "") if locked_row else ""
                if not locked_sql:
                    conn.exec_driver_sql("COMMIT")
                    return
                if "'refunded'" in locked_sql:
                    _record_migration(conn, VERIFY_STATUS_REFUNDED_MIGRATION)
                    conn.exec_driver_sql("COMMIT")
                    return

                new_sql, replaced = re.subn(
                    r"CHECK \(verify_status IN \([^)]*\)\)",
                    "CHECK (verify_status IN "
                    "('unverified', 'verified', 'refunded'))",
                    locked_sql,
                    count=1,
                )
                if replaced != 1:
                    raise RuntimeError(
                        "无法识别 verify_records.verify_status 检查约束，迁移中止"
                    )
                new_sql = new_sql.replace(
                    "CREATE TABLE verify_records",
                    "CREATE TABLE verify_records_new",
                    1,
                )

                conn.exec_driver_sql(new_sql)
                conn.exec_driver_sql(
                    f"INSERT INTO verify_records_new ({columns}) "
                    f"SELECT {columns} FROM verify_records"
                )
                conn.exec_driver_sql("DROP TABLE verify_records")
                conn.exec_driver_sql(
                    "ALTER TABLE verify_records_new RENAME TO verify_records"
                )
                # 重建原表的普通索引；唯一约束已随建表 SQL 一并复制。
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_verify_records_order_id "
                    "ON verify_records (order_id)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_verify_records_merchant_id "
                    "ON verify_records (merchant_id)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_verify_records_verify_status "
                    "ON verify_records (verify_status)"
                )
                _record_migration(conn, VERIFY_STATUS_REFUNDED_MIGRATION)
                conn.exec_driver_sql("COMMIT")
            except Exception:
                conn.exec_driver_sql("ROLLBACK")
                raise
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")

        logger.info(
            "数据库迁移完成：verify_records.verify_status 新增 refunded 状态"
        )


def _migrate_verify_status_expired(bind: Engine) -> None:
    """
    为 verify_records.verify_status 的 CHECK 约束增加 expired 值。

    离园结算会把未使用子项置为 expired，SQLite 无法 ALTER CHECK，
    因此沿用“新表 → 拷贝 → 替换”的原地重建方式；迁移幂等且保留数据。
    执行顺序必须在 refunded 迁移之后，旧库会依次获得两个新状态。
    """
    if bind.dialect.name != "sqlite":
        logger.info(
            "非 SQLite 数据库，请使用 Alembic 执行 verify_records 过期状态迁移"
        )
        return

    with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        row = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='verify_records'"
        ).first()
        if row is None:
            # 全新数据库由 create_all 按最新模型建表。
            return

        table_sql = str(row[0] or "")
        if "'expired'" in table_sql:
            _record_migration(conn, VERIFY_STATUS_EXPIRED_MIGRATION)
            return

        columns = (
            "id, order_id, merchant_id, item_name, verify_status, "
            "created_at, updated_at"
        )

        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                locked_row = conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='verify_records'"
                ).first()
                locked_sql = str(locked_row[0] or "") if locked_row else ""
                if not locked_sql:
                    conn.exec_driver_sql("COMMIT")
                    return
                if "'expired'" in locked_sql:
                    _record_migration(conn, VERIFY_STATUS_EXPIRED_MIGRATION)
                    conn.exec_driver_sql("COMMIT")
                    return

                new_sql, replaced = re.subn(
                    r"CHECK \(verify_status IN \([^)]*\)\)",
                    "CHECK (verify_status IN "
                    "('unverified', 'verified', 'refunded', 'expired'))",
                    locked_sql,
                    count=1,
                )
                if replaced != 1:
                    raise RuntimeError(
                        "无法识别 verify_records.verify_status 检查约束，迁移中止"
                    )
                new_sql = new_sql.replace(
                    "CREATE TABLE verify_records",
                    "CREATE TABLE verify_records_new",
                    1,
                )

                conn.exec_driver_sql(new_sql)
                conn.exec_driver_sql(
                    f"INSERT INTO verify_records_new ({columns}) "
                    f"SELECT {columns} FROM verify_records"
                )
                conn.exec_driver_sql("DROP TABLE verify_records")
                conn.exec_driver_sql(
                    "ALTER TABLE verify_records_new RENAME TO verify_records"
                )
                # 重建原表的普通索引；唯一约束已随建表 SQL 一并复制。
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_verify_records_order_id "
                    "ON verify_records (order_id)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_verify_records_merchant_id "
                    "ON verify_records (merchant_id)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_verify_records_verify_status "
                    "ON verify_records (verify_status)"
                )
                _record_migration(conn, VERIFY_STATUS_EXPIRED_MIGRATION)
                conn.exec_driver_sql("COMMIT")
            except Exception:
                conn.exec_driver_sql("ROLLBACK")
                raise
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")

        logger.info(
            "数据库迁移完成：verify_records.verify_status 新增 expired 状态"
        )


def _migrate_split_amount_column(bind: Engine) -> None:
    """
    为 split_records 增加 split_amount（实际参与分账的已核销金额）。

    历史分账流水生成时要求全部项目已核销，因此回填 split_amount =
    total_amount，保证新旧流水的展示口径一致。
    """
    if bind.dialect.name != "sqlite":
        logger.info(
            "非 SQLite 数据库，请使用 Alembic 执行 split_records 列迁移"
        )
        return

    with bind.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='split_records'"
        ).first()
        if row is None:
            # 全新数据库由 create_all 按最新模型建表。
            return

        column_names = {
            str(column[1])
            for column in conn.exec_driver_sql(
                "PRAGMA table_info(split_records)"
            ).fetchall()
        }
        if "split_amount" not in column_names:
            conn.exec_driver_sql(
                "ALTER TABLE split_records ADD COLUMN split_amount "
                "NUMERIC(10, 2) NOT NULL DEFAULT 0"
            )
            conn.exec_driver_sql(
                "UPDATE split_records SET split_amount = total_amount "
                "WHERE split_amount = 0"
            )
            logger.info(
                "数据库迁移完成：split_records 新增 split_amount 列并回填历史数据"
            )

        _record_migration(conn, SPLIT_AMOUNT_COLUMN_MIGRATION)


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


def _backfill_split_order_status(bind: Engine) -> None:
    """把已有分账流水、但仍为 paid 的历史订单回填为 split。"""
    if bind.dialect.name != "sqlite":
        return
    with bind.begin() as conn:
        has_split_table = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='split_records'"
        ).first()
        if has_split_table is None:
            return
        result = conn.exec_driver_sql(
            "UPDATE orders SET status = 'split' "
            "WHERE status = 'paid' "
            "AND id IN (SELECT order_id FROM split_records)"
        )
        if result.rowcount and result.rowcount > 0:
            logger.info("已回填 %s 笔历史订单为 split 状态", result.rowcount)


def initialize_database() -> None:
    """
    统一数据库初始化入口：结构迁移 → 建表 → 状态回填 → 演示数据。

    main.py 与 app_scan.py 都调用此函数，避免两个入口对旧库的初始化不一致。
    """
    _migrate_order_status_split(engine)
    _migrate_verify_status_refunded(engine)
    _migrate_verify_status_expired(engine)
    _migrate_split_amount_column(engine)
    _migrate_order_lifecycle_columns(engine)
    _migrate_merchant_business_hours(engine)
    Base.metadata.create_all(bind=engine)
    _backfill_split_order_status(engine)
    _backfill_order_entry_times(engine)
    with SessionLocal() as db:
        seed_data(db)


# ===========================================================================
# 九、应用初始化
# ===========================================================================

@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用启动时执行结构迁移、建表并写入演示数据；关闭时释放连接池。"""
    logger.info("正在初始化数据库：%s", DATABASE_URL)
    initialize_database()
    logger.info("数据库初始化完成")
    yield
    engine.dispose()


app = FastAPI(
    title="文旅小程序后端底座",
    description=(
        "FastAPI + SQLAlchemy 2.0 + SQLite 的工业级后端底座示例，"
        "包含 HMAC-SHA256 接口签名、分账引擎与智能退款。"
    ),
    version="0.2.0",
    lifespan=lifespan,
)

# CORS 跨域：小程序 / H5 / 管理后台联调时使用。
# 生产环境请把 allow_origins 换成明确的业务域名，不要保留 "*"。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # allow_origins=["*"] 时不能开启凭证
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,
)


# ===========================================================================
# 十、API 接口
# ===========================================================================

@app.get("/api/health", tags=["系统"], summary="健康检查")
def health(db: DbSession) -> dict:
    """最基础的存活探针：同时检查进程与数据库连接是否正常。"""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("健康检查失败：数据库不可用")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="数据库不可用",
        )

    return {
        "status": "ok",
        "service": "tourism-mp-backend",
        "version": app.version,
        "database": "sqlite",
        "time": datetime.now(timezone.utc).isoformat(),
    }


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


@app.post(
    "/api/demo/reset",
    response_model=DemoResetResultOut,
    tags=["系统"],
    summary="一键重置演示环境（需 HMAC 签名）",
)
def reset_demo(db: DbSession, _: RequireSignature) -> DemoResetResultOut:
    """
    清空所有历史订单 / 核销记录 / 分账流水，并生成一张全新的演示订单。

    - 新订单状态为 paid（已支付）；
    - 门票、咖啡、越野车三项均为 unverified（未核销）；
    - 返回新的订单 ID、订单号与门票核销记录 ID。

    该接口会删除全部历史数据，仅用于本地演示环境，生产环境请勿暴露。
    受签名保护：请求头需携带 X-Timestamp / X-Signature。
    """
    try:
        return reset_demo_environment(db)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("重置演示环境失败")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="重置演示环境失败，请稍后重试",
        )


@app.get("/api/merchants", response_model=list[MerchantOut], tags=["商户"], summary="商户列表")
def list_merchants(db: DbSession):
    return db.scalars(select(Merchant).order_by(Merchant.id)).all()


@app.patch(
    "/api/merchants/{merchant_id}/business-hours",
    response_model=MerchantOut,
    tags=["商户"],
    summary="更新商户营业时间（需 HMAC 签名）",
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


@app.get("/api/products", response_model=list[ProductOut], tags=["商品"], summary="商品列表")
def list_products(
    db: DbSession,
    merchant_id: int | None = Query(default=None, description="按商户筛选"),
):
    stmt = select(Product).order_by(Product.id)
    if merchant_id is not None:
        stmt = stmt.where(Product.merchant_id == merchant_id)
    return db.scalars(stmt).all()


@app.get("/api/orders", response_model=list[OrderOut], tags=["订单"], summary="订单列表")
def list_orders(
    db: DbSession,
    order_status: OrderStatus | None = Query(
        default=None, alias="status", description="按订单状态筛选"
    ),
):
    # selectinload 预加载核销记录，避免响应序列化时的 N+1 查询。
    stmt = (
        select(Order)
        .options(selectinload(Order.verify_records))
        .order_by(Order.id.desc())
    )
    if order_status is not None:
        stmt = stmt.where(Order.status == order_status)
    return db.scalars(stmt).all()


@app.get(
    "/api/orders/{order_id}",
    response_model=OrderOut,
    tags=["订单"],
    summary="订单详情（含核销记录）",
)
def get_order(order_id: int, db: DbSession):
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.verify_records))
        .where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="订单不存在",
        )
    return order


@app.get(
    "/api/verify-records",
    response_model=list[VerifyRecordOut],
    tags=["核销"],
    summary="核销记录列表",
)
def list_verify_records(
    db: DbSession,
    order_id: int | None = Query(default=None, description="按订单筛选"),
):
    stmt = select(VerifyRecord).order_by(VerifyRecord.id)
    if order_id is not None:
        stmt = stmt.where(VerifyRecord.order_id == order_id)
    return db.scalars(stmt).all()


@app.post(
    "/api/verify-records/{record_id}/verify",
    response_model=VerifyRecordOut,
    tags=["核销"],
    summary="核销一个项目（可按现场岗位自动选择）",
)
def verify_record(
    record_id: int,
    db: DbSession,
    post: VerifyPost | None = Query(
        default=None,
        description=(
            "当前核销岗位：gate=景区检票员，coffee=咖啡店员，offroad=越野车安全员。"
            "传入时，record_id 可以是该订单内的任意一条核销记录（通常是二维码里的门票记录），"
            "后端会按岗位只核销对应子项；不传则保持旧的“核销指定记录”行为。"
        ),
    ),
    order_id: int | None = Query(
        default=None,
        description=(
            "当前订单 ID（可选但推荐）。传入时，record_id 必须属于该订单，"
            "且所有子项状态只从该订单的核销记录读取，避免旧订单 / 历史数据串单。"
        ),
    ),
):
    """
    将一条核销记录从“未核销”改为“已核销”。

    业务约束：
      - 记录必须存在；
      - 显式传入 order_id 时，票号必须属于该订单；
      - 已核销的记录不能重复核销（防止重复消耗）；
      - 已退款的记录不能再核销（防止退款后继续使用）；
      - 只有“已支付”的订单才允许核销；
      - 传入 post 时按岗位选择子项，不再按顺序自动核销下一项：
        咖啡 / 越野车等后续项目必须先完成门票核销。
    """
    seed = db.get(VerifyRecord, record_id)
    if seed is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="核销记录不存在",
        )

    # 统一以“本次订单”为作用域：order_id 显式传入时以它为准，
    # 否则使用票号所属订单。后续所有状态判断只读该订单的核销记录。
    target_order_id = order_id if order_id is not None else seed.order_id
    order = _load_order_with_records(db, target_order_id)
    if seed.order_id != order.id:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="票号与订单不匹配，请使用当前订单的二维码",
        )
    if order.status != OrderStatus.PAID:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"订单状态为 {order.status.value}，不允许核销",
        )

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

    if post is None:
        # 兼容旧调用：record_id 就是本次要核销的那一条记录。
        record = seed
        if record.verify_status == VerifyStatus.VERIFIED:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="该核销项已完成核销",
            )
        if record.verify_status == VerifyStatus.REFUNDED:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="该核销项已退款，不可再核销",
            )
        if record.verify_status == VerifyStatus.EXPIRED:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="本次游园已结束，未使用项目已过期，如需游玩请重新购票",
            )
    else:
        # 新逻辑：二维码只提供“这是哪张订单”，由岗位决定核销哪个子项。
        record = _select_record_for_post(order, post)

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
    lifecycle = _order_lifecycle(
        order,
        settings,
        close_time=_resolve_order_close_time(db, order),
    )
    if not lifecycle.can_verify:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=_verify_blocked_message(lifecycle),
        )

    record.verify_status = VerifyStatus.VERIFIED
    if target_is_ticket and order.entered_at is None:
        order.entered_at = _utcnow_naive()
    db.commit()
    db.refresh(record)
    logger.info(
        "核销成功：record_id=%s, order_id=%s, merchant_id=%s, item=%s, post=%s",
        record.id,
        record.order_id,
        record.merchant_id,
        record.item_name,
        post.value if post is not None else "legacy",
    )
    return record


@app.get(
    "/api/order/{order_id}/lifecycle",
    response_model=LifecycleOut,
    tags=["生命周期"],
    summary="查询订单游园生命周期",
)
def get_order_lifecycle(order_id: int, db: DbSession) -> LifecycleOut:
    order = _load_order_with_records(db, order_id)
    settings = _load_park_settings(db)
    snapshot = _order_lifecycle(
        order,
        settings,
        close_time=_resolve_order_close_time(db, order),
    )
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

    snapshot = _order_lifecycle(
        order,
        settings,
        close_time=_resolve_order_close_time(db, order),
    )
    return _to_lifecycle_out(order, settings, snapshot)


@app.post(
    "/api/order/{order_id}/exit-settlement",
    response_model=ExitSettlementOut,
    tags=["生命周期"],
    summary="离园结算：过期未使用子项并立即分账（需 HMAC 签名）",
)
def settle_order_on_exit(
    order_id: int,
    db: DbSession,
    _: RequireSignature,
) -> ExitSettlementOut:
    """
    离园结算引擎：解决“已离园但订单永远卡在部分核销”的资损 / 对账问题。

    触发前提：
      - 订单已支付（或部分退款后仍待结算），且尚未生成分账流水；
      - 游客已完成门票核销（entered_at 非空）。

    执行步骤（全部处于同一数据库事务）：
      1. 未登记离园时，以当前时间补登 exited_at；
      2. 把所有 unverified 子项强制置为 expired（未使用即作废）；
      3. 已核销子项按商户 split_rate 正常分账，商户实得入账；
      4. 已过期子项金额全额计入平台留存（forfeited_amount），不退款、
         不分给商户；
      5. 订单状态条件 UPDATE 为 split，并写入唯一分账流水快照。

    结算完成后，订单只可能是 verified / expired / refunded 三种终态，
    生命周期立即结束，不会再停留在“部分核销”。

    受签名保护：请求头需携带 X-Timestamp / X-Signature。
    """
    order = _load_order_with_records(db, order_id)

    if order.status == OrderStatus.UNPAID:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单未支付，不能进行离园结算",
        )
    if order.status == OrderStatus.SPLIT:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="该订单已完成离园结算，不能重复结算",
        )
    if not order.verify_records:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单没有可结算项目",
        )
    if order.entered_at is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="游客尚未入园，无法办理离园结算",
        )

    # 兼容历史数据：状态仍为 paid/refunded 但已存在流水时，直接拒绝。
    existing_split_id = db.scalar(
        select(SplitRecord.id).where(SplitRecord.order_id == order.id)
    )
    if existing_split_id is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="该订单已生成分账流水，不能重复结算",
        )

    now_value = _utcnow_naive()
    if order.exited_at is None:
        # 一键离园结算：现场没有单独扫码离园时，以结算时间补登。
        order.exited_at = now_value

    # 核心动作 1：所有未核销子项立即作废，绝不允许继续停留在 unverified。
    expired_records = [
        record
        for record in order.verify_records
        if record.verify_status == VerifyStatus.UNVERIFIED
    ]
    for record in expired_records:
        record.verify_status = VerifyStatus.EXPIRED

    # 核心动作 2：重新计算分账。expired 子项金额整笔计入平台留存。
    result = _build_split_result(db, order)

    # 核心动作 3：条件 UPDATE 原子迁移为 split，防止并发重复结算。
    updated = db.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.status.in_([OrderStatus.PAID, OrderStatus.REFUNDED]),
        )
        .values(status=OrderStatus.SPLIT)
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        db.rollback()
        latest_status = db.scalar(select(Order.status).where(Order.id == order.id))
        if latest_status == OrderStatus.SPLIT:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="该订单已完成离园结算，不能重复结算",
            )
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单状态已变化，离园结算未执行，请刷新后重试",
        )

    # 保持会话内对象与数据库一致（expire_on_commit=False）。
    order.status = OrderStatus.SPLIT

    split_record = SplitRecord(
        order_id=order.id,
        total_amount=result.total_amount,
        split_amount=result.split_amount,
        platform_retention=result.platform_retention,
        merchant_income_total=result.merchant_income_total,
        # 结算项目数 = 已核销分账项 + 已过期项。
        item_count=sum(
            1
            for item in result.items
            if item.split_eligible
            or item.verify_status == VerifyStatus.EXPIRED
        ),
    )
    split_record.items = [
        SplitItemRecord(
            verify_record_id=item.verify_record_id,
            item_name=item.item_name,
            merchant_id=item.merchant_id,
            merchant_name=item.merchant_name,
            verify_status=item.verify_status.value,
            amount=item.amount,
            split_rate=item.split_rate,
            platform_commission=item.platform_commission,
            merchant_income=item.merchant_income,
        )
        for item in result.items
    ]
    db.add(split_record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning("重复离园结算被唯一约束拦截：order_id=%s", order_id)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="该订单已完成离园结算，不能重复结算",
        )
    except SQLAlchemyError:
        db.rollback()
        logger.exception("离园结算落库失败：order_id=%s", order_id)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="离园结算保存失败，请稍后重试",
        )

    result = result.model_copy(update={"order_status": OrderStatus.SPLIT})
    message = (
        f"离园结算完成：{len(expired_records)} 个未使用项目已过期，"
        f"过期金额 {_format_money(result.forfeited_amount)} 元留在平台；"
        f"已核销项目分账 {_format_money(result.split_amount)} 元。"
    )
    logger.info(
        "离园结算完成：order_id=%s, expired=%s, forfeited=%s, split=%s, "
        "platform_retention=%s, split_record_id=%s",
        order.id,
        len(expired_records),
        _format_money(result.forfeited_amount),
        _format_money(result.split_amount),
        _format_money(result.platform_retention),
        split_record.id,
    )
    return ExitSettlementOut(
        **result.model_dump(),
        exited_at=order.exited_at,
        expired_item_count=len(expired_records),
        expired_amount=result.forfeited_amount,
        message=message,
    )


@app.post(
    "/api/order/{order_id}/split",
    response_model=SplitResultOut,
    tags=["分账"],
    summary="订单分账计算（需 HMAC 签名）",
)
def split_order(
    order_id: int,
    db: DbSession,
    _: RequireSignature,
) -> SplitResultOut:
    """
    按订单内“已核销”项目的金额与商户 split_rate 计算分账结果并落库。

    支持「部分核销 + 部分退款」的订单：退款只退未核销子项，已核销子项
    仍可正常参与分账，因此已退款订单不再被一刀切拦截。

    业务顺序（先校验状态与分账池，再计算，最后原子落库）：
      1. 未支付订单不允许分账（409）；
      2. 已分账订单不允许重复分账（409）；
      3. 订单必须至少有一个“已核销”子项，否则没有可分账金额（409）；
      4. 遍历全部子项：仅“已核销”子项进入分账池；未核销 / 已退款子项
         的金额、平台抽成、商户实得全部记为 0；
      5. 通过条件 UPDATE（paid / refunded → split）完成原子状态迁移，
         确保同一订单只会生成一条分账流水；
      6. 写入 split_records 与 split_record_items 快照，唯一约束兜底。

    门票商户默认抽成 5%，咖啡商户默认抽成 10%（以 merchants.split_rate 为准）；
    平台留存 = Σ(已核销项目金额 × split_rate)，商户实得 = 已核销项目金额 - 平台抽成。

    受签名保护：请求头需携带 X-Timestamp / X-Signature，
    可使用 build_signature_headers() 生成。
    """
    order = _load_order_with_records(db, order_id)

    # 注意：已退款订单不再直接拒绝。部分退款后，已核销子项仍应参与分账。
    if order.status == OrderStatus.UNPAID:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单未支付，不能进行分账",
        )
    if order.status == OrderStatus.SPLIT:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="该订单已生成分账流水，不能重复分账",
        )
    if not order.verify_records:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单没有可分账项目",
        )

    settings = _load_park_settings(db)
    lifecycle = _order_lifecycle(
        order,
        settings,
        close_time=_resolve_order_close_time(db, order),
    )
    if not lifecycle.can_split:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=lifecycle.message,
        )

    # 分账池 = 已核销子项。未核销 / 已退款子项记 0，不参与抽成与结算。
    verified_items = [
        record
        for record in order.verify_records
        if record.verify_status == VerifyStatus.VERIFIED
    ]
    if not verified_items:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单没有已核销项目，分账金额为 0，无法生成分账流水",
        )

    # 防重复分账：兼容历史数据（状态仍为 paid 但已有流水）。
    existing_split_id = db.scalar(
        select(SplitRecord.id).where(SplitRecord.order_id == order.id)
    )
    if existing_split_id is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="该订单已生成分账流水，不能重复分账",
        )

    result = _build_split_result(db, order)

    # 原子状态迁移：paid（正常分账）或 refunded（部分退款后分账）均可进入 split。
    # 多实例部署时，重复分账请求由这条条件 UPDATE 与 split_records 唯一约束共同拦截。
    updated = db.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.status.in_([OrderStatus.PAID, OrderStatus.REFUNDED]),
        )
        .values(status=OrderStatus.SPLIT)
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        db.rollback()
        latest_status = db.scalar(select(Order.status).where(Order.id == order.id))
        if latest_status == OrderStatus.SPLIT:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="该订单已生成分账流水，不能重复分账",
            )
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单状态已变化，分账未执行，请刷新后重试",
        )

    split_record = SplitRecord(
        order_id=order.id,
        total_amount=result.total_amount,
        split_amount=result.split_amount,
        platform_retention=result.platform_retention,
        merchant_income_total=result.merchant_income_total,
        # 分账项目数只统计真正进入分账池的已核销子项。
        item_count=len(verified_items),
    )
    split_record.items = [
        SplitItemRecord(
            verify_record_id=item.verify_record_id,
            item_name=item.item_name,
            merchant_id=item.merchant_id,
            merchant_name=item.merchant_name,
            verify_status=item.verify_status.value,
            amount=item.amount,
            split_rate=item.split_rate,
            platform_commission=item.platform_commission,
            merchant_income=item.merchant_income,
        )
        for item in result.items
    ]
    db.add(split_record)
    try:
        db.commit()
    except IntegrityError:
        # 并发请求同时通过上面的检查时，唯一约束会拦下后到的那一笔；
        # 回滚同时撤销已执行的 paid → split 状态迁移。
        db.rollback()
        logger.warning("重复分账被唯一约束拦截：order_id=%s", order_id)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="该订单已生成分账流水，不能重复分账",
        )
    except SQLAlchemyError:
        db.rollback()
        logger.exception("分账流水落库失败：order_id=%s", order_id)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="分账流水保存失败，请稍后重试",
        )

    result = result.model_copy(update={"order_status": OrderStatus.SPLIT})
    logger.info(
        "分账完成：order_id=%s, total=%s, platform_retention=%s, split_record_id=%s",
        order.id,
        _format_money(result.total_amount),
        _format_money(result.platform_retention),
        split_record.id,
    )
    return result


def _load_split_record_for_order(db: Session, order_id: int) -> SplitRecord:
    """加载订单的分账流水及项目快照，供查询接口复用。"""
    record = db.scalar(
        select(SplitRecord)
        .options(
            joinedload(SplitRecord.order),
            selectinload(SplitRecord.items),
        )
        .where(SplitRecord.order_id == order_id)
    )
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="该订单尚未分账",
        )
    return record


@app.get(
    "/api/order/{order_id}/split",
    response_model=SplitRecordOut,
    tags=["分账"],
    summary="查询订单分账流水（需 HMAC 签名）",
)
def get_order_split(
    order_id: int,
    db: DbSession,
    _: RequireSignature,
) -> SplitRecordOut:
    """查询单笔订单已落库的分账流水，包含项目与商户快照。"""
    return _to_split_record_out(_load_split_record_for_order(db, order_id))


@app.get(
    "/api/split-records",
    response_model=SplitRecordPageOut,
    tags=["分账"],
    summary="分账流水列表（需 HMAC 签名）",
)
def list_split_records(
    db: DbSession,
    _: RequireSignature,
    order_id: int | None = Query(
        default=None, ge=1, description="按订单 ID 精确筛选"
    ),
    order_no: str | None = Query(
        default=None, max_length=32, description="按订单号模糊筛选"
    ),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        default=20, ge=1, le=100, description="每页条数，最大 100"
    ),
) -> SplitRecordPageOut:
    """分页查询分账流水，按流水 ID 倒序排列。"""
    filters = []
    if order_id is not None:
        filters.append(SplitRecord.order_id == order_id)
    if order_no and order_no.strip():
        filters.append(
            SplitRecord.order.has(Order.order_no.contains(order_no.strip()))
        )

    total = (
        db.scalar(
            select(func.count()).select_from(SplitRecord).where(*filters)
        )
        or 0
    )
    records = db.scalars(
        select(SplitRecord)
        .options(joinedload(SplitRecord.order))
        .where(*filters)
        .order_by(SplitRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return SplitRecordPageOut(
        total=total,
        page=page,
        page_size=page_size,
        items=[_to_split_record_summary(record) for record in records],
    )


@app.post(
    "/api/order/{order_id}/refund",
    response_model=RefundResultOut,
    tags=["退款"],
    summary="智能退款（需 HMAC 签名）",
)
def refund_order(
    order_id: int,
    db: DbSession,
    _: RequireSignature,
) -> RefundResultOut:
    """
    智能退款（支持部分退款）：

    - 全部未核销：按订单原价全额退款，整体扣除 10% 手续费；
    - 部分核销：已核销项目退款 0 元（拒绝退款）；仅未核销项目按原始单价
      退款，并逐项扣除 10% 手续费；
    - 已退款项目所属子项置为 refunded，既不能再次退款，也不能再核销；
    - 全部已核销：返回 409，提示不可退款；
    - 响应 items 为逐项明细，包含项目、核销状态（已用 / 未用 / 已退款）、
      退款金额与处理原因；
    - 已分账订单不允许退款；
    - 只要产生退款，订单状态置为 refunded，防止重复退款；
    - 部分退款后，已核销子项仍可通过分账接口单独结算。

    受签名保护：请求头需携带 X-Timestamp / X-Signature。
    """
    order = _load_order_with_records(db, order_id)

    if order.status == OrderStatus.REFUNDED:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单已退款，不能重复退款",
        )
    if order.status == OrderStatus.SPLIT:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单已分账，不可退款",
        )
    if order.status != OrderStatus.PAID:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"订单状态为 {order.status.value}，只有已支付订单可以退款",
        )

    # 分账后不可退款：已生成分账流水的订单不允许再退款（反向不成立，
    # 部分退款后仍可对已核销子项分账）。
    existing_split_id = db.scalar(
        select(SplitRecord.id).where(SplitRecord.order_id == order.id)
    )
    if existing_split_id is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单已分账，不可退款",
        )

    if not order.verify_records:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单没有可退款项目",
        )

    settings = _load_park_settings(db)
    close_time = _resolve_order_close_time(db, order)
    now_value = _utcnow_naive()

    # 最高优先级：园区配置“离园后不允许退款”时，先于生命周期判断直接拒绝。
    # 这样即使 refund_window_hours 仍保留 72 小时的历史值，也不会被误用。
    policy_block = _refund_policy_block_reason(
        order, settings, close_time=close_time, now=now_value
    )
    if policy_block:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=policy_block,
        )

    lifecycle = _order_lifecycle(
        order,
        settings,
        now=now_value,
        close_time=close_time,
    )
    if not lifecycle.can_refund:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=lifecycle.message,
        )

    result = _build_refund_result(db, order)
    if not result.refunded:
        # 全部项目都不可退：用 409 明确表达拒绝，同时在 detail 中给出逐项原因。
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=result.model_dump(mode="json"),
        )

    # 原子状态迁移：只有仍为 paid 的订单能进入 refunded。
    # 与分账接口的条件 UPDATE 共同保证多实例下资金动作只有一个赢家。
    updated = db.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == OrderStatus.PAID)
        .values(status=OrderStatus.REFUNDED)
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        db.rollback()
        latest_status = db.scalar(select(Order.status).where(Order.id == order.id))
        if latest_status == OrderStatus.SPLIT:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="订单已分账，不可退款",
            )
        if latest_status == OrderStatus.REFUNDED:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="订单已退款，不能重复退款",
            )
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="订单状态已变化，退款未执行，请刷新后重试",
        )

    # 与订单状态迁移处于同一事务：退款成功即把实际退款的子项标记为 refunded。
    # 这样后续分账只能看到 verified 子项，未核销 / 已退款子项一律记 0。
    refunded_record_ids = [
        item.verify_record_id for item in result.items if item.refundable
    ]
    if refunded_record_ids:
        db.execute(
            update(VerifyRecord)
            .where(VerifyRecord.id.in_(refunded_record_ids))
            .values(verify_status=VerifyStatus.REFUNDED)
            .execution_options(synchronize_session=False)
        )

    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("退款落库失败：order_id=%s", order_id)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="退款处理失败，请稍后重试",
        )

    logger.info(
        "退款完成：order_id=%s, refund_amount=%s, fee_amount=%s, "
        "refunded_items=%s, rejected_items=%s",
        order.id,
        _format_money(result.refund_amount),
        _format_money(result.fee_amount),
        result.refunded_item_count,
        result.rejected_item_count,
    )
    return result


# ===========================================================================
# 十一、本地启动入口
# ===========================================================================

if __name__ == "__main__":
    import uvicorn

    # 0.0.0.0 便于同一局域网内的手机 / 小程序开发者工具访问。
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
