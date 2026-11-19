"""齐契闸口：入站校验、值班放行、整笔回滚、编排台账。

工程凭证只择一：本仓启用「旁路齐契凭证表」。契约换版时凭证在同一事务内
原子切换；入站与放行读的都是凭证快照，因此「齐契」与「可入库」同源。
凭证缺失（缺契）当场拦截，不再出现静默成功；未知列不在凭证字段清单内，
整笔拒收，不落库。
"""

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta

from app.db import get_db
from app.services.errors import ServiceError

CST = timezone(timedelta(hours=8))

VERDICT_MATCHED = "齐契"
VERDICT_NO_CREDENTIAL = "缺契"
VERDICT_STALE = "凭证陈旧"
VERDICT_UNKNOWN = "未知列"
VERDICT_BAD_TYPE = "类型不符"
VERDICT_EMPTY = "空批次"
VERDICT_BAD_ROW = "行格式不符"

STATUS_PENDING = "待放行"
STATUS_STORED = "已入库"
STATUS_REJECTED = "拒收"
STATUS_ROLLED_BACK = "已回滚"

FATE = {
    STATUS_PENDING: "待放行",
    STATUS_STORED: "齐契入库",
    STATUS_ROLLED_BACK: "整笔回滚",
}

PLAIN_REJECT = {
    VERDICT_NO_CREDENTIAL: "缺契：这张表没有现行齐契凭证，入站先拦下，未落库。",
    VERDICT_STALE: "凭证陈旧：齐契凭证与现行契约对不上，入站先拦下，未落库。",
    VERDICT_UNKNOWN: "报文里有凭证没登记的列，整笔拒收，未落库。",
    VERDICT_BAD_TYPE: "字段类型对不上凭证，整笔拒收，未落库。",
    VERDICT_EMPTY: "报文是空的，没有可入库的行。",
    VERDICT_BAD_ROW: "报文里有不是对象的行，整笔拒收，未落库。",
}


def now_text() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(table_name: str, version_no: int, fields: dict[str, str]) -> str:
    blob = _json(
        {
            "table": table_name,
            "version": int(version_no),
            "fields": [[k, fields[k]] for k in sorted(fields)],
        }
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def contract_fields(conn: sqlite3.Connection, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def issue_credential(conn: sqlite3.Connection, contract: sqlite3.Row) -> dict:
    """为一张现行契约签发（或原子刷新）齐契凭证。"""
    fields = contract_fields(conn, contract["id"])
    fp = _fingerprint(contract["table_name"], contract["version_no"], fields)
    conn.execute(
        "INSERT INTO gate_credentials(table_name, contract_id, version_no, field_spec, fingerprint, issued_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(table_name) DO UPDATE SET "
        "contract_id=excluded.contract_id, version_no=excluded.version_no, "
        "field_spec=excluded.field_spec, fingerprint=excluded.fingerprint, "
        "issued_at=excluded.issued_at",
        (
            contract["table_name"],
            contract["id"],
            contract["version_no"],
            _json(fields),
            fp,
            now_text(),
        ),
    )
    return get_credential(conn, contract["table_name"])


def revoke_credential(conn: sqlite3.Connection, table_name: str) -> None:
    conn.execute("DELETE FROM gate_credentials WHERE table_name=?", (table_name,))


def get_credential(conn: sqlite3.Connection, table_name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM gate_credentials WHERE table_name=?", (table_name,)
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["fields"] = json.loads(data.pop("field_spec"))
    return data


def _type_ok(field_type: str, value) -> bool:
    ft = field_type.upper()
    if value is None:
        return True
    if ft in {"INT", "INTEGER", "BIGINT"}:
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, str) and value.strip().lstrip("-").isdigit()
    if ft in {"DECIMAL", "NUMERIC", "FLOAT", "DOUBLE"}:
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str):
            try:
                float(value)
                return True
            except ValueError:
                return False
        return False
    if ft in {"DATE", "TIMESTAMP", "DATETIME"}:
        return isinstance(value, str)
    return True


def _live_matches_credential(
    conn: sqlite3.Connection, table_name: str, credential: dict
) -> bool:
    """凭证必须与现行契约同源：字段与版本都一致才算齐契。"""
    live = conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
        (table_name,),
    ).fetchone()
    if not live:
        return False
    if int(live["version_no"]) != int(credential["version_no"]):
        return False
    return contract_fields(conn, live["id"]) == credential["fields"]


