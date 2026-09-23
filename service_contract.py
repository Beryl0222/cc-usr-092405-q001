"""验证基础服务在领域模块开发前保持可运行。"""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, SERVICE_NAME, health_payload


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(health_payload(), {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME})

    def test_health_endpoint_returns_json(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertEqual(json.load(response), health_payload())

    def test_unknown_route_is_not_exposed(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


class ApiContractTest(unittest.TestCase):
    """正式治理接口的端到端契约：审批 -> 实验 -> 事件 -> 指标 -> 回滚。"""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(f"{self.base_url}{path}", data=data, method=method,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)

    def _post(self, path, payload):
        return self.call("POST", path, payload)

    def _get(self, path):
        return self.call("GET", path)

    def test_full_governance_flow_over_http(self):
        weights = {"effective_watch_share": 0.2, "completion_rate": 0.15,
                   "favorite_rate": 0.2, "discussion_quality": 0.15,
                   "diversity_surface": 0.1, "return_visit_d7": 0.2}
        _, policy = self._post("/api/policies/submit", {
            "title": "HTTP 流程策略", "goal": "多信号纠偏", "weights": weights,
            "audience": {"include": {"taste": "culture"}},
            "effective_from": "2026-09-01T00:00:00",
            "effective_to": "2026-09-30T23:59:59",
            "owner": "pm-http", "rollout": 0.1})
        self.assertEqual(policy["status"], "待批准")
        self._post(f"/api/policies/{policy['id']}/approve",
                   {"role": "内容负责人", "approver": "c"})
        _, policy = self._post(f"/api/policies/{policy['id']}/approve",
                               {"role": "风险负责人", "approver": "r"})
        self.assertEqual(policy["status"], "已批准")

        _, exp = self._post("/api/experiments/open",
                            {"name": "http-exp", "policy_id": policy["id"],
                             "salt": "http-salt"})
        exp_id = exp["id"]

        self._post("/api/user-attributes",
                   {"user_ref": "hu1", "attrs": {"taste": "culture"}})
        _, decision = self._post("/api/route",
                                 {"user_ref": "hu1", "ts": "2026-09-02T09:00:00"})
        self.assertIn("strategy", decision)

        _, event = self._post("/api/events", {
            "event_id": "he1", "kind": "exposure",
            "occurred_at": "2026-09-02T09:00:00", "decision_seq": decision["seq"],
            "content_id": "hc1",
            "data": {"declared_duration": 100, "watch_seconds": 80,
                     "category": "知识讲解"}})
        self.assertIn(event["classification"], {"counted", "duplicate"})

        status, repro = self._get(f"/api/reproduce?day=2026-09-02")
        self.assertEqual(status, 200)
        self.assertTrue(repro["reproducible"])

        _, rolled = self._post(f"/api/experiments/{exp_id}/rollback",
                               {"reason": "http 紧急回滚"})
        self.assertTrue(rolled["rolled_back"])

    def test_metric_catalog_is_served(self):
        status, catalog = self._get("/api/metrics/catalog")
        self.assertEqual(status, 200)
        self.assertTrue(catalog["metrics"])
        self.assertEqual(catalog["catalog_version"], "v2.0")

    def test_raw_identity_event_rejected_over_http(self):
        _, result = self._post("/api/events", {
            "event_id": "leak1", "kind": "exposure", "user_id": "real-user",
            "anon_id": "a1", "content_id": "c",
            "occurred_at": "2026-09-02T10:00:00"})
        self.assertEqual(result["classification"], "rejected")


if __name__ == "__main__":
    unittest.main()
