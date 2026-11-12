# 🌿 乡村文旅多商户票务与资金分账系统

> 一套面向乡村文旅综合体的**多商户票务履约 + 资金分账**后端底座与现场核销工作台。
> FastAPI + SQLAlchemy 2.0 + SQLite + Gradio，聚焦三个真实业务难题：**多商户隔离**、**防黄牛/防重复核销**、**资金漏损**。

---

## 📖 项目简介

乡村文旅场景下，一张"门票 + 咖啡 + 越野车"的套餐订单往往由**多个独立商户**共同履约。传统方案通常把订单绑定到单一商户，导致：

| 业务痛点 | 现实表现 | 本系统的解法 |
| :--- | :--- | :--- |
| **多商户隔离** | 套餐跨商户履约时，核销记录、抽成比例、结算归属混在一起，对账靠人工 Excel | 订单不直连商户，由 `verify_records` 逐项记录"哪个商户核销了哪一项"，分账按核销商户的 `split_rate` 精细结算 |
| **防黄牛 / 防重复核销** | 一张二维码被反复扫码、跨岗位顺带核销、票码被截图倒卖 | 岗位级核销（检票员/咖啡店/越野车安全员各管一项）、二维码与订单强绑定、重复扫码返回 409、离园后票码自动失效 |
| **资金漏损** | 退款与分账并发执行导致重复出款；浮点误差造成分币丢失；未使用金额归属不清 | HMAC 签名保护资金接口、条件 UPDATE + 唯一约束防重复分账、Decimal 分币精确计算、过期金额明确留归平台 |

系统包含两个可独立运行的服务：

- **`main.py`** —— 企业级后端底座：订单、核销、生命周期、退款、分账、离园结算、园区配置。
- **`app_scan.py`** —— Gradio 现场工作台：游客购票取码、商家人工核销、运营后台。

```text
┌──────────────────────────────┐        HTTP + HMAC-SHA256        ┌──────────────────────────────┐
│  app_scan.py (Gradio :7861)  │ ───────────────────────────────▶ │  main.py (FastAPI :8000)     │
│                              │                                  │                              │
│  · 游客购票 / 电子二维码      │  POST /api/order/{id}/split      │  · HMAC 签名校验中间件        │
│  · 商家扫码 / 人工核销        │  POST /api/order/{id}/refund     │  · 核销 / 生命周期状态机      │
│  · 运营分账 / 退款 / 离园结算 │  POST /api/.../exit-settlement   │  · 分账引擎 / 智能退款        │
│  · 园区退款政策配置           │  GET  /api/order/{id}/lifecycle  │  · 条件 UPDATE 并发防护       │
└──────────────────────────────┘                                  │  · SQLite / SQLAlchemy 2.0   │
                                                                  └──────────────────────────────┘
```

---

## ✨ 核心技术亮点

### 1️⃣ HMAC-SHA256 接口防篡改

所有**资金类接口**（分账、退款、离园结算、园区配置、商户营业时间）都要求携带签名请求头：

```http
X-Timestamp: 1762819200
X-Signature: 5f2c...e91a
```

签名原文采用固定的 canonical string，业务字段之间以换行分隔：

```text
{timestamp}\n{METHOD}\n{path}\n{sha256_hex(body)}
signature = HMAC-SHA256(API_SIGNING_SECRET, canonical).hexdigest()
```

- 空请求体同样参与摘要计算，防止 body 被替换；
- 时间戳默认允许 **±300 秒**偏差（`API_SIGNATURE_TTL_SECONDS`），拦截重放攻击；
- 签名比较使用 `hmac.compare_digest`，避免时序侧信道；
- 任何失败（缺头、过期、签名不符）统一返回 **403**，且不泄露期望签名。

客户端可直接复用 `main.build_signature_headers()` 生成请求头，`app_scan.py` 内也实现了逐字符一致的签名逻辑。

### 2️⃣ 数据库条件 UPDATE「乐观锁」防并发

分账、退款、离园结算的状态迁移**不依赖进程内锁**，而是交给数据库做原子裁决：

```sql
UPDATE orders
   SET status = 'split'
 WHERE id = :order_id
   AND status IN ('paid', 'refunded');   -- 只有这两个状态允许迁移
```

- 判断受影响行数（`rowcount`）：若为 `0`，说明已被其他请求抢先迁移，直接返回 **409**；
- `split_records.order_id` 上的**唯一约束**是最后一道防线：即使并发请求同时通过应用层检查，数据库也只接受一条分账流水；
- 该方案不依赖 Redis / 分布式锁，天然支持多实例部署。

### 3️⃣ Decimal 资金精度