def _evaluate(conn: sqlite3.Connection, table_name: str, rows: list) -> dict:
    """入站校验与可入库判断的唯一入口（齐契与可入库同源）。"""
    credential = get_credential(conn, table_name)
    if credential is None:
        rejected = [
            {"row_no": i + 1, "reason": VERDICT_NO_CREDENTIAL, "columns": []}
            for i in range(len(rows))
        ]
        return {
            "verdict": VERDICT_NO_CREDENTIAL,
            "detail": PLAIN_REJECT[VERDICT_NO_CREDENTIAL],
            "rejected_rows": rejected,
            "accepted": 0,
            "credential": None,
        }
    if not _live_matches_credential(conn, table_name, credential):
        rejected = [
            {"row_no": i + 1, "reason": VERDICT_STALE, "columns": []}
            for i in range(len(rows))
        ]
        return {
            "verdict": VERDICT_STALE,
            "detail": PLAIN_REJECT[VERDICT_STALE],
            "rejected_rows": rejected,
            "accepted": 0,
            "credential": None,
        }
    if not rows:
        return {
            "verdict": VERDICT_EMPTY,
            "detail": PLAIN_REJECT[VERDICT_EMPTY],
            "rejected_rows": [],
            "accepted": 0,
            "credential": credential,
        }

    spec = credential["fields"]
    rejected: list[dict] = []
    accepted = 0
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            rejected.append(
                {"row_no": idx + 1, "reason": VERDICT_BAD_ROW, "columns": []}
            )
            continue
        unknown = sorted(k for k in row if k not in spec)
        if unknown:
            rejected.append(
                {"row_no": idx + 1, "reason": VERDICT_UNKNOWN, "columns": unknown}
            )
            continue
        bad = sorted(
            k for k, v in row.items() if k in spec and not _type_ok(spec[k], v)
        )
        if bad:
            rejected.append(
                {"row_no": idx + 1, "reason": VERDICT_BAD_TYPE, "columns": bad}
            )
            continue
        accepted += 1

    if rejected:
        reasons = {r["reason"] for r in rejected}
        verdict = VERDICT_UNKNOWN if VERDICT_UNKNOWN in reasons else (
            VERDICT_BAD_ROW if VERDICT_BAD_ROW in reasons else VERDICT_BAD_TYPE
        )
        return {
            "verdict": verdict,
            "detail": PLAIN_REJECT[verdict],
            "rejected_rows": rejected,
            "accepted": accepted,
            "credential": credential,
        }

    return {
        "verdict": VERDICT_MATCHED,
        "detail": f"齐契通过：{len(rows)} 行字段都在凭证 v{credential['version_no']} 内。",
        "rejected_rows": [],
        "accepted": accepted,
        "credential": credential,
    }


def batch_fate(row: dict) -> str:
    if row["status"] == STATUS_REJECTED:
        return {
            VERDICT_NO_CREDENTIAL: "缺契拦截",
            VERDICT_STALE: "凭证陈旧拦截",
            VERDICT_UNKNOWN: "未知列拦截",
            VERDICT_BAD_TYPE: "类型拦截",
            VERDICT_EMPTY: "空批拦截",
            VERDICT_BAD_ROW: "格式拦截",
        }.get(row["verdict"], "校验拦截")
    return FATE.get(row["status"], row["status"])


def _log(
    conn: sqlite3.Connection,
    action: str,
    outcome: str,
    message: str,
    operator: str = "值班",
    batch_id: int | None = None,
    table_name: str = "",
) -> None:
    conn.execute(
        "INSERT INTO gate_events(batch_id, table_name, action, outcome, message, operator) "
        "VALUES (?,?,?,?,?,?)",
        (batch_id, table_name, action, outcome, message, operator),
    )


def _make_batch_no() -> str:
    stamp = datetime.now(CST).strftime("%Y%m%d%H%M%S%f")
    return f"b-{stamp}-{secrets.token_hex(3)}"


