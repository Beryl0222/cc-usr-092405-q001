"""领域规则单元测试：与 service_contract 一起由 npm test 运行。"""

import os
import tempfile
import unittest

from gov import metrics as metric_dir
from gov.channels import ChannelRegistry, kanonymize
from gov.events import EventPipeline
from gov.experiments import BASELINE_STRATEGY, ExperimentHub
from gov.platform import Platform
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


# 同编号重投的基准载荷：含完整实验归属与业务数据
CONFLICT_BASE = {
    "event_id": "cf1", "kind": "exposure",
    "occurred_at": "2026-09-02T09:00:00", "anon_id": "a1", "content_id": "ct1",
    "data": {"declared_duration": 100, "watch_seconds": 50, "category": "知识讲解"},
    "decision_seq": 7, "experiment_id": "EXP0001", "segment_seq": 1,
    "strategy": "PL0001", "variant": "experiment",
}


class EventConflictTest(unittest.TestCase):
    """同 event_id 重投：载荷相同才算重试；任一关键字段不同即为冲突。"""

    def setUp(self):
        self.pipe = EventPipeline("2026-09-02T08:00:00")
        self.assertEqual(self.pipe.ingest(dict(CONFLICT_BASE)).classification,
                         "counted")

    def test_identical_payload_is_idempotent_even_with_later_received_at(self):
        # received_at 是到达时间，重投天然更晚，不参与一致性比较
        retry = dict(CONFLICT_BASE, received_at="2026-09-02T12:00:00")
        r = self.pipe.ingest(retry)
        self.assertEqual(r.classification, "duplicate")
        self.assertTrue(r.accepted)
        win = self.pipe.audit()["windows"]["2026-09-02"]
        self.assertEqual(win["duplicate_deliveries"], 1)
        self.assertEqual(win["conflict_deliveries"], 0)

    def test_each_material_field_conflict_is_rejected_with_evidence(self):
        variants = {
            "kind": {"kind": "favorite"},
            "anon_id": {"anon_id": "a2"},
            "content_id": {"content_id": "ct2"},
            "occurred_at": {"occurred_at": "2026-09-02T09:01:00"},
            "data": {"data": {"declared_duration": 100, "watch_seconds": 99}},
            "experiment_id": {"experiment_id": "EXP0002"},
            "segment_seq": {"segment_seq": 2},
            "strategy": {"strategy": "PL0002"},
            "variant": {"variant": "control"},
            "decision_seq": {"decision_seq": 8},
        }
        for field_name, patch in variants.items():
            pipe = EventPipeline("2026-09-02T08:00:00")
            pipe.ingest(dict(CONFLICT_BASE))
            r = pipe.ingest({**CONFLICT_BASE, **patch})
            self.assertEqual(r.classification, "conflict", field_name)
            self.assertFalse(r.accepted, field_name)
            self.assertEqual(r.conflict["differing_fields"], [field_name])
            # 证据中两份内容都要留档，可查询
            self.assertEqual(r.conflict["stored"][field_name],
                             CONFLICT_BASE[field_name])
            self.assertEqual(r.conflict["incoming"][field_name],
                             {**CONFLICT_BASE, **patch}[field_name])
            self.assertEqual(r.conflict["day"], "2026-09-02")
            self.assertEqual(len(pipe.conflict_ledger), 1)

    def test_conflict_never_overwrites_metrics(self):
        before = self.pipe.compute_short_term("2026-09-02")
        # 若被覆盖，完播率会从 0.5 变成 0.99
        r = self.pipe.ingest({**CONFLICT_BASE,
                              "data": {"declared_duration": 100, "watch_seconds": 99}})
        self.assertEqual(r.classification, "conflict")
        after = self.pipe.compute_short_term("2026-09-02")
        self.assertEqual(after, before)
        self.assertEqual(after["metrics"]["completion_rate"], 0.5)
        # 冲突之后真正相同的重试仍然只计一次
        self.assertEqual(self.pipe.ingest(dict(CONFLICT_BASE)).classification,
                         "duplicate")
        self.assertEqual(self.pipe.compute_short_term("2026-09-02"), before)

    def test_conflict_after_finalization_is_still_detected_and_flagged(self):
        # 撤回先到不能破坏判断；定稿后冲突重投也要被识别（而非伪装成迟到隔离）
        self.pipe.finalize_due("2026-09-04T00:00:00")
        before = self.pipe.compute_short_term("2026-09-02")
        r = self.pipe.ingest({
            **CONFLICT_BASE, "received_at": "2026-09-05T09:00:00",
            "data": {"declared_duration": 100, "watch_seconds": 99}})
        self.assertEqual(r.classification, "conflict")
        self.assertTrue(r.conflict["window_finalized"],
                        "证据必须标明冲突命中的窗口已定稿，需要人工复核")
        self.assertEqual(self.pipe.compute_short_term("2026-09-02"), before)
        # 同编号真正相同的重投即使在定稿后也仍是幂等命中
        ok = self.pipe.ingest({**CONFLICT_BASE,
                               "received_at": "2026-09-05T09:05:00"})
        self.assertEqual(ok.classification, "duplicate")

    def test_revoke_then_redelivery_keeps_single_truth(self):
        # 原事件已撤回：相同重投仍为幂等且保持作废；不同内容仍是冲突
        self.assertEqual(self.pipe.ingest(
            {"event_id": "cf1", "revoke": True,
             "received_at": "2026-09-02T10:00:00"}).classification, "counted")
        revoked = lambda: self.pipe.audit()["windows"]["2026-09-02"]["revoked"]
        self.assertEqual(revoked(), 1)
        self.assertEqual(self.pipe.ingest(dict(CONFLICT_BASE)).classification,
                         "duplicate")
        self.assertEqual(revoked(), 1, "幂等重投不得把已撤回事件复活")
        r = self.pipe.ingest({**CONFLICT_BASE, "anon_id": "a9"})
        self.assertEqual(r.classification, "conflict")

    def test_conflict_report_is_queryable_by_event_and_day(self):
        other = {**CONFLICT_BASE, "event_id": "cf_other"}
        self.pipe.ingest(dict(other))
        self.pipe.ingest({**CONFLICT_BASE, "anon_id": "a2"})
        self.pipe.ingest({**other, "anon_id": "a3"})
        by_event = self.pipe.conflict_report(event_id="cf1")
        self.assertEqual(by_event["conflict_count"], 1)
        by_day = self.pipe.conflict_report(day="2026-09-02")
        self.assertEqual(by_day["conflict_count"], 2)
        self.assertEqual(self.pipe.conflict_report(day="2026-09-03"),
                         {"conflict_count": 0, "conflicts": []})


