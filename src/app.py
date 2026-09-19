from __future__ import annotations

import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ledger import DomainError, Ledger, NotFoundError, Store

SERVICE_NAME = '运动康复费用理赔账本'

ROUTES: list[tuple[str, re.Pattern, object]] = []


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def route(method: str, pattern: str):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def decorator(fn):
        ROUTES.append((method, regex, fn))
        return fn

    return decorator


class Api:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def dispatch(self, method: str, path: str, query: dict, body):
        for route_method, regex, handler in ROUTES:
            if route_method != method:
                continue
            match = regex.match(path)
            if match:
                return handler(self, match.groupdict(), query, body if body is not None else {})
        raise NotFoundError(f"接口不存在: {method} {path}", code="route_not_found")


# ---------- 基础 ----------


@route("GET", "/health")
def _health(api, params, query, body):
    return 200, health_payload()


@route("GET", "/events")
def _events(api, params, query, body):
    events = api.ledger.list_events(
        type=query.get("type"),
        entity_kind=query.get("entity_kind"),
        entity_id=query.get("entity_id"),
    )
    return 200, {"events": [e.to_dict() for e in events]}


# ---------- 球员 / 疗程 / 处方 ----------


@route("POST", "/players")
def _create_player(api, params, query, body):
    player = api.ledger.add_player(
        name=body.get("name"), team=body.get("team", ""), player_id=body.get("player_id")
    )
    return 201, {"player": player.to_dict()}


@route("GET", "/players/{player_id}")
def _get_player(api, params, query, body):
    return 200, {"player": api.ledger.get_player(params["player_id"]).to_dict()}


@route("POST", "/courses")
def _create_course(api, params, query, body):
    course = api.ledger.add_course(
        player_id=body.get("player_id"),
        diagnosis=body.get("diagnosis"),
        started_at=body.get("started_at"),
        course_id=body.get("course_id"),
    )
    return 201, {"course": course.to_dict()}


@route("POST", "/prescriptions")
def _create_prescription(api, params, query, body):
    rx = api.ledger.add_prescription(
        course_id=body.get("course_id"),
        items=body.get("items"),
        effective_from=body.get("effective_from"),
        decided_at=body.get("decided_at"),
        reason=body.get("reason", "初始处方"),
        prescription_id=body.get("prescription_id"),
        actor=body.get("actor", "team_doctor"),
    )
    return 201, {"prescription": rx.to_dict()}


@route("POST", "/prescriptions/{prescription_id}/revisions")
def _revise_prescription(api, params, query, body):
    rx = api.ledger.revise_prescription(
        params["prescription_id"],
        items=body.get("items"),
        reason=body.get("reason"),
        effective_from=body.get("effective_from"),
        decided_at=body.get("decided_at"),
        actor=body.get("actor", "team_doctor"),
    )
    return 200, {"prescription": rx.to_dict()}


# ---------- 保单 / 预授权 ----------


@route("POST", "/policies")
def _create_policy(api, params, query, body):
    policy = api.ledger.add_policy(
        payer_type=body.get("payer_type"),
        payer_name=body.get("payer_name"),
        policy_year=body.get("policy_year"),
        effective_from=body.get("effective_from"),
        effective_to=body.get("effective_to"),
        deductible=body.get("deductible", "0"),
        annual_limit=body.get("annual_limit", "0"),
        currency=body.get("currency", "CNY"),
        priority=body.get("priority"),
        rules=body.get("rules"),
        policy_id=body.get("policy_id"),
    )
    return 201, {"policy": policy.to_dict()}


@route("GET", "/policies")
def _list_policies(api, params, query, body):
    return 200, {"policies": [p.to_dict() for p in api.ledger.list_policies()]}


@route("POST", "/pre-authorizations")
def _create_preauth(api, params, query, body):
    preauth = api.ledger.add_preauthorization(
        policy_id=body.get("policy_id"),
        course_id=body.get("course_id"),
        max_amount=body.get("max_amount"),
        valid_from=body.get("valid_from"),
        valid_until=body.get("valid_until"),
        service_codes=body.get("service_codes"),
        preauth_id=body.get("preauth_id"),
        actor=body.get("actor", "insurer"),
    )
    return 201, {"pre_authorization": preauth.to_dict()}


# ---------- 收据 ----------


@route("POST", "/receipts")
def _create_receipt(api, params, query, body):
    result = api.ledger.add_receipt(
        player_id=body.get("player_id"),
        course_id=body.get("course_id"),
        vendor=body.get("vendor"),
        receipt_no=body.get("receipt_no"),
        service_date=body.get("service_date"),
        lines=body.get("lines"),
        currency=body.get("currency", "CNY"),
        documents=body.get("documents"),
        image=body.get("image"),
        received_at=body.get("received_at"),
        receipt_id=body.get("receipt_id"),
        uploaded_by=body.get("uploaded_by", "vendor"),
    )
    status = 200 if result["duplicate"] else 201
    return status, {
        "receipt": result["receipt"].to_dict(),
        "duplicate": result["duplicate"],
        "merged": result["merged"],
    }


