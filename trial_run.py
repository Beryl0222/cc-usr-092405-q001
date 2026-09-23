"""端到端试运行：在不训练新模型的前提下，验证推荐策略治理全链路。

覆盖需求点名的场景：
1. 策略提交（目标/权重/人群/有效期）→ 内容+风险双批准 → 小流量分发；
2. 长内容（知识长讲解、非遗慢直播）与短视频共存，多信号口径不偏向短视频；
3. 事件迟到、撤回（含乱序先到）、重复投递均不污染指标；定稿窗口冻结；
4. 实验中途调权另开分段，回滚后分流回落基线，分段独立统计；
5. 复现某日每一次分流采用的策略与桶位；
6. 不偷换口径地比较短期互动与长期回访（口径版本一致、分母一致）；
7. 关闭画像 / 重置兴趣后历史偏好失效、假名不可链接；
8. 创作者可解释独立通道进出；聚合报表 k 匿名抑制。

运行：python3 trial_run.py
"""

import json

from gov.events import EventPipeline
from gov.platform import Platform

DAY1 = "2026-09-02"          # 队落日
T0 = "2026-09-01T08:00:00"
T_OPEN = "2026-09-02T09:00:00"
T_ADJUST = "2026-09-03T10:00:00"
T_ROLLBACK = "2026-09-03T12:00:00"
T_AFTER = "2026-09-03T13:00:00"

LONG_LECTURE = {"content_id": "C_LECTURE_4H", "declared_duration": 4 * 3600,
                "watch_seconds": int(4 * 3600 * 0.8), "category": "知识讲解"}
SLOW_LIVE = {"content_id": "C_HERITAGE_LIVE", "declared_duration": 3 * 3600,
             "watch_seconds": int(3 * 3600 * 0.55), "category": "非遗慢直播"}
SHORT_VIDEO = {"content_id": "C_TEXT_FILM_CLIP", "declared_duration": 60,
               "watch_seconds": 57, "category": "经典课文影像"}


def show(title, payload):
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_policy(weights, pid_note=""):
    return dict(
        title=f"多信号纠偏策略{pid_note}",
        goal="纠正以完播率为唯一核心指标导致长内容无曝光的问题，抑制兴趣茧房",
        weights=weights,
        audience={"include": {"taste": "culture"}, "exclude": {"is_internal": True}},
        effective_from="2026-09-01T00:00:00",
        effective_to="2026-09-30T23:59:59",
        owner="产品经理-林沙",
        rollout=0.10,
    )


WEIGHTS_V1 = {
    "effective_watch_share": 0.20, "completion_rate": 0.15,
    "favorite_rate": 0.20, "discussion_quality": 0.15,
    "diversity_surface": 0.10, "return_visit_d7": 0.20,
}
WEIGHTS_V2 = {  # 中途调权：上调收藏与长期回访，下调完播
    "effective_watch_share": 0.15, "completion_rate": 0.05,
    "favorite_rate": 0.25, "discussion_quality": 0.15,
    "diversity_surface": 0.15, "return_visit_d7": 0.25,
}