- 金额字段使用 `Numeric(10, 2)`，抽成比例使用 `Numeric(5, 4)`，杜绝二进制浮点误差；
- 所有金额统一经 `_money()` 做 **"分"级量化（`ROUND_HALF_UP`）**；
- 多子项分摊时先按比例/均分量化，再把分币差额补到最后一个子项，**保证分项之和严格等于订单总额**；
- JSON 响应中的金额以两位小数字符串输出（如 `"76.00"`），前端不再做二次浮点运算。

### 4️⃣ 游园生命周期状态机

门票核销即入园，离园/闭园后进入退款窗口，生命周期结束才允许分账：

| 生命周期状态 | 可分账 | 可退款 | 可核销 | 说明 |
| :--- | :---: | :---: | :---: | :--- |
| `not_applicable` | ✅ | ✅* | ✅ | 订单不含门票 |
| `not_started` | ❌ | ✅* | ✅（闭园前） | 未入园 |
| `active`（园内） | ❌ | ✅ | ✅ | 游园进行中 |
| `active`（离园窗口） | ❌ | ✅ | ❌ | 已离园/闭园，等待退款窗口结束 |
| `completed` | ✅ | ❌ | ❌ | 生命周期结束，再入园须重新购票 |

> `leave_at = min(实际扫码离园时间, 入园当天该商户闭园时刻)`，闭园时间取自订单门票商户的 `merchants.close_time`（默认 `23:59:59`）。

配套的**离园结算引擎**（`POST /api/order/{id}/exit-settlement`）用于终结"已离园却卡在部分核销"的订单：未使用子项强制置为 `expired`，已核销部分按商户抽成分账，过期金额全额留归平台。

### 5️⃣ 分账与退款规则速览

| 动作 | 规则 |
| :--- | :--- |
| **分账池** | 仅 `verified` 子项参与；`unverified` / `refunded` 计 0；`expired` 全额留归平台 |
| **抽成比例** | 门票 5%、咖啡 10%、越野车 15%（按商户 `split_rate`） |
| **退款** | 已核销项目拒绝退款；未核销项目按原价退款，统一扣 10% 手续费 |
| **唯一性** | 每笔订单最多一条分账流水，已分账订单禁止再次分账 / 退款 |

---

## 🧪 自动化防御测试说明

测试文件：`test_core_poc.py`，基于 `pytest` + `FastAPI TestClient` + 内存 SQLite（`StaticPool`），与生产库完全隔离。

```bash
pytest test_core_poc.py -v
```

### 用例 1：业务状态机流转与生命周期

- 未核销门票时直接核销咖啡 → **409**，提示"门票未核销，请先去检票口"；
- 正常核销门票后，`orders.entered_at` 被写入；
- 生命周期尚未结束时调用分账 → **409**，提示"生命周期进行中"。

### 用例 2：接口防篡改与防重放

| 攻击方式 | 预期结果 |
| :--- | :--- |
| 不带任何签名头直接请求退款 | **403**（缺少 `X-Timestamp`） |
| 伪造 `X-Signature` | **403**（签名不匹配） |
| 使用 400 秒前的时间戳重放 | **403**（时间戳已过期） |

### 用例 3：并发下的防重复分账（杀手锏）

在多线程极限并发压测下，系统成功拦截了大部分重复分账请求，断言最终只有 **1 次分账成功**（1 passed, 9 intercepted），杜绝了资金资损风险。

```python
with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
    results = list(executor.map(lambda _: make_request(), range(10)))

assert success_count == 1    # 最终只有 1 次成功
assert conflict_count == 9   # 其余全部被 409 拦截
```

测试随后**绕过 ORM 缓存**，用原生 SQL 复查数据库最终状态：

```python
assert result == "split"     # 数据库最终状态必须是 split，且只生成一条分账流水
```

该用例同时验证了三层防线：应用层状态校验 → 条件 UPDATE 原子迁移 → `split_records.order_id` 唯一约束兜底。即使把并发请求放大到数百级、或多实例部署，资金也不会重复出账。

---

## 🚀 快速启动指南

### 环境要求

- Python **3.10+**
- 操作系统：Windows / macOS / Linux 均可（SQLite 无需额外安装）

### 1. 安装依赖

```bash
pip install fastapi "uvicorn[standard]" "sqlalchemy>=2.0" gradio opencv-python qrcode pillow numpy requests pytest
```

### 2. 启动后端服务（终端 1）

```bash
python main.py
```

启动后自动完成：

1. 创建 `tourism.db`（SQLite）；
2. 执行幂等结构迁移（`schema_migrations`）；
3. 按模型自动建表；
4. 写入演示商户 / 商品 / 订单 / 核销记录。

服务地址：<http://127.0.0.1:8000>，健康检查：`GET /api/health`，交互式文档：<http://127.0.0.1:8000/docs>

