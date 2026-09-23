"""文化推荐策略治理的运行入口与正式 HTTP 接口。

健康入口保持向后兼容；正式接口均以 /api 开头，JSON 收发。
状态保存在单进程内存中，供联调与试运行（trial_run.py）使用。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from gov.metrics import catalog_snapshot
from gov.platform import Platform

SERVICE_ID = "culture-policy-audit"
SERVICE_NAME = "文化推荐策略治理"

# 启动时钟；试运行脚本可通过 /api/clock 推进
START_CLOCK = "2026-09-01T08:00:00"
PLATFORM = Platform(START_CLOCK)


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查与治理 API，供本地联调、运维巡检与试运行使用。"""

    # ---------- 基础收发 ----------
    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _query(self) -> dict:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    def log_message(self, *_args):
        return

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/health":
                return self._send(200, health_payload())
            if path == "/api/metrics/catalog":
                return self._send(200, catalog_snapshot())
            if path == "/api/audit":
                return self._send(200, PLATFORM.audit())
            if path == "/api/reproduce":
                day = self._query().get("day")
                if not day:
                    return self._send(400, {"error": "需要 day 参数"})
                return self._send(200, PLATFORM.reproduce_day(day))
            if path.startswith("/api/reports/short"):
                q = self._query()
                scope = json.loads(q["scope"]) if q.get("scope") else None
                return self._send(200, PLATFORM.report_short(q["day"], scope))
            if path.startswith("/api/reports/long"):
                q = self._query()
                scope = json.loads(q["scope"]) if q.get("scope") else None
                return self._send(200, PLATFORM.report_long(q["day"], scope))
            if path.startswith("/api/experiments/") and path.endswith("/compare"):
                exp_id = path.split("/")[3]
                day = self._query().get("day")
                return self._send(200, PLATFORM.compare_segments(day, exp_id))
            if path.startswith("/api/channels/explain"):
                content_id = self._query().get("content_id")
                return self._send(200, PLATFORM.channel_explain(content_id))
            if path.startswith("/api/privacy/status"):
                user_ref = self._query().get("user_ref")
                return self._send(200, PLATFORM.privacy_status(user_ref))
            self.send_error(404)
        except KeyError as exc:
            self._send(400, {"error": f"缺少参数: {exc}"})
        except Exception as exc:  # 领域错误统一 400
            self._send(400, {"error": str(exc)})

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/clock":
                frozen = PLATFORM.tick(body["now"])
                return self._send(200, {"now": PLATFORM.clock, "finalized_windows": frozen})
            if path == "/api/user-attributes":
                PLATFORM.set_user_attributes(body["user_ref"], body["attrs"])
                return self._send(200, {"ok": True})
            if path == "/api/policies/submit":
                return self._send(200, PLATFORM.submit_policy(body))
            if path.endswith("/approve"):
                pid = path.split("/")[3]
                return self._send(200, PLATFORM.approve_policy(
                    pid, role=body["role"], approver=body["approver"],
                    reason=body.get("reason", "")))
            if path.endswith("/reject"):
                pid = path.split("/")[3]
                return self._send(200, PLATFORM.reject_policy(
                    pid, role=body["role"], approver=body["approver"],
                    reason=body["reason"]))
            if path == "/api/experiments/open":
                return self._send(200, PLATFORM.open_experiment(**body))
            if path.endswith("/adjust"):
                exp_id = path.split("/")[3]
                return self._send(200, PLATFORM.adjust_weights(
                    exp_id, body["new_policy_id"], reason=body.get("reason", "")))
            if path.endswith("/rollback"):
                exp_id = path.split("/")[3]
                return self._send(200, PLATFORM.rollback(
                    exp_id, reason=body.get("reason", "")))
            if path == "/api/route":
                return self._send(200, PLATFORM.route(
                    body["user_ref"], body.get("ts")))
            if path == "/api/events":
                return self._send(200, PLATFORM.ingest(body))
            if path == "/api/privacy/disable":
                return self._send(200, PLATFORM.disable_profiling(body["user_ref"]))
            if path == "/api/privacy/reset":
                return self._send(200, PLATFORM.reset_interests(body["user_ref"]))
            if path == "/api/channels/enter":
                return self._send(200, PLATFORM.channel_enter(body))
            if path == "/api/channels/exit":
                return self._send(200, PLATFORM.channel_exit(body))
            if path == "/api/reports/kanon":
                return self._send(200, PLATFORM.k_anonymous_report(
                    body["groups"], body.get("k", 5)))
            self.send_error(404)
        except KeyError as exc:
            self._send(400, {"error": f"缺少字段: {exc}"})
        except Exception as exc:  # 领域错误统一 400
            self._send(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert catalog_snapshot()["catalog_version"]
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