class PipelineRestartTest(unittest.TestCase):
    """重启恢复：首条记录、冲突台账、定稿状态与挂起撤回跨重启保持判断。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "events.log")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pipe(self):
        return EventPipeline("2026-09-02T08:00:00", store_path=self.path)

    def test_idempotency_and_conflict_judgments_survive_restart(self):
        pipe = self._pipe()
        pipe.ingest(dict(CONFLICT_BASE))
        pipe.ingest(dict(CONFLICT_BASE))  # 幂等一次
        pipe.ingest({**CONFLICT_BASE, "data": {"declared_duration": 100,
                                               "watch_seconds": 99}})
        # 挂起的撤回也必须随日志恢复
        pipe.ingest({"event_id": "rr1", "revoke": True,
                     "received_at": "2026-09-02T11:00:00"})
        before = pipe.compute_short_term("2026-09-02")
        pipe.close()

        restored = self._pipe()
        self.assertEqual(restored.compute_short_term("2026-09-02"), before)
        self.assertEqual(len(restored.conflict_ledger), 1)
        self.assertIn("rr1", restored.audit()["pending_revokes"])
        # 挂起撤回恢复后，原事件到达即作废
        r = restored.ingest({"event_id": "rr1", "kind": "favorite",
                             "occurred_at": "2026-09-02T11:05:00",
                             "anon_id": "a1", "content_id": "ct1"})
        self.assertEqual(r.classification, "revoked_on_arrival")
        # 幂等判断延续：相同重投计数继续累加
        self.assertEqual(restored.ingest(dict(CONFLICT_BASE)).classification,
                         "duplicate")
        # 冲突判断延续：不同内容再次被拒，台账继续追加
        r = restored.ingest({**CONFLICT_BASE, "anon_id": "a2"})
        self.assertEqual(r.classification, "conflict")
        self.assertEqual(len(restored.conflict_ledger), 2)
        restored.close()

    def test_finalized_window_and_late_quarantine_survive_restart(self):
        pipe = self._pipe()
        pipe.ingest(dict(CONFLICT_BASE))
        pipe.finalize_due("2026-09-04T00:00:00")
        before = pipe.compute_short_term("2026-09-02")
        pipe.close()

        restored = self._pipe()  # 恢复时不重放定稿之外的时钟动作
        self.assertTrue(
            restored.audit()["windows"]["2026-09-02"]["finalized"])
        late = restored.ingest({
            "event_id": "late1", "kind": "favorite",
            "occurred_at": "2026-09-02T22:00:00",
            "received_at": "2026-09-04T01:00:00",
            "anon_id": "a1", "content_id": "ct1"})
        self.assertEqual(late.classification, "quarantined_late")
        # 定稿后的冲突重投同样被识别，且指标保持冻结
        conflict = restored.ingest({**CONFLICT_BASE, "anon_id": "a2"})
        self.assertEqual(conflict.classification, "conflict")
        self.assertEqual(restored.compute_short_term("2026-09-02"), before)
        restored.close()


class PlatformRestartTest(unittest.TestCase):
    """平台级重启：分流决策盖戳能力恢复，指标不被冲突重投改写。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "events.log")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_decision_stamping_and_conflict_judgment_survive_restart(self):
        pf = Platform("2026-09-01T08:00:00", event_store_path=self.path)
        d = pf.route("u_rt", "2026-09-02T09:00:00")
        event = {"event_id": "pe1", "kind": "exposure",
                 "occurred_at": "2026-09-02T09:00:00",
                 "decision_seq": d["seq"], "content_id": "ct1",
                 "data": {"declared_duration": 100, "watch_seconds": 50}}
        self.assertEqual(pf.ingest(event)["classification"], "counted")
        self.assertEqual(pf.ingest({**event, "data": {
            "declared_duration": 100, "watch_seconds": 99}})["classification"],
            "conflict")
        before = pf.report_short("2026-09-02")
        pf.close()

        pf2 = Platform("2026-09-01T08:00:00", event_store_path=self.path)
        # 决策日志恢复：仅给 decision_seq 仍能盖戳，同载荷识别为重试
        self.assertEqual(pf2.ingest(event)["classification"], "duplicate")
        self.assertEqual(pf2.ingest({**event, "data": {
            "declared_duration": 100, "watch_seconds": 99}})["classification"],
            "conflict")
        self.assertEqual(pf2.report_short("2026-09-02"), before)
        # 决策序号游标恢复，重启后不产生重复 seq
        d2 = pf2.route("u_rt", "2026-09-02T10:00:00")
        self.assertEqual(d2["seq"], d["seq"] + 1)
        pf2.close()


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
