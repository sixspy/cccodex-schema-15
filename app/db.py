import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, check_same_thread=False, timeout=15, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db(db_path: str | None = None, immediate: bool = False):
    """打开一个事务。

    immediate=True 用 BEGIN IMMEDIATE 先取写锁，供放行 / 换版 / 回滚这类
    必须串行化的写路径使用；缺排他会两次写同一放行状态。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS contract_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  field_name TEXT NOT NULL,
  field_type TEXT NOT NULL,
  UNIQUE(contract_id, field_name)
);

CREATE TABLE IF NOT EXISTS catalog_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  live_version INTEGER NOT NULL,
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


GATE_SCHEMA = """
-- 工程凭证只择一：本表被 CHECK(id=1) 钉死为单行。
CREATE TABLE IF NOT EXISTS gate_policy (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  mechanism TEXT NOT NULL CHECK (mechanism IN ('凭证表','唯一索引')),
  rationale TEXT NOT NULL,
  switched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 旁路齐契凭证表：入站校验与可入库判断的唯一依据。
CREATE TABLE IF NOT EXISTS gate_credentials (
  table_name TEXT PRIMARY KEY,
  contract_id INTEGER NOT NULL,
  version_no INTEGER NOT NULL,
  field_spec TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  issued_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_no TEXT NOT NULL UNIQUE,
  table_name TEXT NOT NULL,
  payload TEXT NOT NULL,
  row_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('待放行','已入库','拒收','已回滚')),
  verdict TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  credential_fingerprint TEXT,
  rejected_rows TEXT NOT NULL DEFAULT '[]',
  accepted_rows INTEGER NOT NULL DEFAULT 0,
  rolled_back_rows INTEGER NOT NULL DEFAULT 0,
  rolled_back_at TEXT,
  operator TEXT NOT NULL DEFAULT '值班',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  released_at TEXT
);

CREATE TABLE IF NOT EXISTS ingest_rows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES ingest_batches(id),
  row_no INTEGER NOT NULL,
  data TEXT NOT NULL,
  credential_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER,
  table_name TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  outcome TEXT NOT NULL,
  message TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT '值班',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ingest_rows_batch ON ingest_rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_ingest_batches_status ON ingest_batches(status);
"""


SEED = [
    ("dw_orders_d", 1, "现行", "初始现行", [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE")]),
    ("dw_orders_d", 2, "草稿", "拟删 amount 改币种", [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE")]),
    ("dw_payments_d", 1, "现行", "支付现行", [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")]),
]

GATE_MECHANISM = "凭证表"
GATE_RATIONALE = (
    "齐契闸口只择一工程凭证：启用旁路齐契凭证表（原子切换 + 整笔回滚）。"
    "入站校验读取凭证快照而非可变契约行，凭证未命中即拦，未知列无法混进入库；"
    "唯一索引方案未启用（它只能约束一表一现行，挡不住未知列）。"
)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "reconcile_reports", "detail_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "reconcile_reports", "orphan_catalog", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "reconcile_reports", "purged_missing_rows", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "reconcile_reports", "contract_missing_rows", "INTEGER NOT NULL DEFAULT 0")


def _fields_for(conn: sqlite3.Connection, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def _seed_gate(conn: sqlite3.Connection, with_samples: bool = False) -> None:
    from app.services.gate import issue_credential, revoke_credential, sample_batch

    conn.execute(
        "INSERT OR IGNORE INTO gate_policy(id, mechanism, rationale) VALUES (1, ?, ?)",
        (GATE_MECHANISM, GATE_RATIONALE),
    )
    live = conn.execute(
        "SELECT * FROM schema_contracts WHERE status='现行'"
    ).fetchall()
    live_tables = {c["table_name"] for c in live}
    for contract in live:
        issue_credential(conn, contract)
    # 没有现行契约的表不留旧凭证：缺契必须是真缺契，不能靠陈旧凭证放行。
    for row in conn.execute("SELECT table_name FROM gate_credentials").fetchall():
        if row["table_name"] not in live_tables:
            revoke_credential(conn, row["table_name"])

    # 样例只在首次建库时摆；升级已有库不往里塞假入站记录。
    if not with_samples:
        return
    if conn.execute("SELECT value FROM meta WHERE key='gate_seeded'").fetchone():
        return

    sample_batch(
        conn,
        "b-20261118-001",
        "dw_orders_d",
        [
            {"order_id": 1, "amount": "12.50", "biz_date": "2026-11-18"},
            {"order_id": 2, "amount": "7.00", "biz_date": "2026-11-18"},
        ],
        status="已入库",
        verdict="齐契",
        detail="齐契通过，值班放行后入库 2 行",
        operator="值班甲",
        stored=True,
    )
    sample_batch(
        conn,
        "b-20261118-002",
        "dw_payments_d",
        [{"pay_id": 1, "order_id": 9, "paid_at": "2026-11-18 10:00:00", "channel": "alipay"}],
        status="拒收",
        verdict="未知列",
        detail="第1行含未知列 channel；整笔拒收，未落库",
        rejected_rows=[{"row_no": 1, "reason": "未知列", "columns": ["channel"]}],
    )
    sample_batch(
        conn,
        "b-20261118-003",
        "dw_refunds_d",
        [{"refund_id": 1, "order_id": 9}],
        status="拒收",
        verdict="缺契",
        detail="表 dw_refunds_d 无齐契凭证（现行契约缺失），入站被拦，未落库",
        rejected_rows=[{"row_no": 1, "reason": "缺契", "columns": []}],
    )
    conn.execute("INSERT INTO meta(key, value) VALUES ('gate_seeded','1')")


def _prepare(db_path: str | None = None) -> None:
    """建库前把 WAL 打开并建表；DDL 走 executescript，必须在事务外。"""
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.executescript(GATE_SCHEMA)
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    _prepare(db_path)
    with get_db(db_path) as conn:
        _migrate(conn)
        first_run = not conn.execute(
            "SELECT value FROM meta WHERE key='seeded'"
        ).fetchone()
        if not first_run:
            _seed_gate(conn)
            return
        for table, ver, status, note, fields in SEED:
            cur = conn.execute(
                "INSERT INTO schema_contracts(table_name, version_no, status, note) VALUES (?,?,?,?)",
                (table, ver, status, note),
            )
            cid = cur.lastrowid
            for fname, ftype in fields:
                conn.execute(
                    "INSERT INTO contract_fields(contract_id, field_name, field_type) VALUES (?,?,?)",
                    (cid, fname, ftype),
                )
            if status == "现行":
                conn.execute(
                    "INSERT OR REPLACE INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                    (table, ver),
                )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        _seed_gate(conn, with_samples=True)
