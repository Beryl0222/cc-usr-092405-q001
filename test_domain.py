"""领域规则单元测试：与 service_contract 一起由 npm test 运行。"""

import unittest

from gov import metrics as metric_dir
from gov.channels import ChannelRegistry, kanonymize
from gov.events import EventPipeline
from gov.experiments import BASELINE_STRATEGY, ExperimentHub
from gov.policies import PolicyRegistry, ROLLOUT_CAP
from gov.privacy import PrivacyStore

NOW = "2026-09-01T08:00:00"


def approved_policy(reg, weights=None, rollout=0.10):
    weights = weights or {
        "effective_watch_share": 0.2, "completion_rate": 0.15,
        "favorite_rate": 0.2, "discussion_quality": 0.15,
        "diversity_surface": 0.1, "return_visit_d7": 0.2}
    p = reg.submit(
        title="t", goal="g", weights=weights,
        audience={"include": {"taste": "culture"}},
        effective_from="2026-09-01T00:00:00",
        effective_to="2026-09-30T23:59:59", owner="pm", rollout=rollout, now=NOW)
    reg.send_for_approval(p.id, NOW)
    reg.approve(p.id, role="内容负责人", approver="c", reason="", now=NOW)
    reg.approve(p.id, role="风险负责人", approver="r", reason="", now=NOW)
    return reg.get(p.id)


class MetricCatalogTest(unittest.TestCase):
    def test_weights_must_reference_registered_metrics(self):
        with self.assertRaises(ValueError):
            metric_dir.validate_weights({"play_count": 1.0})

    def test_weights_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            metric_dir.validate_weights({"favorite_rate": 0.5})

    def test_valid_weights(self):
        metric_dir.validate_weights({"favorite_rate": 0.5, "return_visit_d7": 0.5})


class PolicyApprovalTest(unittest.TestCase):
    def setUp(self):
        self.reg = PolicyRegistry()

    def test_four_required_elements(self):
        with self.assertRaises(ValueError):
            self.reg.submit(title="t", goal="", weights={"favorite_rate": 1.0},
                            audience={"include": {}}, effective_from=NOW,
                            effective_to="2026-09-30T00:00:00",
                            owner="pm", rollout=0.1, now=NOW)

    def test_rollout_cap_enforced(self):
        with self.assertRaises(ValueError):
            approved_policy(self.reg, rollout=ROLLOUT_CAP + 0.01)

    def test_single_approval_does_not_activate(self):
        p = self.reg.submit(
            title="t", goal="g",
            weights={"favorite_rate": 0.5, "return_visit_d7": 0.5},
            audience={"include": {}}, effective_from=NOW,
            effective_to="2026-09-30T00:00:00", owner="pm",
            rollout=0.1, now=NOW)
        self.reg.send_for_approval(p.id, NOW)
        self.reg.approve(p.id, role="内容负责人", approver="c", reason="", now=NOW)
        self.assertEqual(self.reg.get(p.id).status, "待批准")

    def test_duplicate_approval_rejected(self):
        p = self.reg.submit(
            title="t", goal="g",
            weights={"favorite_rate": 0.5, "return_visit_d7": 0.5},
            audience={"include": {}}, effective_from=NOW,
            effective_to="2026-09-30T00:00:00", owner="pm",
            rollout=0.1, now=NOW)
        self.reg.send_for_approval(p.id, NOW)
        self.reg.approve(p.id, role="内容负责人", approver="c", reason="", now=NOW)
        with self.assertRaises(ValueError):
            self.reg.approve(p.id, role="内容负责人", approver="c2", reason="", now=NOW)


class ExperimentSegmentTest(unittest.TestCase):
    def _hub_with_exp(self, weights=None):
        reg = PolicyRegistry()
        p = approved_policy(reg, weights)
        hub = ExperimentHub()
        return reg, hub, hub.open(name="e", policy=p, salt="s", start_ts=NOW)

    def test_adjust_opens_new_immutable_segment(self):
        reg, hub, exp = self._hub_with_exp()
        old_w = dict(exp.live_segment.weights)
        p2 = approved_policy(reg, {"effective_watch_share": 0.1,
                                   "completion_rate": 0.1, "favorite_rate": 0.2,
                                   "discussion_quality": 0.1,
                                   "diversity_surface": 0.2,
                                   "return_visit_d7": 0.3})
        seg2 = hub.get(exp.id).adjust_weights(policy=p2, now="2026-09-02T00:00:00",
                                              reason="tune")
        self.assertEqual(seg2.seq, 2)
        frozen = exp.segments[0]
        self.assertIsNotNone(frozen.end_ts)
        self.assertEqual(frozen.weights, old_w)  # 旧分段权重不被就地改写
        self.assertEqual(exp.segment_at("2026-09-01T23:59:59").seq, 1)
        self.assertEqual(exp.segment_at("2026-09-02T00:00:00").seq, 2)

    def test_adjust_with_same_weights_rejected(self):
        reg, hub, exp = self._hub_with_exp()
        with self.assertRaises(ValueError):
            hub.get(exp.id).adjust_weights(policy=reg.get(exp.segments[0].policy_id),
                                           now="2026-09-02T00:00:00", reason="x")

    def test_rollback_closes_segment_and_routes_baseline(self):
        reg, hub, exp = self._hub_with_exp()
        hub.rollback(exp.id, now="2026-09-02T00:00:00", reason="紧急")
        self.assertTrue(hub.get(exp.id).rolled_back)
        self.assertIsNone(hub.get(exp.id).live_segment)
        d = hub.assign(anon_id="a1_x", ts="2026-09-02T01:00:00",
                       audience_match=True, profiling_enabled=True)
        self.assertEqual(d.strategy, BASELINE_STRATEGY)

    def test_bucket_is_deterministic_and_reproducible(self):
        _, hub, exp = self._hub_with_exp()
        seg = exp.live_segment
        b1 = exp.bucket_of("a1_user", seg)
        b2 = exp.bucket_of("a1_user", seg)
        self.assertEqual(b1, b2)
        self.assertTrue(0 <= b1 < 10000)

    def test_profiling_disabled_forces_baseline(self):
        _, hub, exp = self._hub_with_exp()
        d = hub.assign(anon_id="a1_user", ts=NOW, audience_match=True,
                       profiling_enabled=False)
        self.assertEqual(d.strategy, BASELINE_STRATEGY)
        self.assertIn("关闭画像", d.reason)


