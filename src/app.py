from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from domain import ConflictError, NotFoundError, ValidationError
from engine import LedgerEngine
from store import JsonStore

SERVICE_NAME = '运动康复费用理赔账本'
DEFAULT_STORE_PATH = ".runtime/ledger.json"


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


class Api:
    """理赔账本 HTTP 接口：所有响应均为 JSON，金额字段一律以分(_cents)表示。"""

    def __init__(self, engine: LedgerEngine) -> None:
        self.engine = engine

    def handle(self, method: str, raw_path: str, body: dict) -> tuple[int, object]:
        parsed = urlparse(raw_path)
        segments = [s for s in parsed.path.split("/") if s]
        query = parse_qs(parsed.query)

        if method == "GET" and segments == ["health"]:
            return 200, health_payload()

        # 基础档案
        if method == "POST" and segments == ["players"]:
            return 201, self.engine.create_player(body)
        if method == "GET" and len(segments) == 2 and segments[0] == "players":
            return 200, self.engine.get_player(segments[1])
        if method == "POST" and segments == ["courses"]:
            return 201, self.engine.create_course(body)
        if method == "POST" and segments == ["policies"]:
            return 201, self.engine.create_policy(body)

        # 处方与治疗方案变更
        if method == "POST" and segments == ["prescriptions"]:
            return 201, self.engine.create_prescription(body)
        if method == "POST" and len(segments) == 3 and segments[0] == "prescriptions" \
                and segments[2] == "revisions":
            return 201, self.engine.revise_prescription(segments[1], body)

        # 预授权
        if method == "POST" and segments == ["pre-authorizations"]:
            return 201, self.engine.create_pre_authorization(body)

        # 收据：登记与补传版本
        if method == "POST" and segments == ["receipts"]:
            return 201, self.engine.register_receipt(body)
        if method == "GET" and len(segments) == 2 and segments[0] == "receipts":
            return 200, self.engine.get_receipt(segments[1])
        if method == "POST" and len(segments) == 3 and segments[0] == "receipts" \
                and segments[2] == "versions":
            return 201, self.engine.add_receipt_version(segments[1], body)

        # 理赔申请与补件
        if method == "POST" and segments == ["claims"]:
            return 201, self.engine.submit_claim(body)
        if method == "GET" and len(segments) == 2 and segments[0] == "claims":
            return 200, self.engine.get_claim(segments[1])
        if method == "POST" and len(segments) == 3 and segments[0] == "claims" \
                and segments[2] == "supplements":
            return 200, self.engine.supplement_claim(segments[1], body)

        # 队医视角：按球员 + 疗程的免赔额与额度
        if method == "GET" and len(segments) == 5 and segments[0] == "players" \
                and segments[2] == "courses" and segments[4] == "coverage":
            return 200, self.engine.coverage_view(segments[1], segments[3])

        # 付款批次与责任反查
        if method == "POST" and segments == ["payment-batches"]:
            return 201, self.engine.create_payment_batch(body)
        if method == "GET" and len(segments) == 2 and segments[0] == "payment-batches":
            return 200, self.engine.get_payment_batch(segments[1])
        if method == "GET" and len(segments) == 3 and segments[0] == "payment-batches" \
                and segments[2] == "trace":
            return 200, self.engine.trace_payment_batch(segments[1])

        # 补件跟进（读取时自动标记到期案件）
        if method == "GET" and segments == ["follow-ups"]:
            return 200, {"flagged": self.engine.scan_follow_ups()}

        # 审计事件
        if method == "GET" and segments == ["events"]:
            entity_type = query.get("entity_type", [None])[0]
            entity_id = query.get("entity_id", [None])[0]
            return 200, {"events": self.engine.list_events(entity_type, entity_id)}

        raise NotFoundError(f"路由不存在：{method} {parsed.path}")


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("请求体必须是合法的 JSON")
        if not isinstance(body, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return body

    def _dispatch(self, method: str) -> None:
        try:
            body = self._read_body() if method == "POST" else {}
            status, payload = self.server.api.handle(method, self.path, body)
        except ValidationError as exc:
            status, payload = 400, {"error": "validation_error", "message": exc.message,
                                    "field": exc.field}
        except NotFoundError as exc:
            status, payload = 404, {"error": "not_found", "message": exc.message}
        except ConflictError as exc:
            status, payload = 409, exc.payload
        except Exception as exc:  # pragma: no cover - 兜底
            status, payload = 500, {"error": "internal_error", "message": str(exc)}
        self._send_json(status, payload)

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, store_path: str | None = DEFAULT_STORE_PATH,
                  now_fn=None) -> ThreadingHTTPServer:
    engine = LedgerEngine(JsonStore(store_path), now_fn=now_fn)
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.api = Api(engine)
    return server
