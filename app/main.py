import json
from pathlib import Path

from aiohttp import web
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    apply_contract,
    gate_check,
    gate_policy,
    get_batch,
    ingest,
    list_batches,
    list_contracts,
    list_events,
    list_reports,
    release,
    rollback,
    run_breaking_check,
    run_reconcile,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-schema-session")


def create_app(db_path: str | None = None) -> web.Application:
    if db_path:
        import os

        os.environ["DATABASE_PATH"] = db_path
    init_db(db_path)
    app = web.Application(middlewares=[error_middleware])
    app["db_path"] = db_path
    app.router.add_static("/static/", STATIC)

    app.router.add_get("/", root)
    app.router.add_get("/login", login_page)
    app.router.add_get("/app/", spa_page)

    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/meta", api_meta)

    app.router.add_get("/api/contracts", api_contracts)
    app.router.add_post("/api/contracts/{cid}/breaking-check", api_breaking)
    app.router.add_post("/api/contracts/{cid}/apply", api_apply)

    app.router.add_get("/api/reconcile-reports", api_reports)
    app.router.add_post("/api/reconcile-reports", api_reconcile)

    # 齐契闸口
    app.router.add_get("/api/gate", api_gate)
    app.router.add_post("/api/gate/check", api_gate_check)
    app.router.add_get("/api/ingest-batches", api_batches)
    app.router.add_get("/api/ingest-batches/{bid}", api_batch)
    app.router.add_post("/api/ingest", api_ingest)
    app.router.add_post("/api/ingest-batches/{bid}/release", api_release)
    app.router.add_post("/api/ingest-batches/{bid}/rollback", api_rollback)
    app.router.add_get("/api/gate-events", api_events)
    return app


def _session(request: web.Request) -> dict:
    raw = request.cookies.get("schema_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: web.Request) -> str:
    user = _session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_resp(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
    )


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ServiceError as exc:
        return json_resp({"ok": False, "error": exc.message, "plain": exc.message}, exc.status_code)
    except web.HTTPException:
        raise
    except json.JSONDecodeError:
        return json_resp({"ok": False, "error": "请求体不是合法 JSON"}, 400)
    except Exception as exc:  # noqa: BLE001 - 面向值班吐白话，不吐栈
        return json_resp({"ok": False, "error": f"服务内部出错：{exc}"}, 500)


async def root(request: web.Request):
    if _session(request).get("user") == Config.ADMIN_USER:
        raise web.HTTPFound("/app/")
    raise web.HTTPFound("/login")


async def login_page(_request: web.Request):
    return web.FileResponse(STATIC / "login.html")


async def spa_page(request: web.Request):
    if _session(request).get("user") != Config.ADMIN_USER:
        raise web.HTTPFound("/login")
    return web.FileResponse(STATIC / "index.html")


async def api_login(request: web.Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_resp({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_resp({"ok": True, "user": user})
    resp.set_cookie(
        "schema_session",
        serializer.dumps({"user": user}),
        httponly=True,
        path="/",
    )
    return resp


async def api_logout(_request: web.Request):
    resp = json_resp({"ok": True})
    resp.del_cookie("schema_session", path="/")
    return resp


async def api_meta(request: web.Request):
    require_user(request)
    return json_resp({"lab": Config.LAB_NAME, "product": "cc-schema-lab"})


def _db(request: web.Request) -> str | None:
    return request.app.get("db_path")


async def api_contracts(request: web.Request):
    require_user(request)
    return json_resp({"items": list_contracts(_db(request))})


async def api_breaking(request: web.Request):
    require_user(request)
    cid = int(request.match_info["cid"])
    return json_resp({"check": run_breaking_check(cid, _db(request))})


async def api_apply(request: web.Request):
    require_user(request)
    cid = int(request.match_info["cid"])
    return json_resp({"contract": apply_contract(cid, _db(request))})


async def api_reports(request: web.Request):
    require_user(request)
    return json_resp({"items": list_reports(_db(request))})


async def api_reconcile(request: web.Request):
    require_user(request)
    body = await _json_body(request)
    as_of = str(body.get("as_of") or "").strip() or "2026-11-18"
    return json_resp({"report": run_reconcile(as_of, _db(request))})


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise ServiceError("请求体不是合法 JSON", 400)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ServiceError("请求体必须是 JSON 对象", 400)
    return body


async def api_gate(request: web.Request):
    require_user(request)
    return json_resp({"gate": gate_policy(_db(request))})


async def api_gate_check(request: web.Request):
    require_user(request)
    body = await _json_body(request)
    table = str(body.get("table_name") or "").strip()
    if not table:
        raise ServiceError("没填表名，核不了闸口", 400)
    rows = body.get("rows")
    if rows is not None and not isinstance(rows, list):
        raise ServiceError("rows 必须是行数组", 400)
    return json_resp({"check": gate_check(table, rows, _db(request))})


async def api_batches(request: web.Request):
    require_user(request)
    items = list_batches(
        _db(request),
        table_name=request.query.get("table_name") or None,
        status=request.query.get("status") or None,
    )
    return json_resp({"items": items})


async def api_batch(request: web.Request):
    require_user(request)
    bid = int(request.match_info["bid"])
    return json_resp({"batch": get_batch(bid, _db(request))})


async def api_ingest(request: web.Request):
    require_user(request)
    body = await _json_body(request)
    table = str(body.get("table_name") or "").strip()
    rows = body.get("rows")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ServiceError("rows 必须是行数组", 400)
    operator = str(body.get("operator") or "值班").strip() or "值班"
    batch = ingest(
        table,
        rows,
        batch_no=body.get("batch_no") or None,
        operator=operator,
        db_path=_db(request),
    )
    if batch["status"] == "待放行":
        return json_resp({"ok": True, "batch": batch, "accepted": True, "message": batch["detail"]}, 202)
    # 拒收不是成功：给 422 + ok=false，不把「缺契」说成静默通过。
    return json_resp(
        {
            "ok": False,
            "accepted": False,
            "batch": batch,
            "error": batch["detail"],
            "message": batch["detail"],
        },
        422,
    )


async def api_release(request: web.Request):
    require_user(request)
    bid = int(request.match_info["bid"])
    body = await _json_body(request)
    operator = str(body.get("operator") or "值班").strip() or "值班"
    batch = release(bid, operator=operator, db_path=_db(request))
    return json_resp({"batch": batch, "message": batch["detail"]})


async def api_rollback(request: web.Request):
    require_user(request)
    bid = int(request.match_info["bid"])
    body = await _json_body(request)
    operator = str(body.get("operator") or "值班").strip() or "值班"
    batch = rollback(bid, operator=operator, db_path=_db(request))
    return json_resp({"batch": batch, "message": batch["detail"]})


async def api_events(request: web.Request):
    require_user(request)
    return json_resp({"items": list_events(_db(request))})
