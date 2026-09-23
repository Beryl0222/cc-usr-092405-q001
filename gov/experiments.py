"""实验分段与确定性分流。

核心约定：
- 一个实验由若干**不可变分段（Segment）**组成。分段一旦开启，其权重、
  流量比例、口径版本即冻结；中途调权必须关闭当前分段、另开新分段，
  新分段使用新的分桶盐值（重新分流），统计上分段各自独立，绝不把
  两段拼成"一次连续实验"。
- 分流是确定性的：bucket = HMAC(实验盐, 假名ID|实验|分段序号)，同一
  假名用户在同一分段内结果稳定，且任何一天的每一次分流都可凭
  决策日志 + 实验快照离线复算复现。
- 紧急回滚立即闭合当前分段；其后的分流一律回落基线策略，不再进实验。
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from itertools import count
from typing import Dict, List, Optional

from . import metrics as metric_dir

BASELINE_STRATEGY = "BASELINE"
# 基线：不以完播率为唯一指标，保留对长内容友好的多信号口径。
BASELINE_WEIGHTS = {
    "effective_watch_share": 0.20,
    "completion_rate": 0.15,
    "favorite_rate": 0.20,
    "discussion_quality": 0.15,
    "diversity_surface": 0.10,
    "return_visit_d7": 0.20,
}

BUCKET_SPACE = 10000  # 万分位，rollout 按此量化


class ExperimentError(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    seq: int
    policy_id: str
    weights: Dict[str, float]
    rollout: float
    catalog_version: str
    salt: str
    start_ts: str
    end_ts: Optional[str] = None
    close_reason: Optional[str] = None

    def public(self) -> dict:
        return {
            "seq": self.seq, "policy_id": self.policy_id,
            "weights": dict(self.weights), "rollout": self.rollout,
            "catalog_version": self.catalog_version, "salt": self.salt,
            "start_ts": self.start_ts, "end_ts": self.end_ts,
            "close_reason": self.close_reason,
            "is_live": self.end_ts is None,
        }


@dataclass
class Decision:
    """一次分流的完整记录——复现"某日每次分流采用的策略"靠它。"""
    ts: str
    day: str
    seq: int                 # 全局单调序号，乱序到达也能还原真实次序
    anon_id: str
    experiment_id: Optional[str]
    segment_seq: Optional[int]
    bucket: Optional[int]
    strategy: str            # 实际采用的策略：实验策略 id 或 BASELINE
    in_experiment: bool
    reason: str

    def public(self) -> dict:
        return vars(self)


class Experiment:
    def __init__(self, exp_id: str, name: str, policy, salt: str, start_ts: str):
        self.id = exp_id
        self.name = name
        self.salt = salt
        self.segments: List[Segment] = []
        self.rolled_back = False
        self._open_segment(
            policy_id=policy.id, weights=policy.weights,
            rollout=policy.rollout, start_ts=start_ts,
        )

    @property
    def live_segment(self) -> Optional[Segment]:
        live = [s for s in self.segments if s.end_ts is None]
        return live[0] if live else None

    def _open_segment(self, *, policy_id, weights, rollout, start_ts):
        seq = len(self.segments) + 1
        seg = Segment(
            seq=seq, policy_id=policy_id, weights=dict(weights),
            rollout=rollout, catalog_version=metric_dir.CATALOG_VERSION,
            salt=f"{self.salt}:seg{seq}", start_ts=start_ts,
        )
        self.segments.append(seg)
        return seg

    def segment_at(self, ts: str) -> Optional[Segment]:
        """返回 ts 时刻生效的分段：要求 start_ts <= ts，且未关闭或关闭晚于 ts。"""
        for s in self.segments:
            if s.start_ts <= ts and (s.end_ts is None or ts < s.end_ts):
                return s
        return None

    def adjust_weights(self, *, policy, now: str, reason: str) -> Segment:
        """中途调权：闭合旧分段、另开新分段。权重必须确有变化，否则拒绝。"""
        if self.rolled_back:
            raise ExperimentError("实验已回滚，不能再调权")
        old = self.live_segment
        if old is None:
            raise ExperimentError("没有进行中的分段")
        new_weights = dict(policy.weights)
        if new_weights == old.weights:
            raise ExperimentError("新分段权重与当前分段相同，无需另开分段")
        # 不可变：冻结旧分段
        self.segments[-1] = Segment(
            seq=old.seq, policy_id=old.policy_id, weights=old.weights,
            rollout=old.rollout, catalog_version=old.catalog_version,
            salt=old.salt, start_ts=old.start_ts, end_ts=now,
            close_reason=f"中途调权，另开分段：{reason}",
        )
        return self._open_segment(
            policy_id=policy.id, weights=new_weights,
            rollout=policy.rollout, start_ts=now,
        )

    def rollback(self, now: str, reason: str) -> None:
        """紧急回滚：立即闭合当前分段，此后不再分流进实验。"""
        if self.rolled_back:
            return
        self.rolled_back = True
        live = self.live_segment
        if live is not None:
            idx = self.segments.index(live)
            self.segments[idx] = Segment(
                seq=live.seq, policy_id=live.policy_id, weights=live.weights,
                rollout=live.rollout, catalog_version=live.catalog_version,
                salt=live.salt, start_ts=live.start_ts, end_ts=now,
                close_reason=f"紧急回滚：{reason}",
            )

    def bucket_of(self, anon_id: str, seg: Segment) -> int:
        msg = f"{anon_id}|{self.id}|{seg.seq}".encode("utf-8")
        digest = hmac.new(seg.salt.encode("utf-8"), msg, hashlib.sha256).digest()
        return int.from_bytes(digest[:8], "big") % BUCKET_SPACE

    def snapshot(self) -> dict:
        """实验快照：连同决策日志即可离线复现任一分流。"""
        return {
            "id": self.id, "name": self.name, "salt": self.salt,
            "rolled_back": self.rolled_back,
            "segments": [s.public() for s in self.segments],
        }


class ExperimentHub:
    def __init__(self):
        self._items: Dict[str, Experiment] = {}
        self._ids = count(1)
        self.decisions: List[Decision] = []
        self._seq = count(1)

    def open(self, *, name, policy, salt, start_ts) -> Experiment:
        if policy.status != "已批准":
            raise ExperimentError("只有已批准策略才能开启实验小流量")
        eid = f"EXP{next(self._ids):04d}"
        exp = Experiment(eid, name, policy, salt, start_ts)
        self._items[eid] = exp
        return exp

    def get(self, eid: str) -> Experiment:
        if eid not in self._items:
            raise ExperimentError(f"实验不存在: {eid}")
        return self._items[eid]

    def experiments_for_policy(self, policy_id: str) -> List[Experiment]:
        return [e for e in self._items.values()
                if any(s.policy_id == policy_id for s in e.segments)]

    def rollback(self, eid: str, *, now: str, reason: str,
                 policies=None) -> None:
        exp = self.get(eid)
        exp.rollback(now, reason)
        if policies is not None:
            for seg in exp.segments:
                if seg.policy_id != BASELINE_STRATEGY:
                    policies.mark_rolled_back(seg.policy_id, now, reason)

    def assign(self, *, anon_id: str, ts: str, audience_match,
               profiling_enabled: bool, audience_resolver=None) -> Decision:
        """执行一次分流并记录决策。

        优先级：画像关闭 -> 不进任何实验；人群不匹配 -> 基线；
        实验已回滚/无生效分段 -> 基线；命中桶内 -> 实验策略，否则基线对照。
        对照组（桶外）同样记录，保证 A/B 可评估。

        audience_resolver(policy_id) 由平台层提供，按分段所依据策略判断人群；
        缺省使用外部预先算好的 audience_match。
        """
        day = ts[:10]
        seq = next(self._seq)
        chosen_exp = None
        chosen_seg = None
        bucket = None
        strategy = BASELINE_STRATEGY
        in_exp = False
        if not profiling_enabled:
            reason = "用户已关闭画像，不进入个性化实验，走基线"
        elif not audience_match:
            reason = "不在实验适用人群内，走基线"
        else:
            for exp in self._items.values():
                seg = exp.segment_at(ts)
                if seg is None:
                    continue
                if audience_resolver is not None and not audience_resolver(seg.policy_id):
                    continue
                chosen_exp, chosen_seg = exp, seg
                break
            if chosen_seg is None:
                reason = "无生效实验分段，走基线"
            else:
                bucket = chosen_exp.bucket_of(anon_id, chosen_seg)
                if bucket < int(chosen_seg.rollout * BUCKET_SPACE):
                    in_exp = True
                    strategy = chosen_seg.policy_id
                    reason = f"命中 {chosen_exp.id} 分段{chosen_seg.seq} 实验桶"
                else:
                    reason = f"进入 {chosen_exp.id} 分段{chosen_seg.seq} 对照桶（基线）"
        d = Decision(
            ts=ts, day=day, seq=seq, anon_id=anon_id,
            experiment_id=chosen_exp.id if chosen_exp else None,
            segment_seq=chosen_seg.seq if chosen_seg else None,
            bucket=bucket, strategy=strategy,
            in_experiment=in_exp, reason=reason,
        )
        self.decisions.append(d)
        return d

    def decisions_on(self, day: str) -> List[Decision]:
        return [d for d in self.decisions if d.day == day]

    def verify_reproduction(self, day: str) -> dict:
        """离线复算指定日期的每一次分流，核对策略、桶位、入组是否一致。"""
        checked = 0
        for d in self.decisions_on(day):
            if d.experiment_id is None or d.segment_seq is None:
                continue
            exp = self.get(d.experiment_id)
            seg = next(s for s in exp.segments if s.seq == d.segment_seq)
            recomputed = exp.bucket_of(d.anon_id, seg)
            assert recomputed == d.bucket, f"决策 {d.seq} 桶位不可复现"
            expect_in = recomputed < int(seg.rollout * BUCKET_SPACE)
            assert expect_in == d.in_experiment, f"决策 {d.seq} 入组结论不可复现"
            expect_strategy = seg.policy_id if expect_in else BASELINE_STRATEGY
            assert expect_strategy == d.strategy, f"决策 {d.seq} 采用策略不可复现"
            checked += 1
        return {"day": day, "decisions": len(self.decisions_on(day)),
                "recomputed": checked, "reproducible": True}