class EventPipelineTest(unittest.TestCase):
    def test_dedupe_revoke_and_late_quarantine(self):
        pipe = EventPipeline("2026-09-02T08:00:00")
        fav = {"event_id": "f1", "kind": "favorite",
               "occurred_at": "2026-09-02T10:00:00", "anon_id": "a1",
               "content_id": "c1"}
        self.assertEqual(pipe.ingest(fav).classification, "counted")
        self.assertEqual(pipe.ingest(dict(fav)).classification, "duplicate")
        # 撤回乱序先到
        r = pipe.ingest({"event_id": "f2", "revoke": True,
                         "received_at": "2026-09-02T11:00:00"})
        self.assertEqual(r.classification, "pending_revoke")
        r = pipe.ingest({"event_id": "f2", "kind": "favorite",
                         "occurred_at": "2026-09-02T11:05:00",
                         "anon_id": "a1", "content_id": "c1"})
        self.assertEqual(r.classification, "revoked_on_arrival")
        # 定稿后迟到事件隔离
        pipe.finalize_due("2026-09-04T00:00:00")
        late = pipe.ingest({"event_id": "f3", "kind": "favorite",
                            "occurred_at": "2026-09-02T22:00:00",
                            "received_at": "2026-09-04T01:00:00",
                            "anon_id": "a1", "content_id": "c1"})
        self.assertEqual(late.classification, "quarantined_late")

    def test_raw_identity_rejected(self):
        pipe = EventPipeline("2026-09-02T08:00:00")
        r = pipe.ingest({"event_id": "x", "kind": "exposure", "user_id": "p1",
                         "anon_id": "a1", "content_id": "c",
                         "occurred_at": "2026-09-02T09:00:00"})
        self.assertEqual(r.classification, "rejected")

    def test_finalized_window_is_immutable(self):
        pipe = EventPipeline("2026-09-02T08:00:00")
        pipe.ingest({"event_id": "e1", "kind": "exposure",
                     "occurred_at": "2026-09-02T09:00:00", "anon_id": "a1",
                     "content_id": "c1",
                     "data": {"declared_duration": 100, "watch_seconds": 50}})
        before = pipe.compute_short_term("2026-09-02")
        pipe.finalize_due("2026-09-04T00:00:00")
        pipe.ingest({"event_id": "e1", "revoke": True,
                     "received_at": "2026-09-04T01:00:00"})
        self.assertEqual(pipe.compute_short_term("2026-09-02"), before)


class PrivacyTest(unittest.TestCase):
    def test_reset_rotates_pseudonym_and_clears_preferences(self):
        store = PrivacyStore()
        store.record_preference("u1", "非遗", 1.0)
        old = store.anon_id("u1")
        status = store.reset_interests("u1", NOW)
        self.assertNotEqual(old, status["anon_id"])
        self.assertEqual(store.effective_preferences("u1"), {})
        epoch = int(old[1:].split("_")[0])
        with self.assertRaises(ValueError):
            store.pseudo.anonymize("u1", epoch)  # 旧盐已销毁

    def test_disable_blocks_preference_writes_and_forces_empty(self):
        store = PrivacyStore()
        store.record_preference("u1", "知识", 1.0)
        store.disable_profiling("u1", NOW)
        self.assertEqual(store.effective_preferences("u1"), {})
        with self.assertRaises(ValueError):
            store.record_preference("u1", "知识", 1.0)


class ChannelTest(unittest.TestCase):
    def test_enter_exit_explanation(self):
        reg = ChannelRegistry()
        reg.enter(content_id="c1", creator_id="cr", reason="达标",
                  signals={"x": 1}, basis="PL1", now=NOW)
        reg.exit(content_id="c1", reason="回滚", signals={}, basis="EXP1", now=NOW)
        explanation = reg.explain("c1")
        self.assertFalse(explanation["currently_in_channel"])
        self.assertEqual([h["in_channel"] for h in explanation["history"]],
                         [True, False])

    def test_kanonymity_suppresses_small_groups(self):
        out = kanonymize({"大品类": {"users": 10, "metrics": {}},
                          "小品类": {"users": 1, "metrics": {}}}, k=5)
        self.assertIn("小品类", out["suppressed_groups"])
        self.assertNotIn("小品类", out["released"])


if __name__ == "__main__":
    unittest.main()