### 3. 启动现场工作台（终端 2）

```bash
python app_scan.py
```

浏览器访问：<http://127.0.0.1:7861>

> **重要**：`app_scan.py` 与 `main.py` 必须使用相同的 `API_SIGNING_SECRET`，否则所有资金接口返回 403。

```bash
# Windows CMD
set API_SIGNING_SECRET=your-production-secret

# Git Bash / PowerShell / macOS / Linux
export API_SIGNING_SECRET=your-production-secret
```

### 常用环境变量

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `API_SIGNING_SECRET` | `tourism-mp-demo-secret` | HMAC 密钥，**生产环境必须修改** |
| `API_SIGNATURE_TTL_SECONDS` | `300` | 签名时间戳允许偏差（秒） |
| `DATABASE_URL` | `sqlite:///./tourism.db` | 数据库连接串，可切换 PostgreSQL/MySQL |
| `API_BASE_URL` | `http://127.0.0.1:8000` | `app_scan.py` 访问的后端地址 |
| `SCAN_APP_PORT` | `7861` | Gradio 工作台端口 |
| `SQL_ECHO` | `0` | 设为 `1` 打印 SQL，便于排查 |

### 调用受保护接口示例

```python
from main import build_signature_headers
import requests

path = "/api/order/1/split"
headers = build_signature_headers("POST", path)

resp = requests.post(f"http://127.0.0.1:8000{path}", headers=headers)
print(resp.status_code, resp.json())
```

---

## 🔌 核心 API 一览

| 分类 | 方法 & 路径 | 签名 | 说明 |
| :--- | :--- | :---: | :--- |
| 系统 | `GET /api/health` | — | 健康检查 |
| 商户 | `GET /api/merchants` | — | 商户列表 |
| 商户 | `PATCH /api/merchants/{id}/business-hours` | ✅ | 更新营业 / 闭园时间 |
| 订单 | `GET /api/orders` · `GET /api/orders/{id}` | — | 订单列表 / 详情 |
| 核销 | `POST /api/verify-records/{id}/verify?post=gate` | — | 按岗位核销（gate/coffee/offroad） |
| 生命周期 | `GET /api/order/{id}/lifecycle` | — | 查询游园生命周期 |
| 生命周期 | `POST /api/verify-records/{id}/exit` | — | 扫码离园（幂等） |
| 结算 | `POST /api/order/{id}/exit-settlement` | ✅ | 离园结算：过期 + 分账 |
| 分账 | `POST /api/order/{id}/split` | ✅ | 执行分账 |
| 分账 | `GET /api/order/{id}/split` | ✅ | 查询单笔分账流水 |
| 分账 | `GET /api/split-records` | ✅ | 分账流水分页查询 |
| 退款 | `POST /api/order/{id}/refund` | ✅ | 智能退款 |
| 配置 | `GET /api/park-settings` · `PUT /api/park-settings` | PUT ✅ | 园区退款政策 |
| 演示 | `POST /api/demo/reset` | ✅ | 一键重置演示订单 |

---

## 🗺️ 未来演进蓝图

### 阶段一：生产加固（0–3 个月）

- 密钥托管到 KMS / 密钥管理系统，接口签名升级为密钥轮换 + 商户级密钥；
- 数据库切换 PostgreSQL，结构变更交由 Alembic 管理；
- 增加幂等键（Idempotency-Key）、操作审计日志与资金对账任务；
- 接入 Prometheus 指标与告警，补齐 CI 流水线。

### 阶段二：多租户 SaaS 与小程序化（3–9 个月）

- 微信小程序购票、支付回调与电子发票打通；
- 多园区 / 多租户隔离，商户自助后台（商品、核销、结算单）；
- 自动生成商户结算单与提现流水，支持 T+1 对账；
- 实名购票、限购规则、动态票价，进一步压缩黄牛空间。

### 阶段三：数据智能与生态（9–18 个月）

- 客流预测、动态定价与园区承载量预警；
- 跨商户联合套餐与智能推荐；
- 开放平台 API，接入 OTA 渠道与本地生活平台；
- 运营数据大屏与商户经营分析。

---

## 📌 说明与免责声明

- 本项目默认使用 SQLite 与演示密钥，定位为**可运行的业务底座 / PoC**，请勿直接用于生产环境。
- 生产部署前务必：修改 `API_SIGNING_SECRET`、切换 PostgreSQL、开启 HTTPS 与网关鉴权、限制 CORS 来源。
- 当前 `app_scan.py` 的购票链路在后端未提供创建订单接口时，会自动回退到本地 SQLAlchemy 写入，便于单机演示；正式环境建议统一走支付回调创建订单。

---

<p align="center">🌾 让乡村文旅的每一笔收入，都算得清、分得明、追得到。</p>