@route("GET", "/receipts/{receipt_id}")
def _get_receipt(api, params, query, body):
    return 200, {"receipt": api.ledger.get_receipt(params["receipt_id"]).to_dict()}


@route("POST", "/receipts/{receipt_id}/images")
def _add_receipt_image(api, params, query, body):
    receipt = api.ledger.add_receipt_image(
        params["receipt_id"],
        image_ref=body.get("image_ref"),
        uploaded_by=body.get("uploaded_by", "vendor"),
        kind=body.get("kind"),
        note=body.get("note", ""),
        uploaded_at=body.get("uploaded_at"),
    )
    return 201, {"receipt": receipt.to_dict()}


# ---------- 理赔 ----------


@route("POST", "/claims")
def _submit_claim(api, params, query, body):
    result = api.ledger.submit_claim(
        body.get("receipt_id"),
        submitted_by=body.get("submitted_by", "frontdesk"),
        submitted_at=body.get("submitted_at"),
    )
    claim = result["claim"].to_dict()
    status = 200 if result["duplicate"] else 201
    return status, {
        "claim": claim,
        "duplicate": result["duplicate"],
        "status": claim["status"],
    }


@route("GET", "/claims")
def _list_claims(api, params, query, body):
    claims = api.ledger.list_claims(
        player_id=query.get("player_id"),
        course_id=query.get("course_id"),
        status=query.get("status"),
    )
    return 200, {"claims": [c.to_dict() for c in claims]}


@route("GET", "/claims/{claim_id}")
def _get_claim(api, params, query, body):
    return 200, {"claim": api.ledger.get_claim(params["claim_id"]).to_dict()}


@route("POST", "/claims/{claim_id}/supplements")
def _submit_supplement(api, params, query, body):
    claim = api.ledger.submit_supplement(
        params["claim_id"],
        documents=body.get("documents"),
        image=body.get("image"),
        uploaded_by=body.get("uploaded_by", "provider"),
        at=body.get("at"),
    )
    return 200, {"claim": claim.to_dict()}


# ---------- 队医视图 / 会计视图 ----------


@route("GET", "/players/{player_id}/courses/{course_id}/summary")
def _course_summary(api, params, query, body):
    return 200, api.ledger.course_summary(params["player_id"], params["course_id"])


@route("POST", "/payment-batches")
def _create_batch(api, params, query, body):
    batch = api.ledger.create_payment_batch(
        body.get("policy_id"),
        claim_ids=body.get("claim_ids"),
        created_by=body.get("created_by", "accounting"),
    )
    return 201, {"batch": batch.to_dict()}


@route("GET", "/payment-batches/{batch_id}")
def _get_batch(api, params, query, body):
    return 200, {"batch": api.ledger.get_batch(params["batch_id"]).to_dict()}


@route("GET", "/payment-batches/{batch_id}/trace")
def _batch_trace(api, params, query, body):
    return 200, api.ledger.batch_trace(params["batch_id"])


# ---------- 跟进 ----------


@route("GET", "/follow-ups")
def _follow_ups(api, params, query, body):
    return 200, {"follow_ups": api.ledger.list_follow_ups()}


@route("POST", "/maintenance/sweep")
def _sweep(api, params, query, body):
    flagged = api.ledger.sweep(as_of=body.get("as_of"))
    return 200, {"flagged": flagged}


# ---------- HTTP 服务 ----------


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._respond(
                    400,
                    {
                        "error": {
                            "code": "invalid_json",
                            "message": "请求体不是有效 JSON",
                            "details": {},
                        }
                    },
                )
                return
        try:
            status, payload = self.server.api.dispatch(self.command, parsed.path, query, body)
        except DomainError as exc:
            status, payload = exc.status, {"error": exc.to_dict()}
        except Exception:  # noqa: BLE001 - 兜底，避免连接被直接掐断
            traceback.print_exc()
            status, payload = 500, {
                "error": {"code": "internal_error", "message": "服务器内部错误", "details": {}}
            }
        self._respond(status, payload)

    def _respond(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def load_catalog() -> dict:
    path = os.getenv("DOMAIN_PATH")
    if not path:
        path = str(Path(__file__).resolve().parent.parent / "reference" / "domain.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def build_ledger() -> Ledger:
    store = Store(os.getenv("LEDGER_PATH", ".runtime/ledger.json"))
    window = int(os.getenv("SUPPLEMENT_WINDOW_DAYS", "30"))
    return Ledger(store, catalog=load_catalog(), supplement_window_days=window)


def create_server(host: str, port: int, api: Api | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.api = api or Api(build_ledger())
    return server
