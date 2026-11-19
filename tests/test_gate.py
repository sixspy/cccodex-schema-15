"""齐契闸口：入站校验、放行、整笔回滚、对账复验。"""

from app.db import connect
from app.services.gate import release


async def _ingest(client, table, rows, **extra):
    r = await client.post("/api/ingest", json={"table_name": table, "rows": rows, **extra})
    return r, await r.json()


async def test_gate_policy_is_single_credential_mechanism(authed):
    gate = (await (await authed.get("/api/gate")).json())["gate"]
    assert gate["mechanism"] == "凭证表"
    assert "唯一索引" in gate["rationale"]
    tables = {c["table_name"] for c in gate["credentials"]}
    assert {"dw_orders_d", "dw_payments_d"} <= tables


async def test_unknown_column_rejected_and_not_stored(authed):
    r, body = await _ingest(
        authed,
        "dw_orders_d",
        [{"order_id": 1, "amount": "1.0", "biz_date": "2026-11-19", "ghost": "x"}],
    )
    assert r.status == 422
    assert body["ok"] is False
    assert body["batch"]["status"] == "拒收"
    assert body["batch"]["verdict"] == "未知列"
    assert body["batch"]["stored_rows"] == []
    assert body["batch"]["rejected_rows"][0]["columns"] == ["ghost"]
    assert "未落库" in body["message"]


async def test_missing_credential_blocks_quiet_success(authed):
    r, body = await _ingest(authed, "dw_refunds_d", [{"refund_id": 1}])
    assert body["batch"]["status"] == "拒收"
    assert body["batch"]["verdict"] == "缺契"
    assert body["batch"]["stored_rows"] == []
    assert "缺契" in body["message"]

    # 拒收票不允许被放行，避免「缺契静默成功」。
    again = await authed.post(f"/api/ingest-batches/{body['batch']['id']}/release", json={})
    assert again.status == 409
    assert "拒收" in (await again.json())["error"]


async def test_gate_check_same_source_as_ingest(authed):
    ok = (await (await authed.post("/api/gate/check", json={"table_name": "dw_orders_d"})).json())["check"]
    assert ok["attached"] is True
    assert ok["verdict"] == "齐契"

    missing = (await (await authed.post("/api/gate/check", json={"table_name": "nope_t"})).json())["check"]
    assert missing["attached"] is False
    assert missing["verdict"] == "缺契"

    # 带样例行时，核闸口结论与入站判定必须一致。
    sample = (await (await authed.post(
        "/api/gate/check",
        json={"table_name": "dw_orders_d", "rows": [{"order_id": 1, "bogus": 1}]},
    )).json())["check"]
    assert sample["verdict"] == "未知列"


async def test_apply_switches_credential_atomically(authed):
    items = (await (await authed.get("/api/contracts")).json())["items"]
    draft = next(c for c in items if c["status"] == "草稿")
    await authed.post(f"/api/contracts/{draft['id']}/apply", json={})

    gate = (await (await authed.get("/api/gate")).json())["gate"]
    cred = next(c for c in gate["credentials"] if c["table_name"] == "dw_orders_d")
    assert cred["version_no"] == 2
    assert "currency" in cred["fields"] and "amount" not in cred["fields"]

    # 旧列在新凭证下就是未知列，立刻拦。
    _, body = await _ingest(authed, "dw_orders_d", [{"order_id": 2, "amount": "5", "biz_date": "x"}])
    assert body["batch"]["verdict"] == "未知列"
    assert body["batch"]["stored_rows"] == []

    # 新字段齐契，可入。
    _, body = await _ingest(
        authed, "dw_orders_d", [{"order_id": 2, "currency": "CNY", "biz_date": "2026-11-19"}]
    )
    assert body["batch"]["status"] == "待放行"


async def test_release_then_rollback(authed):
    _, body = await _ingest(authed, "dw_orders_d", [{"order_id": 3, "amount": "1", "biz_date": "x"}])
    bid = body["batch"]["id"]
    assert body["batch"]["status"] == "待放行"

    rel = await authed.post(f"/api/ingest-batches/{bid}/release", json={})
    assert rel.status == 200
    stored = (await rel.json())["batch"]
    assert stored["status"] == "已入库"
    assert len(stored["stored_rows"]) == 1
    assert stored["credential_fingerprint"]

    again = await authed.post(f"/api/ingest-batches/{bid}/release", json={})
    assert again.status == 409

    rb = await authed.post(f"/api/ingest-batches/{bid}/rollback", json={})
    assert rb.status == 200
    rolled = (await rb.json())["batch"]
    assert rolled["status"] == "已回滚"
    assert rolled["stored_rows"] == []
    assert rolled["rolled_back_rows"] == 1


async def test_concurrent_release_writes_once(authed, tmp_path):
    _, body = await _ingest(authed, "dw_orders_d", [{"order_id": 4, "amount": "1", "biz_date": "x"}])
    bid = body["batch"]["id"]
    db_path = str(tmp_path / "test.db")

    import threading

    outcomes: list[str] = []

    def worker():
        try:
            release(bid, db_path=db_path)
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert all(o == "ok" or "已放行" in o for o in outcomes)
    batch = (await (await authed.get(f"/api/ingest-batches/{bid}")).json())["batch"]
    assert len(batch["stored_rows"]) == 1


