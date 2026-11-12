import pytest
import concurrent.futures
import time
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# 导入你的 main.py 中的核心组件
from main import (
    app, Base, get_db, SessionLocal,
    Order, OrderStatus, VerifyRecord, VerifyStatus,
    Merchant, build_signature_headers,
    _utcnow_naive
)

# ===========================================================================
# 测试环境隔离：使用内存 SQLite
# ===========================================================================
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)

@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

# ===========================================================================
# 测试用例 1：业务状态机流转与生命周期
# ===========================================================================
def test_business_flow_and_lifecycle(client, db_session):
    merchant = Merchant(name="测试商户", split_rate=0.05)
    db_session.add(merchant)
    db_session.commit()
    
    order = Order(order_no="TEST001", total_amount=200.00, status=OrderStatus.PAID)
    db_session.add(order)
    db_session.commit()
    
    ticket = VerifyRecord(order_id=order.id, merchant_id=merchant.id, item_name="门票", verify_status=VerifyStatus.UNVERIFIED)
    coffee = VerifyRecord(order_id=order.id, merchant_id=merchant.id, item_name="咖啡", verify_status=VerifyStatus.UNVERIFIED)
    db_session.add_all([ticket, coffee])
    db_session.commit()

    # 测试前置拦截：尝试直接核销咖啡
    response = client.post(f"/api/verify-records/{coffee.id}/verify?post=coffee")
    assert response.status_code == 409
    assert "门票未核销" in response.json()["detail"]

    # 正常核销门票
    response = client.post(f"/api/verify-records/{ticket.id}/verify?post=gate")
    assert response.status_code == 200
    db_session.refresh(order)
    assert order.entered_at is not None 

    # 测试未结束生命周期分账拦截
    headers = build_signature_headers("POST", f"/api/order/{order.id}/split")
    response = client.post(f"/api/order/{order.id}/split", headers=headers)
    assert response.status_code == 409
    # ✅ 修复点 1：匹配你后端实际输出的中文提示
    assert "生命周期进行中" in response.json()["detail"]

# ===========================================================================
# 测试用例 2：接口防篡改与防重放（HMAC 签名校验）
# ===========================================================================
def test_hmac_signature_security(client, db_session):
    order = Order(order_no="TEST002", total_amount=100.00, status=OrderStatus.PAID)
    db_session.add(order)
    db_session.commit()

    path = f"/api/order/{order.id}/refund"

    # 无签名直接拦截
    response = client.post(path)
    assert response.status_code == 403
    assert "缺少 X-Timestamp" in response.json()["detail"]

    # 错误签名拦截
    headers = build_signature_headers("POST", path)
    headers["X-Signature"] = "fake_signature_123"
    response = client.post(path, headers=headers)
    assert response.status_code == 403

    # 过期时间戳拦截
    old_timestamp = int(time.time()) - 400
    headers = build_signature_headers("POST", path, timestamp=old_timestamp)
    response = client.post(path, headers=headers)
    assert response.status_code == 403
    assert "已过期" in response.json()["detail"]

# ===========================================================================
# 测试用例 3：资金安全 - 并发下的防重复分账 (杀手锏)
# ===========================================================================
def test_concurrent_split_protection(client, db_session):
    import concurrent.futures
    from sqlalchemy import text

    # 1. 准备数据
    merchant = Merchant(name="分账商户", split_rate=0.10)
    db_session.add(merchant)
    db_session.commit()
    
    order = Order(order_no="TEST003", total_amount=100.00, status=OrderStatus.PAID, entered_at=_utcnow_naive())
    db_session.add(order)
    db_session.commit()
    
    record = VerifyRecord(order_id=order.id, merchant_id=merchant.id, item_name="门票", verify_status=VerifyStatus.VERIFIED)
    db_session.add(record)
    db_session.commit()

    # 🚨 核心修改：数据准备好了，立刻关闭主线程的 session，释放连接！
    test_order_id = order.id 
    db_session.close()

    # 2. 准备并发请求
    path = f"/api/order/{order.id}/split"
    def make_request():
        headers = build_signature_headers("POST", path)
        # 注意：这里用 test_client 模拟真实 HTTP 请求，不走主线程的 db_session
        return client.post(path, headers=headers)

    # 3. 开始并发攻击
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(lambda _: make_request(), range(10)))

    # 4. 统计结果
    success_count = sum(1 for r in results if r.status_code == 200)
    conflict_count = sum(1 for r in results if r.status_code == 409)
    
    assert success_count == 1, f"并发攻击下产生了多笔分账流水！实际成功数：{success_count}"
    assert conflict_count == 9, f"并发拦截失效！实际拦截数：{conflict_count}"

    # 5. 终极验证：重新开个会话，用原生 SQL 查最终状态（绕过 ORM 缓存）
    verify_db = TestingSessionLocal()
    try:
        # 直接查字符串状态，防止 ORM 状态不一致
        result = verify_db.execute(
            text("SELECT status FROM orders WHERE id = :order_id"), 
            {"order_id": test_order_id}
        ).scalar_one()
        assert result == "split", f"数据库最终状态不是 split，而是：{result}"
    finally:
        verify_db.close()