def main():
    pf = Platform(T0)

    # 1) 提交策略并双批准 -------------------------------------------------
    p1 = pf.submit_policy(build_policy(WEIGHTS_V1, "V1"))
    assert p1["status"] == "待批准"
    pf.approve_policy(p1["id"], role="内容负责人", approver="总编-周言",
                      reason="长内容口径合理")
    mid = pf.policies.get(p1["id"])
    assert mid.status == "待批准", "单方批准不得生效"
    pf.approve_policy(p1["id"], role="风险负责人", approver="风控-郑铉",
                      reason="流量在上限内，可试")
    assert pf.policies.get(p1["id"]).status == "已批准"
    exp = pf.open_experiment(name="多信号纠偏实验", policy_id=p1["id"],
                             salt="culture-exp-2026-09")
    exp_id = exp["id"]

    # 2) 准备用户并在 DAY1 分流 -------------------------------------------
    pf.tick(T_OPEN)
    N = 60
    for i in range(N):
        ref = f"u{i:03d}"
        pf.set_user_attributes(ref, {"taste": "culture", "is_internal": False})
        pf.route(ref, f"{DAY1}T09:{i % 60:02d}:00")
    pf.set_user_attributes("u_outsider", {"taste": "variety", "is_internal": False})
    pf.route("u_outsider", f"{DAY1}T09:30:00")  # 不在人群 -> 基线

    day1_decisions = pf.hub.decisions_on(DAY1)
    in_exp = [d for d in day1_decisions if d.in_experiment]
    assert in_exp, "10% 小流量下应有人命中实验桶"
    assert any(d.strategy == "BASELINE" and d.experiment_id for d in day1_decisions), \
        "应存在对照桶用户"
    assert all(d.strategy == "BASELINE" for d in day1_decisions
               if d.anon_id == pf.privacy.anon_id("u_outsider"))

    # 3) 长短内容曝光与反馈（去标识化盖戳） -------------------------------
    contents = [LONG_LECTURE, SLOW_LIVE, SHORT_VIDEO]
    for d in day1_decisions:
        for ci, c in enumerate(contents):
            eid = f"EXP-{d.seq}-{c['content_id']}"
            pf.ingest({
                "event_id": eid, "kind": "exposure",
                "occurred_at": f"{DAY1}T09:{(d.seq + ci) % 60:02d}:10",
                "decision_seq": d.seq, "content_id": c["content_id"], "data": c,
            })
        # 收藏：长内容更常被收藏
        c0 = contents[d.seq % 3]
        pf.ingest({"event_id": f"FAV-{d.seq}", "kind": "favorite",
                   "occurred_at": f"{DAY1}T10:00:00",
                   "decision_seq": d.seq,
                   "content_id": c0["content_id"]})
    # 质量评论
    for d in day1_decisions[::4]:
        pf.ingest({"event_id": f"CMT-{d.seq}", "kind": "comment",
                   "occurred_at": f"{DAY1}T11:00:00", "decision_seq": d.seq,
                   "content_id": LONG_LECTURE["content_id"],
                   "data": {"quality_pass": True}})

    # 4) 重复、撤回乱序、迟到撤回 ------------------------------------------
    target = day1_decisions[0]
    dup = {"event_id": f"DUP-{target.seq}", "kind": "favorite",
           "occurred_at": f"{DAY1}T15:00:00", "decision_seq": target.seq,
           "content_id": LONG_LECTURE["content_id"]}
    pf.ingest(dup)
    r1 = pf.ingest(dict(dup))  # 重复投递
    assert r1["classification"] == "duplicate"

    # 撤回先到（乱序），原事件后到 -> 作废
    pf.ingest({"event_id": "REORDER-1", "revoke": True,
               "received_at": f"{DAY1}T12:01:00"})
    r2 = pf.ingest({"event_id": "REORDER-1", "kind": "favorite",
                    "occurred_at": f"{DAY1}T12:00:00",
                    "decision_seq": target.seq,
                    "content_id": SLOW_LIVE["content_id"]})
    assert r2["classification"] == "revoked_on_arrival"

    metrics_before = pf.report_short(DAY1)

    # 5) 中途调权：新策略双批准 + 另开分段（DAY1 窗口尚未定稿） -----------
    pf.tick(T_ADJUST)
    p2 = pf.submit_policy(build_policy(WEIGHTS_V2, "V2"))
    pf.approve_policy(p2["id"], role="内容负责人", approver="总编-周言",
                      reason="强化长期信号")
    pf.approve_policy(p2["id"], role="风险负责人", approver="风控-郑铉",
                      reason="继续限流式")
    snap = pf.adjust_weights(exp_id, p2["id"], reason="首日数据显示收藏信号偏弱")
    assert len(snap["segments"]) == 2
    assert snap["segments"][0]["end_ts"] == T_ADJUST
    assert snap["segments"][0]["is_live"] is False
    assert snap["segments"][1]["is_live"] is True
    assert snap["segments"][0]["weights"] != snap["segments"][1]["weights"]

    # 6) 紧急回滚 ----------------------------------------------------------
    pf.tick(T_ROLLBACK)
    rolled = pf.rollback(exp_id, reason="监测到非遗品类曝光异常集中，紧急止血")
    assert all(s["end_ts"] for s in rolled["segments"]), "回滚须闭合所有进行中分段"
    assert pf.policies.get(p2["id"]).status == "已回滚"
    pf.tick(T_AFTER)
    after = [pf.route(f"u{i:03d}", T_AFTER) for i in range(N)]
    assert all(d["strategy"] == "BASELINE" and not d["in_experiment"] for d in after), \
        "回滚后不得再分流进实验"

    # 7) 复现 DAY1 的每一次分流 -------------------------------------------
    repro = pf.hub.verify_reproduction(DAY1)
    assert repro["reproducible"] and repro["recomputed"] > 0

    # 推进水位冻结 DAY1（定稿时间 = DAY1 后一天 + 1 天宽限）
    pf.tick("2026-09-05T00:00:00")
    audit = pf.events.audit()
    assert audit["windows"][DAY1]["finalized"]
    # 迟到撤回：窗口已定稿 -> 隔离 + 修订台账，指标冻结不回改
    r3 = pf.ingest({"event_id": f"FAV-{day1_decisions[1].seq}", "revoke": True,
                    "received_at": "2026-09-05T09:00:00"})
    assert r3["classification"] == "quarantined_late"
    # 迟到的新事件同样被隔离
    r4 = pf.ingest({"event_id": "LATE-EVT-1", "kind": "favorite",
                    "occurred_at": f"{DAY1}T20:00:00",
                    "received_at": "2026-09-05T09:05:00",
                    "decision_seq": day1_decisions[2].seq,
                    "content_id": SHORT_VIDEO["content_id"]})
    assert r4["classification"] == "quarantined_late"
    assert pf.report_short(DAY1) == metrics_before, "定稿后指标必须保持冻结"

    # 乱序无害：同一批事件以任意顺序喂给独立管道，结果逐位一致
    order_independent = demonstrate_order_independence()
    assert order_independent

    # 8) 长期回访：d7 成熟给终值，d30 未成熟只标 pending；再推进后出终值 ---
    # 给实验桶用户造跨日回访（落在 7 日窗口内）
    for d in in_exp:
        pf.ingest({"event_id": f"RET-{d.seq}-d3", "kind": "return_visit",
                   "occurred_at": "2026-09-05T20:00:00", "decision_seq": d.seq,
                   "content_id": LONG_LECTURE["content_id"]})
    pf.tick("2026-09-06T00:00:00")
    early_long = pf.compare_segments(DAY1, exp_id)
    rows = early_long["rows"]
    assert all(r["catalog_version"] == "v2.0" for r in rows), "对比必须同口径版本"
    exp_row = next(r for r in rows if r["variant"] == "experiment"
                   and r["segment_seq"] == 1)
    assert exp_row["long_term"]["return_visit_d7"]["status"] == "pending_maturity"
    # 时间快进到 d30 窗口成熟之后
    pf.tick("2026-10-10T00:00:00")
    comparison = pf.compare_segments(DAY1, exp_id)
    final_row = next(r for r in comparison["rows"] if r["variant"] == "experiment"
                     and r["segment_seq"] == 1)
    control_row = next(r for r in comparison["rows"] if r["variant"] == "control"
                       and r["segment_seq"] == 1)
    assert final_row["long_term"]["return_visit_d7"]["status"] == "final"
    assert final_row["long_term"]["return_visit_d30"]["status"] == "final"
    # 分段1与分段2分列，未被合并成"一次连续实验"
    seqs = {r["segment_seq"] for r in comparison["rows"] if r["variant"] != "baseline"}
    assert seqs == {1, 2}

    # 9) 关闭画像 / 重置兴趣 -----------------------------------------------
    pf.set_user_attributes("u_private", {"taste": "culture", "is_internal": False})
    pf.privacy.record_preference("u_private", "知识讲解", 1.0)
    pf.disable_profiling("u_private")
    assert pf.privacy.effective_preferences("u_private") == {}
    decision = pf.route("u_private", "2026-10-10T09:00:00")
    assert decision["strategy"] == "BASELINE" and "关闭画像" in decision["reason"]
    try:
        pf.privacy.record_preference("u_private", "非遗", 1.0)
        raise AssertionError("关闭画像期间不得再写入偏好")
    except ValueError:
        pass
    old_anon = pf.privacy.anon_id("u_private")
    after_reset = pf.reset_interests("u_private")
    new_anon = after_reset["anon_id"]
    assert old_anon != new_anon and after_reset["preference_categories"] == []
    # 旧盐销毁：旧假名不可再生成，旧/新假名不可链接
    old_epoch = int(old_anon[1:].split("_")[0])
    try:
        pf.privacy.pseudo.anonymize("u_private", old_epoch)
        raise AssertionError("旧纪元盐必须已销毁")
    except ValueError:
        pass

    # 10) 创作者通道解释 ----------------------------------------------------
    pf.channel_enter({
        "content_id": LONG_LECTURE["content_id"], "creator_id": "CR_顾老师",
        "reason": "多信号策略下有效观看时长与收藏率达标，进入长内容独立通道",
        "signals": {"effective_watch_share": 0.8, "favorite_rate": 0.34},
        "basis": f"{p1['id']} / {exp_id} 分段1"})
    pf.channel_exit({
        "content_id": LONG_LECTURE["content_id"],
        "reason": "紧急回滚期间独立通道暂停，退出走基线分发",
        "signals": {"rollback": T_ROLLBACK}, "basis": f"回滚 {exp_id}"})
    explanation = pf.channel_explain(LONG_LECTURE["content_id"])
    assert explanation["history"][0]["in_channel"] is True
    assert explanation["history"][1]["in_channel"] is False

    # 11) k 匿名聚合 + 去标识化守门 ----------------------------------------
    report = pf.k_anonymous_report({
        "知识讲解": {"users": 42, "metrics": {"favorite_rate": 0.31}},
        "非遗慢直播": {"users": 2, "metrics": {"favorite_rate": 0.5}},  # 抑制
    }, k=5)
    assert "非遗慢直播" in report["suppressed_groups"]
    assert "非遗慢直播" not in report["released"]
    leaked = pf.ingest({"event_id": "BAD-1", "kind": "exposure",
                        "user_id": "real-identity", "anon_id": "a1_x",
                        "content_id": "c", "occurred_at": f"{DAY1}T10:00:00"})
    assert leaked["classification"] == "rejected"

    # ---- 输出留档 ----
    show("策略 V1（双批准后）", pf.policies.get(p1["id"]).public())
    show("实验快照（分段1已被调权闭合、分段2已回滚）", pf.hub.get(exp_id).snapshot())
    show(f"{DAY1} 分流复现", repro)
    show("短期指标（实验桶 vs 对照桶，分段1）", {
        "experiment": final_row["short_term"], "control": control_row["short_term"]})
    show("长期回访（口径一致，成熟后终值）", {
        "experiment": final_row["long_term"], "control": control_row["long_term"]})
    show("定稿后迟到数据隔离与修订台账", {
        "quarantine": pf.events.audit()["quarantine"],
        "revision_ledger": pf.events.audit()["revision_ledger"],
        "duplicate_deliveries": pf.events.audit()["windows"][DAY1]["duplicate_deliveries"],
    })
    show("兴趣重置后的隐私状态", after_reset)
    show("创作者通道解释", explanation)
    show("k 匿名聚合报表", report)
    print("\n试运行通过：审批、分段、去污染、复现、同口径比较与隐私约束全部满足。")