async def test_reconcile_purges_missing_credential_rows(authed, tmp_path):
    _, body = await _ingest(authed, "dw_orders_d", [{"order_id": 5, "amount": "1", "biz_date": "x"}])
    bid = body["batch"]["id"]
    await authed.post(f"/api/ingest-batches/{bid}/release", json={})

    # 模拟凭证被撤（缺契），已入库行必须在对账复验里被清掉。
    conn = connect(str(tmp_path / "test.db"))
    conn.execute("DELETE FROM gate_credentials WHERE table_name='dw_orders_d'")
    conn.commit()
    conn.close()

    rep = (await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-19"})).json())["report"]
    assert rep["contract_missing_rows"] == 0
    assert rep["purged_missing_rows"] >= 1
    assert any(p["batch_id"] == bid for p in rep["detail"]["purged_batches"])

    batch = (await (await authed.get(f"/api/ingest-batches/{bid}")).json())["batch"]
    assert batch["stored_rows"] == []
    assert batch["status"] == "已回滚"


async def test_reconcile_flags_orphan_catalog(authed, tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    conn.execute(
        "INSERT INTO catalog_tables(table_name, live_version) VALUES ('dw_ghost_d', 1)"
    )
    conn.commit()
    conn.close()

    rep = (await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-19"})).json())["report"]
    assert rep["orphan_catalog"] == 1
    assert rep["drifted"] >= 1
    assert any(e["state"] == "目录孤儿" and e["table_name"] == "dw_ghost_d" for e in rep["detail"]["entries"])


async def test_gate_ledger_records_each_action(authed):
    _, body = await _ingest(authed, "dw_orders_d", [{"order_id": 6, "amount": "1", "biz_date": "x"}])
    bid = body["batch"]["id"]
    await authed.post(f"/api/ingest-batches/{bid}/release", json={})
    await authed.post(f"/api/ingest-batches/{bid}/rollback", json={})

    events = (await (await authed.get("/api/gate-events")).json())["items"]
    actions = [(e["batch_id"], e["action"], e["outcome"]) for e in events]
    assert (bid, "入站", "放行待值班") in actions
    assert (bid, "放行", "已入库") in actions
    assert (bid, "整笔回滚", "已回滚") in actions


async def test_rejection_is_whole_batch(authed):
    r, body = await _ingest(
        authed,
        "dw_orders_d",
        [
            {"order_id": 10, "amount": "1", "biz_date": "x"},
            {"order_id": 11, "amount": "1", "biz_date": "x", "ghost": 1},
        ],
    )
    assert r.status == 422
    assert body["batch"]["status"] == "拒收"
    assert body["batch"]["accepted_rows"] == 0
    assert body["batch"]["stored_rows"] == []
    assert body["batch"]["rejected_rows"] == [
        {"row_no": 2, "reason": "未知列", "columns": ["ghost"]}
    ]
    # 整笔拒收：连第 1 行合法的也不进库。
    await authed.post(f"/api/ingest-batches/{body['batch']['id']}/release", json={})
    detail = (await (await authed.get(f"/api/ingest-batches/{body['batch']['id']}")).json())["batch"]
    assert detail["stored_rows"] == []
    assert detail["status"] == "拒收"


async def test_stale_credential_blocks_ingest(authed, tmp_path):
    # 凭证还在，但与现行契约对不上（模拟绕过换版直接改了契约行）→ 凭证陈旧。
    conn = connect(str(tmp_path / "test.db"))
    conn.execute(
        "UPDATE schema_contracts SET version_no=9 WHERE table_name='dw_orders_d' AND status='现行'"
    )
    conn.commit()
    conn.close()

    r, body = await _ingest(authed, "dw_orders_d", [{"order_id": 1, "amount": "1", "biz_date": "x"}])
    assert r.status == 422
    assert body["batch"]["verdict"] == "凭证陈旧"
    assert body["batch"]["stored_rows"] == []

    chk = (await (await authed.post("/api/gate/check", json={"table_name": "dw_orders_d"})).json())["check"]
    assert chk["verdict"] == "凭证陈旧"
    assert chk["attached"] is False
    assert chk["drift"]


async def test_empty_batch_rejected_and_not_releasable(authed):
    r, body = await _ingest(authed, "dw_orders_d", [])
    assert r.status == 422
    assert body["batch"]["verdict"] == "空批次"
    rel = await authed.post(f"/api/ingest-batches/{body['batch']['id']}/release", json={})
    assert rel.status == 409


async def test_unauthenticated_gate_calls_are_401(client):
    for path in ("/api/gate", "/api/ingest-batches", "/api/gate-events"):
        r = await client.get(path)
        assert r.status == 401
    r = await client.post("/api/ingest", json={"table_name": "dw_orders_d", "rows": []})
    assert r.status == 401
