"""领域规则单元测试：与 service_contract 一起由 npm test 运行。"""

import threading
import unittest

from gov import metrics as metric_dir
from gov.channels import ChannelRegistry, kanonymize
from gov.events import EventPipeline
from gov.experiments import BASELINE_STRATEGY, ExperimentHub
from gov.policies import PolicyRegistry, ROLLOUT_CAP
from gov.privacy import PrivacyStore
from gov.platform import Platform

NOW = "2026-09-01T08:00:00"
DAY = "2026-09-02"


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


def _event(eid, **over):
    """构造一条默认曝光事件，over 覆盖任意语义字段。"""
    base = {"event_id": eid, "kind": "exposure",
            "occurred_at": f"{DAY}T09:00:00", "anon_id": "a1",
            "content_id": "c1",
            "data": {"declared_duration": 100, "watch_seconds": 50,
                     "category": "知识讲解"}}
    base.update(over)
    return base


class EventConflictTest(unittest.TestCase):
    """同编号异载：真重试只计一次，载荷差异拒绝覆盖并留证；乱序/撤回/定稿/重启不破坏。"""

    def test_identical_redelivery_counts_once(self):
        pipe = EventPipeline(f"{DAY}T08:00:00")
        self.assertEqual(pipe.ingest(_event("x")).classification, "counted")
        self.assertEqual(pipe.ingest(_event("x")).classification, "duplicate")
        # received_at 不同但语义载荷相同：仍是合法重试（投递时间不参与指纹）
        late_ping = _event("x", received_at=f"{DAY}T10:00:00")
        self.assertEqual(pipe.ingest(late_ping).classification, "duplicate")
        self.assertEqual(
            pipe.audit()["windows"][DAY]["duplicate_deliveries"], 2)

    def test_each_semantic_dimension_is_a_conflict(self):
        cases = [
            ("kind", "favorite"),
            ("anon_id", "a2"),
            ("content_id", "c2"),
            ("occurred_at", f"{DAY}T09:01:00"),
            ("experiment_id", "EXP0001"),
            ("segment_seq", 2),
            ("strategy", "PL0001"),
            ("variant", "experiment"),
            ("decision_seq", 7),
            ("data", {"declared_duration": 100, "watch_seconds": 99}),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                pipe = EventPipeline(f"{DAY}T08:00:00")
                pipe.ingest(_event("z"))
                r = pipe.ingest(_event("z", **{field: value}))
                self.assertEqual(r.classification, "conflict")
                self.assertFalse(r.accepted)
                self.assertEqual(r.conflicts, [field])
                self.assertEqual(len(pipe.conflicts()), 1)
                self.assertEqual(pipe.conflicts("z")[0]["differences"], [field])

    def test_conflict_keeps_first_record_and_metric(self):
        pipe = EventPipeline(f"{DAY}T08:00:00")
        pipe.ingest(_event("m", data={"declared_duration": 100,
                                       "watch_seconds": 50}))
        before = pipe.compute_short_term(DAY)
        # 缓存损坏重发：同编号、不同观看时长，试图把完播率从 0.5 改成 0.99
        r = pipe.ingest(_event("m", data={"declared_duration": 100,
                                          "watch_seconds": 99}))
        self.assertEqual(r.classification, "conflict")
        after = pipe.compute_short_term(DAY)
        self.assertEqual(after, before, "冲突重投不得改写指标")
        self.assertEqual(after["metrics"]["completion_rate"], 0.5)
        # 冲突证据同时保留首条与挑战载荷，可查询、可核对
        c = pipe.conflicts("m")[0]
        self.assertEqual(c["first"]["data"]["watch_seconds"], 50)
        self.assertEqual(c["challenger"]["data"]["watch_seconds"], 99)
        self.assertIn("业务数据", c["difference_labels"])
        # 窗口里仍只有首条，且未被计成重复
        self.assertEqual(pipe.audit()["windows"][DAY]["events"], 1)
        self.assertEqual(pipe.audit()["windows"][DAY]["duplicate_deliveries"], 0)

    def test_conflict_then_identical_retry_is_still_idempotent(self):
        pipe = EventPipeline(f"{DAY}T08:00:00")
        pipe.ingest(_event("k"))
        pipe.ingest(_event("k", content_id="OTHER"))   # 冲突，被拒
        # 随后真正相同的重试到达：仍按首条幂等命中，不双计、不再报冲突
        self.assertEqual(pipe.ingest(_event("k")).classification, "duplicate")
        self.assertEqual(len(pipe.conflicts("k")), 1)
        self.assertEqual(pipe.audit()["windows"][DAY]["events"], 1)

    def test_conflict_after_finalization_is_detected_and_evidence_kept(self):
        pipe = EventPipeline(f"{DAY}T08:00:00")
        pipe.ingest(_event("f"))
        before = pipe.compute_short_term(DAY)
        pipe.finalize_due("2026-09-04T00:00:00")
        # 定稿后同载重试：幂等命中，不回改冻结窗口
        same = _event("f", received_at="2026-09-04T01:00:00")
        self.assertEqual(pipe.ingest(same).classification, "duplicate")
        # 定稿后同编号异载：照样识别冲突、拒绝覆盖、留证并隔离
        diff = _event("f", content_id="C9",
                      received_at="2026-09-04T02:00:00")
        r = pipe.ingest(diff)
        self.assertEqual(r.classification, "conflict")
        self.assertTrue(r.conflicts)
        c = pipe.conflicts("f")[0]
        self.assertTrue(c["challenger_after_finalization"])
        self.assertTrue(c["stored_window_finalized"])
        self.assertEqual(pipe.compute_short_term(DAY), before,
                         "定稿后的冲突载荷不得改写冻结指标")
        self.assertTrue(any(q.get("conflict_id") == c["conflict_id"]
                            for q in pipe.audit()["quarantine"]))

    def test_revoke_first_then_original_is_void_even_under_redelivery(self):
        pipe = EventPipeline(f"{DAY}T08:00:00")
        self.assertEqual(pipe.ingest(
            {"event_id": "v", "revoke": True,
             "received_at": f"{DAY}T08:30:00"}).classification, "pending_revoke")
        # 原事件后到：作废，不进指标
        self.assertEqual(pipe.ingest(_event("v")).classification,
                         "revoked_on_arrival")
        self.assertEqual(pipe.compute_short_term(DAY)["denominator"]["exposures"], 0)
        # 作废后再次同载重试：仍是针对首条记录的幂等命中，不能让事件复活
        self.assertEqual(pipe.ingest(_event("v")).classification, "duplicate")
        self.assertEqual(pipe.compute_short_term(DAY)["denominator"]["exposures"], 0)

    def test_revoke_then_conflict_payload_does_not_resurrect_or_overwrite(self):
        pipe = EventPipeline(f"{DAY}T08:00:00")
        pipe.ingest(_event("w"))
        pipe.ingest({"event_id": "w", "revoke": True,
                     "received_at": f"{DAY}T10:00:00"})
        # 撤回已生效后，缓存损坏的异载重发：仍是冲突，不能借"重试"覆盖或复活
        r = pipe.ingest(_event("w", content_id="C_NEW"))
        self.assertEqual(r.classification, "conflict")
        self.assertEqual(pipe.compute_short_term(DAY)["denominator"]["exposures"], 0)
        self.assertEqual(len(pipe.conflicts("w")), 1)

    def test_out_of_order_replay_is_metric_identical_without_conflicts(self):
        """合法流（同载重试 + 撤回乱序）任意顺序重放，指标与撤回数逐位一致。"""
        rows = [
            _event("e1"),
            _event("e2", anon_id="a2", content_id="c2"),
            {"event_id": "e3", "revoke": True,
             "received_at": f"{DAY}T08:00:00"},
            _event("e3", anon_id="a3", content_id="c3"),
            _event("e2", anon_id="a2", content_id="c2"),  # e2 同载真重试
        ]
        p1, p2 = EventPipeline(f"{DAY}T08:00:00"), EventPipeline(f"{DAY}T08:00:00")
        for r in rows:
            p1.ingest(r)
        for r in reversed(rows):
            p2.ingest(r)
        self.assertEqual(p1.compute_short_term(DAY), p2.compute_short_term(DAY))
        self.assertEqual(p1.audit()["windows"][DAY]["revoked"], 1)
        self.assertEqual(p2.audit()["windows"][DAY]["revoked"], 1)
        self.assertEqual(p1.conflicts(), [])
        self.assertEqual(p2.conflicts(), [])

    def test_conflict_detected_in_either_arrival_order(self):
        """缓存损坏的异载重发：无论哪个载荷先到，都拒绝覆盖、留同一条冲突证据。"""
        original = _event("c", data={"declared_duration": 100,
                                     "watch_seconds": 50})
        corrupted = _event("c", data={"declared_duration": 100,
                                      "watch_seconds": 99})
        # 顺序一：原始先到
        p_first = EventPipeline(f"{DAY}T08:00:00")
        p_first.ingest(original)
        m1 = p_first.compute_short_term(DAY)
        r1 = p_first.ingest(corrupted)
        # 顺序二：损坏载荷先到（模拟重发抢先）
        p_second = EventPipeline(f"{DAY}T08:00:00")
        p_second.ingest(corrupted)
        m2 = p_second.compute_short_term(DAY)
        r2 = p_second.ingest(original)

        for r in (r1, r2):
            self.assertEqual(r.classification, "conflict")
            self.assertEqual(r.conflicts, ["data"])
        # 两个顺序都各自保留"先到"的那条，后到者绝不覆盖，指标不再变动
        self.assertEqual(p_first.compute_short_term(DAY), m1)
        self.assertEqual(p_second.compute_short_term(DAY), m2)
        self.assertEqual(p_first.conflicts("c")[0]["first"]["data"]["watch_seconds"], 50)
        self.assertEqual(p_second.conflicts("c")[0]["first"]["data"]["watch_seconds"], 99)
        # 两种顺序都留下可查询证据，运营据此知道该窗口需人工复核
        self.assertEqual(len(p_first.conflicts()), 1)
        self.assertEqual(len(p_second.conflicts()), 1)

    def test_restart_preserves_idempotency_conflict_and_finalization(self):
        pipe = EventPipeline(f"{DAY}T08:00:00")
        pipe.ingest(_event("s"))
        pipe.ingest(_event("s", content_id="OTHER"))   # 冲突
        pipe.ingest({"event_id": "v2", "revoke": True,
                     "received_at": f"{DAY}T08:30:00"})  # 撤回先到挂起
        pipe.finalize_due("2026-09-04T00:00:00")

        restored = EventPipeline.restore_state(pipe.dump_state())
        # 定稿状态保留
        self.assertTrue(restored._windows[DAY].finalized)
        # 冲突证据保留
        self.assertEqual(len(restored.conflicts("s")), 1)
        # 同载重试仍幂等
        same = _event("s", received_at="2026-09-04T03:00:00")
        self.assertEqual(restored.ingest(same).classification, "duplicate")
        # 又一个异载仍被识别为新冲突（不覆盖首条）
        again = restored.ingest(_event("s", anon_id="a9",
                                       received_at="2026-09-04T03:30:00"))
        self.assertEqual(again.classification, "conflict")
        self.assertEqual(len(restored.conflicts("s")), 2)
        # 挂起的撤回在重启后仍然生效：原事件到达即作废
        self.assertEqual(
            restored.ingest(_event("v2",
                                   received_at="2026-09-04T04:00:00")).classification,
            "quarantined_late")

    def test_platform_restart_keeps_full_judgement_consistent(self):
        pf = Platform(f"{DAY}T08:00:00")
        pf.set_user_attributes("u1", {"taste": "culture"})
        decision = pf.route("u1", f"{DAY}T09:00:00")
        anon_before = pf.privacy.anon_id("u1")
        pf.ingest({"event_id": "pe", "kind": "exposure",
                   "occurred_at": f"{DAY}T09:00:00",
                   "decision_seq": decision["seq"], "content_id": "c1",
                   "data": {"declared_duration": 100, "watch_seconds": 40}})
        before = pf.report_short(DAY)

        # 模拟进程重启：从快照重建整个平台
        revived = Platform.restore_state(pf.dump_state())
        self.assertEqual(revived.privacy.anon_id("u1"), anon_before,
                         "重启后假名保持一致，事件可继续按主体去重")
        # 同载重试幂等；异载冲突并留证；指标不被改写
        self.assertEqual(revived.ingest({
            "event_id": "pe", "kind": "exposure",
            "occurred_at": f"{DAY}T09:00:00",
            "decision_seq": decision["seq"], "content_id": "c1",
            "data": {"declared_duration": 100, "watch_seconds": 40}}
        )["classification"], "duplicate")
        clash = revived.ingest({
            "event_id": "pe", "kind": "exposure",
            "occurred_at": f"{DAY}T09:00:00",
            "decision_seq": decision["seq"], "content_id": "c1",
            "data": {"declared_duration": 100, "watch_seconds": 99}})
        self.assertEqual(clash["classification"], "conflict")
        self.assertEqual(revived.report_short(DAY), before)
        self.assertEqual(len(revived.conflicts("pe")), 1)

    def test_concurrent_same_id_different_payload_does_not_double_store(self):
        """并发同编号异载：恰好一个落库，另一个判冲突，绝不双写覆盖。"""
        pipe = EventPipeline(f"{DAY}T08:00:00")
        results = []
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()  # 最大化同时进入 ingest 的概率
            payload = _event("race", data={"declared_duration": 100,
                                           "watch_seconds": 50 + i})
            results.append(pipe.ingest(payload).classification)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stored = pipe.audit()["windows"][DAY]["events"]
        self.assertEqual(stored, 1, "同编号只能有一条落库")
        self.assertEqual(results.count("counted"), 1)
        self.assertEqual(results.count("conflict"), 7)
        self.assertEqual(len(pipe.conflicts("race")), 7)

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
