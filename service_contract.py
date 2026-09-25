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

    def call_raw(self, method, path, payload=None):
        """与 call 相同，但不抛异常地返回非 2xx 的状态码与 JSON 体。"""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(f"{self.base_url}{path}", data=data, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

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

    def test_idempotent_hit_vs_payload_conflict_status_distinction(self):
        base = {"event_id": "cf1", "kind": "exposure",
                "occurred_at": "2026-09-03T09:00:00", "anon_id": "cf-a1",
                "content_id": "cf-c1",
                "data": {"declared_duration": 100, "watch_seconds": 50}}
        code, first = self._post("/api/events", base)
        self.assertEqual(code, 200)
        self.assertIn(first["classification"], {"counted", "duplicate"})
        # 真正相同的重试：200 + duplicate，调用方据此安全忽略
        code, dup = self._post("/api/events", dict(base))
        self.assertEqual(code, 200)
        self.assertEqual(dup["classification"], "duplicate")
        # 同编号、业务数据不同（缓存损坏重发）：409 载荷冲突
        corrupted = dict(base)
        corrupted["data"] = {"declared_duration": 100, "watch_seconds": 99}
        code, conflict = self.call_raw("POST", "/api/events", corrupted)
        self.assertEqual(code, 409)
        self.assertEqual(conflict["classification"], "conflict")
        self.assertFalse(conflict["accepted"])
        self.assertEqual(conflict["conflicts"], ["data"])
        self.assertTrue(conflict["conflict_id"])

    def test_conflict_evidence_is_queryable_by_event_id(self):
        base = {"event_id": "cf2", "kind": "favorite",
                "occurred_at": "2026-09-03T11:00:00", "anon_id": "cf-a2",
                "content_id": "cf-c2"}
        self._post("/api/events", base)
        clash = dict(base, anon_id="cf-a9")  # 匿名主体不同
        code, body = self.call_raw("POST", "/api/events", clash)
        self.assertEqual(code, 409)
        self.assertEqual(body["conflicts"], ["anon_id"])
        _, listing = self._get("/api/events/conflicts?event_id=cf2")
        self.assertGreaterEqual(listing["count"], 1)
        record = listing["conflicts"][0]
        self.assertEqual(record["event_id"], "cf2")
        self.assertEqual(record["first"]["anon_id"], "cf-a2")
        self.assertEqual(record["challenger"]["anon_id"], "cf-a9")
        self.assertIn("匿名主体", record["difference_labels"])

    def test_conflict_redelivery_does_not_rewrite_metric(self):
        # 独占日期，避免共享进程内其它事件干扰聚合断言
        day = "2026-09-06"
        eid = "cf3"
        payload = {"event_id": eid, "kind": "exposure",
                   "occurred_at": f"{day}T12:00:00", "anon_id": "cf-a3",
                   "content_id": "cf-c3",
                   "data": {"declared_duration": 100, "watch_seconds": 20}}
        self._post("/api/events", payload)
        # 异载想把完播率从 0.2 改成 1.0：必须 409 拒绝
        code, _ = self.call_raw("POST", "/api/events", {
            **payload, "data": {"declared_duration": 100, "watch_seconds": 100}})
        self.assertEqual(code, 409)
        _, report = self._get(f"/api/reports/short?day={day}")
        # 首条 watch_seconds=20 落库，冲突载荷未改写指标
        self.assertEqual(report["metrics"]["completion_rate"], 0.2)
        self.assertEqual(report["denominator"]["exposures"], 1)

    def test_restart_restore_preserves_idempotency_and_conflict(self):
        eid = "cf4"
        payload = {"event_id": eid, "kind": "exposure",
                   "occurred_at": "2026-09-03T13:00:00", "anon_id": "cf-a4",
                   "content_id": "cf-c4",
                   "data": {"declared_duration": 100, "watch_seconds": 30}}
        self._post("/api/events", payload)
        self.call_raw("POST", "/api/events", {
            **payload, "content_id": "cf-c9"})  # 409 冲突
        # 模拟重启：导出全量状态并恢复
        _, state = self._get("/api/state/dump")
        self.assertIn("events", state)
        code, restored = self._post("/api/state/restore", state)
        self.assertEqual(code, 200)
        self.assertTrue(restored["ok"])
        # 恢复后同载重试仍是 200 幂等命中（没有双计、没有误报冲突）
        code, dup = self._post("/api/events", dict(payload))
        self.assertEqual(code, 200)
        self.assertEqual(dup["classification"], "duplicate")
        # 恢复后异载仍 409，且历史冲突证据可查
        code, again = self.call_raw("POST", "/api/events", {
            **payload, "anon_id": "cf-aX"})
        self.assertEqual(code, 409)
        self.assertEqual(again["classification"], "conflict")
        _, listing = self._get(f"/api/events/conflicts?event_id={eid}")
        self.assertGreaterEqual(listing["count"], 2)


if __name__ == "__main__":
    unittest.main()
