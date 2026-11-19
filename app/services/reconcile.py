import json

from app.db import get_db
from app.services.gate import STATUS_ROLLED_BACK, STATUS_STORED


def _current_credentials(conn) -> dict[str, dict]:
    return {
        r["table_name"]: dict(r)
        for r in conn.execute("SELECT * FROM gate_credentials").fetchall()
    }


def run_reconcile(as_of: str, db_path: str | None = None) -> dict:
    """契约 / 目录 / 入站行三方复验。

    - 目录多出来的表（无现行契约）单列「目录孤儿」，不再把虚高藏进「对齐」；
      凭证缺失的表单列「缺契」，不按对齐放行。
    - 缺契已入库行整笔清掉；换版前按旧凭证入库的行只亮「换版前入库」。
    """
    with get_db(db_path, immediate=True) as conn:
        contracts = conn.execute(
            "SELECT table_name, version_no FROM schema_contracts WHERE status='现行'"
        ).fetchall()
        live = {c["table_name"]: c["version_no"] for c in contracts}
        catalog = {
            r["table_name"]: r["live_version"]
            for r in conn.execute("SELECT table_name, live_version FROM catalog_tables")
        }
        credentials = _current_credentials(conn)

        matched = 0
        drifted = 0
        missing_credentials: list[str] = []
        details: list[str] = []
        entries: list[dict] = []

        for table, version in live.items():
            got = catalog.get(table)
            has_cred = table in credentials
            row = {
                "table_name": table,
                "contract_version": version,
                "catalog_version": got,
                "credential_version": (
                    credentials[table]["version_no"] if has_cred else None
                ),
            }
            if not has_cred:
                missing_credentials.append(table)
            if got != version:
                drifted += 1
                row["state"] = "漂移"
                details.append(f"{table}:契约v{version}/目录v{got}")
            elif not has_cred:
                # 目录与契约对上了，但这张表没有齐契凭证：入站必被拦，
                # 不能算「对齐」把问题盖过去。
                drifted += 1
                row["state"] = "缺契"
                details.append(f"{table}:无齐契凭证")
            else:
                matched += 1
                row["state"] = "对齐"
            entries.append(row)

        orphan_catalog = sorted(set(catalog) - set(live))
        for table in orphan_catalog:
            drifted += 1
            entries.append(
                {
                    "table_name": table,
                    "contract_version": None,
                    "catalog_version": catalog[table],
                    "credential_version": (
                        credentials[table]["version_no"] if table in credentials else None
                    ),
                    "state": "目录孤儿",
                }
            )
            details.append(f"{table}:目录v{catalog[table]}无现行契约")

        # 复验入站行。
        # - 这表已无齐契凭证：这些是「缺契已入库行」，整笔清掉，复验结论里
        #   不允许再见到它们。
        # - 凭证在、但指纹对不上：说明是换版前按旧凭证齐契入库的，属历史
        #   事实，只亮「换版前入库」待复核，不擅自动数。
        current_fp = {
            table: cred["fingerprint"] for table, cred in credentials.items()
        }
        missing_batches: set[int] = set()
        legacy_batches: set[int] = set()
        for r in conn.execute(
            "SELECT DISTINCT ir.batch_id, ir.credential_fingerprint, ib.table_name "
            "FROM ingest_rows ir JOIN ingest_batches ib ON ib.id=ir.batch_id"
        ).fetchall():
            table = r["table_name"]
            if table not in current_fp:
                missing_batches.add(r["batch_id"])
            elif current_fp[table] != r["credential_fingerprint"]:
                legacy_batches.add(r["batch_id"])

        purged = 0
        purged_batches: list[dict] = []
        for bid in sorted(missing_batches):
            batch = conn.execute(
                "SELECT table_name FROM ingest_batches WHERE id=?", (bid,)
            ).fetchone()
            cur = conn.execute("DELETE FROM ingest_rows WHERE batch_id=?", (bid,))
            removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            purged += removed
            message = f"对账复验缺契，整笔清掉 {removed} 行"
            conn.execute(
                "UPDATE ingest_batches SET status=?, verdict=?, detail=?, "
                "rolled_back_rows=?, rolled_back_at=datetime('now') WHERE id=?",
                (STATUS_ROLLED_BACK, "复验清理·缺契", message, removed, bid),
            )
            conn.execute(
                "INSERT INTO gate_events(batch_id, table_name, action, outcome, message, operator) "
                "VALUES (?,?,?,?,?,?)",
                (bid, batch["table_name"], "对账复验", "已回滚", message, "对账"),
            )
            purged_batches.append(
                {
                    "batch_id": bid,
                    "table_name": batch["table_name"],
                    "reason": "缺契",
                    "rows": removed,
                }
            )

        legacy_rows: list[dict] = []
        for bid in sorted(legacy_batches):
            batch = conn.execute(
                "SELECT batch_no, table_name FROM ingest_batches WHERE id=?", (bid,)
            ).fetchone()
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM ingest_rows WHERE batch_id=?", (bid,)
            ).fetchone()["n"]
            legacy_rows.append(
                {
                    "batch_id": bid,
                    "batch_no": batch["batch_no"],
                    "table_name": batch["table_name"],
                    "rows": n,
                    "reason": "换版前按旧凭证入库",
                }
            )

        missing_stored = conn.execute(
            "SELECT COUNT(*) AS n FROM ingest_batches ib "
            "WHERE ib.status=? AND NOT EXISTS ("
            " SELECT 1 FROM gate_credentials g WHERE g.table_name=ib.table_name)",
            (STATUS_STORED,),
        ).fetchone()["n"]

        summary = f"对账{as_of}：对齐{matched}，漂移{drifted}"
        if missing_credentials:
            summary += f"，契约缺契{len(missing_credentials)}"
        if orphan_catalog:
            summary += f"，目录孤儿{len(orphan_catalog)}"
        if purged:
            summary += f"，缺契清理{purged}行"
        if legacy_rows:
            summary += f"，换版前入库{len(legacy_rows)}票"
        summary += f"，缺契入库{missing_stored}"
        if details:
            summary += "；" + "，".join(details)

        cur = conn.execute(
            "INSERT INTO reconcile_reports(as_of, matched, drifted, summary, detail_json, "
            "orphan_catalog, purged_missing_rows, contract_missing_rows) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                as_of,
                matched,
                drifted,
                summary,
                json.dumps(
                    {
                        "entries": entries,
                        "purged_batches": purged_batches,
                        "legacy_batches": legacy_rows,
                        "missing_credentials": missing_credentials,
                    },
                    ensure_ascii=False,
                ),
                len(orphan_catalog),
                purged,
                missing_stored,
            ),
        )
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        data = dict(row)
        data["detail"] = json.loads(data.pop("detail_json") or "{}")
        return data


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = []
        for r in conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall():
            data = dict(r)
            data["detail"] = json.loads(data.pop("detail_json") or "{}")
            rows.append(data)
        return rows
