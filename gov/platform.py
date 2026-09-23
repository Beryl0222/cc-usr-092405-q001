"""平台应用服务：编排审批、实验、事件、隐私与创作者通道。

这是领域模块对外的唯一门面（facade）。HTTP 层与试运行脚本只调用本类，
不直接拼装各模块，以保证分流盖戳、去标识化、分段隔离等约束集中生效。
"""

from typing import Dict, List, Optional

from . import events as ev_mod
from . import metrics as metric_dir
from . import policies as pol_mod
from .channels import DEFAULT_K, ChannelRegistry, kanonymize
from .events import EventPipeline
from .experiments import BASELINE_STRATEGY, ExperimentHub
from .policies import PolicyRegistry
from .privacy import PrivacyStore


class Platform:
    def __init__(self, now: str):
        self.clock = now
        self.policies = PolicyRegistry()
        self.hub = ExperimentHub()
        self.events = EventPipeline(now)
        self.privacy = PrivacyStore()
        self.channels = ChannelRegistry()
        # 用户人群属性（非敏感分群标签，不含观看明细），用于适用人群判定
        self._user_attrs: Dict[str, dict] = {}

    # ---------- 时钟 ----------
    def tick(self, now: str) -> List[str]:
        self.clock = now
        return self.events.finalize_due(now)

    # ---------- 人群属性 ----------
    def set_user_attributes(self, user_ref: str, attrs: dict) -> None:
        self._user_attrs[user_ref] = dict(attrs)

    def _audience_match(self, user_ref: str, audience: dict) -> bool:
        attrs = self._user_attrs.get(user_ref, {})
        for key, want in audience.get("include", {}).items():
            if attrs.get(key) != want:
                return False
        for key, block in audience.get("exclude", {}).items():
            if attrs.get(key) in (block if isinstance(block, list) else [block]):
                return False
        return True

    # ---------- 策略治理 ----------
    def submit_policy(self, payload: dict) -> dict:
        p = self.policies.submit(now=self.clock, **payload)
        self.policies.send_for_approval(p.id, self.clock)
        return p.public()

    def approve_policy(self, pid: str, *, role, approver, reason="") -> dict:
        return self.policies.approve(
            pid, role=role, approver=approver, reason=reason, now=self.clock).public()

    def reject_policy(self, pid: str, *, role, approver, reason) -> dict:
        return self.policies.reject(
            pid, role=role, approver=approver, reason=reason, now=self.clock).public()

    def open_experiment(self, *, name, policy_id, salt) -> dict:
        policy = self.policies.get(policy_id)
        exp = self.hub.open(name=name, policy=policy, salt=salt, start_ts=self.clock)
        return exp.snapshot()

    def adjust_weights(self, exp_id: str, new_policy_id: str, *, reason: str) -> dict:
        """中途调权：必须基于另一份已批准的新策略，另开分段。"""
        exp = self.hub.get(exp_id)
        policy = self.policies.get(new_policy_id)
        if policy.status != "已批准":
            raise pol_mod.PolicyError("调权所依据的新策略必须已完成双批准")
        exp.adjust_weights(policy=policy, now=self.clock, reason=reason)
        return exp.snapshot()

    def rollback(self, exp_id: str, *, reason: str) -> dict:
        self.hub.rollback(exp_id, now=self.clock, reason=reason,
                          policies=self.policies)
        return self.hub.get(exp_id).snapshot()

    # ---------- 分流 ----------
    def route(self, user_ref: str, ts: Optional[str] = None) -> dict:
        """对真实用户执行一次分流：内部假名化，外部拿不到原始身份关联。"""
        ts = ts or self.clock
        u = self.privacy._user(user_ref)
        anon = self.privacy.anon_id(user_ref)

        def resolver(policy_id):
            return self._audience_match(user_ref, self.policies.get(policy_id).audience)

        d = self.hub.assign(
            anon_id=anon, ts=ts, audience_match=True,
            profiling_enabled=u.profiling_enabled,
            audience_resolver=resolver)
        return d.public()

    # ---------- 事件入库（去标识化、盖戳） ----------
    def ingest(self, raw: dict) -> dict:
        # 只接受假名；若客户端误传真实身份，管道会拒收
        if "decision_seq" in raw:
            d = next((x for x in self.hub.decisions if x.seq == raw["decision_seq"]), None)
            if d is not None:
                raw = dict(raw)
                raw.setdefault("anon_id", d.anon_id)
                raw.setdefault("experiment_id", d.experiment_id)
                raw.setdefault("segment_seq", d.segment_seq)
                raw.setdefault("strategy", d.strategy)
                if d.experiment_id is None:
                    raw.setdefault("variant", "baseline")
                else:
                    raw.setdefault("variant",
                                   "experiment" if d.in_experiment else "control")
        result = self.events.ingest(raw)
        return vars(result)

    # ---------- 指标（同口径，短期/长期分开） ----------
    def report_short(self, day: str, scope: Optional[dict] = None) -> dict:
        return self.events.compute_short_term(day, scope)

    def report_long(self, day: str, scope: Optional[dict] = None) -> dict:
        return self.events.compute_long_term(day, scope, now=self.clock)

    def compare_segments(self, day: str, experiment_id: str) -> dict:
        """在不偷换口径的前提下比较：各分段实验桶 vs 对照桶，短期与长期分列。"""
        exp = self.hub.get(experiment_id)
        rows = []
        for seg in exp.segments:
            for variant in ("experiment", "control"):
                scope = {"experiment_id": experiment_id,
                         "segment_seq": seg.seq, "variant": variant}
                rows.append({
                    "segment_seq": seg.seq,
                    "segment_live": seg.end_ts is None,
                    "variant": variant,
                    "catalog_version": seg.catalog_version,
                    "short_term": self.events.compute_short_term(day, scope)["metrics"],
                    "long_term": self.report_long(day, scope)["metrics"],
                })
        baseline = {"variant": "baseline"}
        rows.append({
            "segment_seq": None, "variant": "baseline",
            "catalog_version": metric_dir.CATALOG_VERSION,
            "short_term": self.events.compute_short_term(day, baseline)["metrics"],
            "long_term": self.report_long(day, baseline)["metrics"],
        })
        return {"day": day, "experiment_id": experiment_id,
                "rule": "分段独立统计，口径版本一致方可横向比较；长期指标未成熟只标 pending",
                "rows": rows}

    # ---------- 隐私 ----------
    def disable_profiling(self, user_ref: str) -> dict:
        return self.privacy.disable_profiling(user_ref, self.clock)

    def reset_interests(self, user_ref: str) -> dict:
        return self.privacy.reset_interests(user_ref, self.clock)

    def privacy_status(self, user_ref: str) -> dict:
        return self.privacy.status(user_ref)

    # ---------- 创作者通道 ----------
    def channel_enter(self, payload: dict) -> dict:
        return self.channels.enter(now=self.clock, **payload).public()

    def channel_exit(self, payload: dict) -> dict:
        return self.channels.exit(now=self.clock, **payload).public()

    def channel_explain(self, content_id: str) -> dict:
        return self.channels.explain(content_id)

    def k_anonymous_report(self, groups: dict, k: int = DEFAULT_K) -> dict:
        return kanonymize(groups, k)

    # ---------- 复现与审计 ----------
    def reproduce_day(self, day: str) -> dict:
        result = self.hub.verify_reproduction(day)
        result["decisions_log"] = [
            d.public() for d in self.hub.decisions_on(day)]
        result["experiments"] = {eid: e.snapshot()
                                 for eid, e in self.hub._items.items()}
        return result

    def audit(self) -> dict:
        return {
            "clock": self.clock,
            "metric_catalog": metric_dir.catalog_snapshot()["catalog_version"],
            "policies": [p.public() for p in self.policies.list()],
            "experiments": [e.snapshot() for e in self.hub._items.values()],
            "events": self.events.audit(),
        }