def ingest(
    table_name: str,
    rows: list,
    batch_no: str | None = None,
    operator: str = "值班",
    db_path: str | None = None,
) -> dict:
    """接一票入站报文。缺契/未知列当场拒收，绝不落库。"""
    table_name = (table_name or "").strip()
    if not table_name:
        raise ServiceError("没填表名，入站无从校验", 400)
    if not isinstance(rows, list):
        raise ServiceError("报文体必须是行数组", 400)

    with get_db(db_path) as conn:
        result = _evaluate(conn, table_name, rows)
        credential = result["credential"]
        status = STATUS_PENDING if result["verdict"] == VERDICT_MATCHED else STATUS_REJECTED
        detail = result["detail"]
        try:
            cur = conn.execute(
                "INSERT INTO ingest_batches(batch_no, table_name, payload, row_count, status, "
                "verdict, detail, credential_fingerprint, rejected_rows, accepted_rows, operator) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_no or _make_batch_no(),
                    table_name,
                    _json(rows),
                    len(rows),
                    status,
                    result["verdict"],
                    detail,
                    credential["fingerprint"] if credential else None,
                    _json(result["rejected_rows"]),
                    result["accepted"] if status == STATUS_PENDING else 0,
                    operator,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "batch_no" in str(exc):
                raise ServiceError("批次号已用过，换一个再送", 409) from exc
            raise ServiceError(f"入站落台账失败：{exc}", 500) from exc
        bid = cur.lastrowid
        _log(
            conn,
            "入站",
            "放行待值班" if status == STATUS_PENDING else "拒收",
            detail,
            operator,
            bid,
            table_name,
        )
        return get_batch(bid, db_path=db_path, conn=conn)


def get_batch(
    batch_id: int,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    if conn is None:
        with get_db(db_path) as c:
            return get_batch(batch_id, conn=c)
    row = conn.execute(
        "SELECT * FROM ingest_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not row:
        raise ServiceError("入站批次不存在", 404)
    data = dict(row)
    data["rejected_rows"] = json.loads(data["rejected_rows"] or "[]")
    data["payload"] = json.loads(data["payload"] or "[]")
    data["fate"] = batch_fate(data)
    data["stored_rows"] = [
        dict(r)
        for r in conn.execute(
            "SELECT row_no, data, credential_fingerprint FROM ingest_rows "
            "WHERE batch_id=? ORDER BY row_no",
            (batch_id,),
        ).fetchall()
    ]
    for item in data["stored_rows"]:
        item["data"] = json.loads(item["data"])
    data["events"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM gate_events WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
    ]
    return data


def release(batch_id: int, operator: str = "值班", db_path: str | None = None) -> dict:
    """值班点放行：按凭证当场改库。失败吐白话并整笔回滚。"""
    failure: ServiceError | None = None
    result: dict | None = None
    with get_db(db_path, immediate=True) as conn:
        batch = conn.execute(
            "SELECT * FROM ingest_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("入站批次不存在", 404)

        # 缺排他会双写放行状态：BEGIN IMMEDIATE 串行化 + 状态守卫。
        if batch["status"] == STATUS_STORED:
            _log(conn, "放行", "拒绝·重复", "已放行过，别重复点", operator, batch_id, batch["table_name"])
            failure = ServiceError("已放行过，别重复点", 409)
        elif batch["status"] == STATUS_ROLLED_BACK:
            _log(conn, "放行", "拒绝·已回滚", "这票已整笔回滚，别重放", operator, batch_id, batch["table_name"])
            failure = ServiceError("这票已整笔回滚，别重放", 409)
        elif batch["status"] == STATUS_PENDING:
            rows = json.loads(batch["payload"] or "[]")
            verdict = _evaluate(conn, batch["table_name"], rows)
            if verdict["verdict"] != VERDICT_MATCHED:
                # 齐契与可入库同源：放行时按凭证再核一遍，缺契一律拦下。
                detail = verdict["detail"]
                conn.execute(
                    "UPDATE ingest_batches SET status=?, verdict=?, detail=?, "
                    "rejected_rows=?, accepted_rows=0 WHERE id=?",
                    (
                        STATUS_REJECTED,
                        verdict["verdict"],
                        detail,
                        _json(verdict["rejected_rows"]),
                        batch_id,
                    ),
                )
                _log(conn, "放行", "拒绝", detail, operator, batch_id, batch["table_name"])
                failure = ServiceError(detail, 409)
            else:
                credential = verdict["credential"]
                try:
                    for idx, row in enumerate(rows, start=1):
                        conn.execute(
                            "INSERT INTO ingest_rows(batch_id, row_no, data, credential_fingerprint) "
                            "VALUES (?,?,?,?)",
                            (batch_id, idx, _json(row), credential["fingerprint"]),
                        )
                except sqlite3.Error as exc:
                    # 当场抛出，让 get_db 整笔回滚：不允许半票入库。
                    raise ServiceError(
                        f"入库失败，已整笔回滚：{exc}", 500
                    ) from exc
                conn.execute(
                    "UPDATE ingest_batches SET status=?, verdict=?, detail=?, "
                    "accepted_rows=?, credential_fingerprint=?, released_at=? WHERE id=?",
                    (
                        STATUS_STORED,
                        VERDICT_MATCHED,
                        f"齐契放行：按凭证 v{credential['version_no']} 入库 {len(rows)} 行。",
                        len(rows),
                        credential["fingerprint"],
                        now_text(),
                        batch_id,
                    ),
                )
                _log(
                    conn,
                    "放行",
                    "已入库",
                    f"按凭证 v{credential['version_no']} 入库 {len(rows)} 行",
                    operator,
                    batch_id,
                    batch["table_name"],
                )
        else:  # 拒收
            _log(
                conn,
                "放行",
                "拒绝",
                "这票入站已被拒收，不能放行",
                operator,
                batch_id,
                batch["table_name"],
            )
            failure = ServiceError("这票入站已拒收，不能放行", 409)

        if failure is None:
            result = get_batch(batch_id, conn=conn)
    if failure is not None:
        raise failure
    return result


def rollback(batch_id: int, operator: str = "值班", db_path: str | None = None) -> dict:
    """整笔回滚一票已误入库的行；回滚后复验不得再见到这些行。"""
    failure: ServiceError | None = None
    result: dict | None = None
    with get_db(db_path, immediate=True) as conn:
        batch = conn.execute(
            "SELECT * FROM ingest_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("入站批次不存在", 404)
        if batch["status"] == STATUS_ROLLED_BACK:
            failure = ServiceError("这票已经整笔回滚过", 409)
        elif batch["status"] != STATUS_STORED:
            failure = ServiceError("这票还没入库，没有可回滚的行", 409)
        else:
            cur = conn.execute("DELETE FROM ingest_rows WHERE batch_id=?", (batch_id,))
            removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.execute(
                "UPDATE ingest_batches SET status=?, verdict=?, detail=?, rolled_back_rows=?, "
                "rolled_back_at=? WHERE id=?",
                (
                    STATUS_ROLLED_BACK,
                    "整笔回滚",
                    f"整笔回滚 {removed} 行，表上不留这票数据。",
                    removed,
                    now_text(),
                    batch_id,
                ),
            )
            _log(
                conn,
                "整笔回滚",
                "已回滚",
                f"回滚 {removed} 行",
                operator,
                batch_id,
                batch["table_name"],
            )
            result = get_batch(batch_id, conn=conn)
    if failure is not None:
        raise failure
    return result


def list_batches(
    db_path: str | None = None,
    table_name: str | None = None,
    status: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM ingest_batches"
    clauses = []
    params: list = []
    if table_name:
        clauses.append("table_name=?")
        params.append(table_name)
    if status:
        clauses.append("status=?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC"
    with get_db(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            r["rejected_rows"] = json.loads(r["rejected_rows"] or "[]")
            r["payload"] = json.loads(r["payload"] or "[]")
            r["fate"] = batch_fate(r)
        return rows


def list_events(db_path: str | None = None, limit: int = 200) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gate_events ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]


def gate_policy(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        policy = conn.execute("SELECT * FROM gate_policy WHERE id=1").fetchone()
        credentials = [
            dict(
                r,
                fields=json.loads(r["field_spec"]),
            )
            for r in conn.execute(
                "SELECT * FROM gate_credentials ORDER BY table_name"
            ).fetchall()
        ]
        for c in credentials:
            c.pop("field_spec", None)
        return {
            "mechanism": policy["mechanism"] if policy else None,
            "rationale": policy["rationale"] if policy else "",
            "switched_at": policy["switched_at"] if policy else None,
            "credentials": credentials,
        }


def gate_check(table_name: str, rows: list | None = None, db_path: str | None = None) -> dict:
    """页上核闸口：给出这张表此刻齐契与否，以及若入站会怎么判。

    判定走与入站同一个 _evaluate，因此页面结论和放行判断不会各说各话。
    """
    table_name = (table_name or "").strip()
    with get_db(db_path) as conn:
        credential = get_credential(conn, table_name)
        policy = conn.execute("SELECT * FROM gate_policy WHERE id=1").fetchone()
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (table_name,),
        ).fetchone()
        drift = None
        if live and credential:
            if not _live_matches_credential(conn, table_name, credential):
                if int(live["version_no"]) != int(credential["version_no"]):
                    drift = "凭证版本与现行契约不一致：可入库判断已陈旧"
                else:
                    drift = "凭证字段与现行契约不一致：可入库判断已陈旧"
        elif not live and credential:
            drift = "凭证还在，但表已无现行契约：凭证陈旧"

        # 没给样例行时只核准入；给了样例行就与入站走同一个 _evaluate，
        # 页面结论和放行判断不会各说各话。
        if rows is None:
            if credential is None:
                verdict = VERDICT_NO_CREDENTIAL
                detail = PLAIN_REJECT[VERDICT_NO_CREDENTIAL]
            elif drift is not None or not _live_matches_credential(conn, table_name, credential):
                verdict = VERDICT_STALE
                detail = PLAIN_REJECT[VERDICT_STALE]
            else:
                verdict = VERDICT_MATCHED
                detail = (
                    f"齐契：凭证 v{credential['version_no']}，"
                    f"字段 {', '.join(sorted(credential['fields']))}"
                )
            sample_rejected: list[dict] = []
        else:
            probe = _evaluate(conn, table_name, rows)
            verdict = probe["verdict"]
            detail = probe["detail"]
            sample_rejected = probe["rejected_rows"]

        return {
            "table_name": table_name,
            "mechanism": policy["mechanism"] if policy else None,
            "credential": credential,
            "has_live_contract": bool(live),
            "drift": drift,
            "attached": verdict in {VERDICT_MATCHED, VERDICT_EMPTY} and drift is None,
            "verdict": verdict,
            "detail": detail,
            "sample_rejected": sample_rejected,
        }


def sample_batch(
    conn: sqlite3.Connection,
    batch_no: str,
    table_name: str,
    rows: list,
    status: str,
    verdict: str,
    detail: str,
    operator: str = "值班",
    rejected_rows: list | None = None,
    stored: bool = False,
) -> None:
    """仅用于首次建库摆一票样例，走与真实入站相同的表结构。"""
    credential = get_credential(conn, table_name)
    cur = conn.execute(
        "INSERT INTO ingest_batches(batch_no, table_name, payload, row_count, status, verdict, "
        "detail, credential_fingerprint, rejected_rows, accepted_rows, operator, released_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            batch_no,
            table_name,
            _json(rows),
            len(rows),
            status,
            verdict,
            detail,
            credential["fingerprint"] if credential else None,
            _json(rejected_rows or []),
            len(rows) if status == STATUS_STORED else 0,
            operator,
            now_text() if status == STATUS_STORED else None,
        ),
    )
    bid = cur.lastrowid
    if stored and status == STATUS_STORED and credential:
        for idx, row in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO ingest_rows(batch_id, row_no, data, credential_fingerprint) "
                "VALUES (?,?,?,?)",
                (bid, idx, _json(row), credential["fingerprint"]),
            )
    _log(
        conn,
        "入站",
        "已入库" if status == STATUS_STORED else "拒收",
        detail,
        operator,
        bid,
        table_name,
    )