def demonstrate_order_independence() -> bool:
    """同一批事件（含乱序撤回）正序/逆序喂两个管道，结果必须逐位相同。"""
    base_day = "2026-09-02"
    rows = [
        {"event_id": "e1", "kind": "exposure", "occurred_at": f"{base_day}T09:00:00",
         "anon_id": "a1", "content_id": "c1",
         "data": {"declared_duration": 100, "watch_seconds": 80, "category": "知识讲解"}},
        {"event_id": "e2", "kind": "exposure", "occurred_at": f"{base_day}T09:05:00",
         "anon_id": "a2", "content_id": "c2",
         "data": {"declared_duration": 100, "watch_seconds": 40, "category": "非遗"}},
        {"event_id": "e3", "kind": "favorite", "occurred_at": f"{base_day}T10:00:00",
         "anon_id": "a1", "content_id": "c1"},
        {"event_id": "e3", "revoke": True, "received_at": f"{base_day}T08:00:00"},
        {"event_id": "e2", "kind": "exposure", "occurred_at": f"{base_day}T09:05:00",
         "anon_id": "a2", "content_id": "c2",
         "data": {"declared_duration": 100, "watch_seconds": 40, "category": "非遗"}},
    ]  # 末行是 e2 的同负载重复投递，验证幂等
    p1, p2 = EventPipeline("2026-09-02T08:00:00"), EventPipeline("2026-09-02T08:00:00")
    for r in rows:
        p1.ingest(r)
    for r in reversed(rows):
        p2.ingest(r)
    return p1.compute_short_term(base_day) == p2.compute_short_term(base_day) \
        and p1.audit()["windows"][base_day]["revoked"] == 1 \
        and p2.audit()["windows"][base_day]["revoked"] == 1


if __name__ == "__main__":
    main()
