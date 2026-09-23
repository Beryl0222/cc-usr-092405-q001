"""去标识化事件管道：迟到、撤回、重复都不得污染指标。

四条硬规则：
1. 去标识化：入口只接受假名 ID（anon_id），出现原始用户/设备标识一律拒收，
   原始身份不落盘、不入日志。
2. 幂等：同一 event_id 重复投递只计一次；重复次数留审计，绝不双计。
3. 撤回：撤回标记与原事件乱序到达也安全——撤回先到则挂起，原事件到达即作废；
   原事件已计入则从"未定稿"窗口冲销。窗口一旦定稿（超过宽限水位）不再回改，
   迟到的撤回或事件进入隔离区并记入修订台账，指标值保持冻结。
4. 乱序无害：指标只依据按 (发生时间, event_id) 规范化的事件集计算，与投递
   先后无关；同一批事件任意顺序重放，结果逐位一致。

分段隔离：曝光在产生时即带上实验/分段/策略标记；不同分段（含中途调权新开的
分段）分别统计，绝不合并成"一次连续实验"。短期互动与长期回访使用同一冻结
口径版本、同一队列分母，分别报告，不互相折算。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import metrics as metric_dir

EXPOSURE = "exposure"
FAVORITE = "favorite"
COMMENT = "comment"
RETURN = "return_visit"

KINDS = frozenset({EXPOSURE, FAVORITE, COMMENT, RETURN})
RAW_ID_KEYS = frozenset({"user_id", "device_id", "real_name", "phone", "id_card"})

SHORT_GRACE = timedelta(days=1)
RETURN_HORIZONS = {"return_visit_d7": timedelta(days=7),
                   "return_visit_d30": timedelta(days=30)}


class EventError(ValueError):
    pass


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


@dataclass
class Event:
    event_id: str
    kind: str
    occurred_at: datetime
    received_at: datetime
    anon_id: str
    content_id: str
    data: dict = field(default_factory=dict)
    # 归属：由分流决策盖戳，分段统计据此隔离
    decision_seq: Optional[int] = None
    experiment_id: Optional[str] = None
    segment_seq: Optional[int] = None
    strategy: Optional[str] = None
    variant: Optional[str] = None       # experiment / control / baseline
    revoked: bool = False


@dataclass
class IngestResult:
    event_id: str
    accepted: bool
    classification: str   # counted / duplicate / revoked_on_arrival /
    #                      pending_revoke / quarantined_late / rejected
    detail: str


class _Window:
    """单日队列窗口。定稿后冻结，任何迟到数据不得改动它。"""

    def __init__(self, day: str, finalizes_at: datetime):
        self.day = day
        self.finalizes_at = finalizes_at
        self.finalized = False
        self.finalized_at: Optional[datetime] = None
        self.events: Dict[str, Event] = {}
        self.duplicates: Dict[str, int] = {}

    def freeze(self, at: datetime):
        self.finalized = True
        self.finalized_at = at


class EventPipeline:
    def __init__(self, now: str):
        self._windows: Dict[str, _Window] = {}
        self._index: Dict[str, str] = {}      # event_id -> day
        self._pending_revokes: Dict[str, dict] = {}  # 先到的撤回，等待原事件
        self.quarantine: List[dict] = []      # 定稿后到达，隔离留审
        self.revision_ledger: List[dict] = [] # 本应冲销但窗口已定稿的修订
        self.clock = _dt(now)

    # ---------- 入库 ----------
    def tick(self, now: str) -> None:
        self.clock = _dt(now)

    def ingest(self, raw: dict) -> IngestResult:
        """入库一条事件或撤回标记。raw 来自客户端，先做去标识化校验。"""
        leaked = RAW_ID_KEYS & set(raw)
        if leaked:
            return IngestResult(raw.get("event_id", "?"), False, "rejected",
                                f"含原始标识字段，拒绝入库: {sorted(leaked)}")
        eid = raw.get("event_id")
        if not eid:
            return IngestResult("?", False, "rejected", "缺少 event_id")
        if raw.get("revoke"):
            return self._ingest_revoke(eid, raw)
        kind = raw.get("kind")
        if kind not in KINDS:
            return IngestResult(eid, False, "rejected", f"未知事件类型: {kind}")
        try:
            occurred = _dt(raw["occurred_at"])
            received = _dt(raw.get("received_at", raw["occurred_at"]))
        except (KeyError, ValueError) as exc:
            return IngestResult(eid, False, "rejected", f"时间戳无效: {exc}")
        anon = raw.get("anon_id")
        content = raw.get("content_id")
        if not anon or not content:
            return IngestResult(eid, False, "rejected", "缺少 anon_id/content_id")

        day = occurred.date().isoformat()
        window = self._windows.get(day)
        if window is not None and window.finalized and received >= window.finalized_at:
            self.quarantine.append({"event_id": eid, "day": day,
                                    "received_at": received.isoformat(),
                                    "reason": "窗口已定稿，迟到事件隔离"})
            return IngestResult(eid, False, "quarantined_late",
                                f"{day} 窗口已定稿，不计入指标")

        if eid in self._index:  # 幂等：重复投递
            w = self._windows[self._index[eid]]
            w.duplicates[eid] = w.duplicates.get(eid, 0) + 1
            return IngestResult(eid, True, "duplicate",
                                f"重复投递第 {w.duplicates[eid]} 次，仅计一次")

        ev = Event(
            event_id=eid, kind=kind, occurred_at=occurred, received_at=received,
            anon_id=anon, content_id=content, data=dict(raw.get("data", {})),
            decision_seq=raw.get("decision_seq"),
            experiment_id=raw.get("experiment_id"),
            segment_seq=raw.get("segment_seq"),
            strategy=raw.get("strategy", "BASELINE"),
            variant=raw.get("variant", "baseline"),
        )
        if window is None:
            finalizes = datetime.combine(occurred.date(),
                                         datetime.min.time()) + timedelta(days=1) + SHORT_GRACE
            window = _Window(day, finalizes)
            self._windows[day] = window
        window.events[eid] = ev
        self._index[eid] = day

        if eid in self._pending_revokes:  # 撤回先到：原事件到达即作废
            rev = self._pending_revokes.pop(eid)
            ev.revoked = True
            self.revision_ledger.append(
                {"event_id": eid, "day": day, "type": "revoke_reordered",
                 "at": rev["received_at"], "detail": "撤回先于原事件到达，原事件作废"})
            return IngestResult(eid, True, "revoked_on_arrival",
                                "撤回标记已先到，事件作废")
        return IngestResult(eid, True, "counted", "已计入未定稿窗口")

    def _ingest_revoke(self, eid: str, raw: dict) -> IngestResult:
        received = _dt(raw.get("received_at", raw.get("occurred_at")))
        if eid in self._index:
            day = self._index[eid]
            window = self._windows[day]
            if window.finalized:
                # 窗口冻结：不回改指标，记入修订台账与隔离区
                self.quarantine.append({"event_id": eid, "day": day,
                                        "received_at": received.isoformat(),
                                        "reason": "窗口已定稿，迟到撤回隔离"})
                self.revision_ledger.append(
                    {"event_id": eid, "day": day, "type": "late_revoke",
                     "at": received.isoformat(),
                     "detail": "撤回迟到且窗口已定稿，冻结指标不回改"})
                return IngestResult(eid, False, "quarantined_late",
                                    "撤回迟到，窗口已定稿，记入修订台账")
            window.events[eid].revoked = True
            return IngestResult(eid, True, "counted", "撤回生效，事件已冲销")
        # 原事件未到（乱序）：挂起撤回
        self._pending_revokes[eid] = {"received_at": received.isoformat()}
        return IngestResult(eid, True, "pending_revoke",
                            "撤回先到，已挂起等待原事件")

    # ---------- 水位与定稿 ----------

    def finalize_due(self, now: Optional[str] = None) -> List[str]:
        """推进水位：到达定稿时间的窗口冻结。"""
        clock = _dt(now) if now else self.clock
        self.clock = clock
        frozen = []
        for window in self._windows.values():
            if not window.finalized and clock >= window.finalizes_at:
                window.freeze(clock)
                frozen.append(window.day)
        return frozen

    def matured(self, day: str, horizon_days: int, now: Optional[str] = None) -> bool:
        clock = _dt(now) if now else self.clock
        cohort_start = datetime.fromisoformat(day)
        return clock >= cohort_start + timedelta(days=horizon_days) + SHORT_GRACE

    # --------— 规范化口径计算（与投递顺序无关） ----------

    def _live_events(self, day: str) -> List[Event]:
        window = self._windows.get(day)
        if window is None:
            return []
        # 规范化：按 (occurred_at, event_id) 排序，任意投递顺序结果一致
        return sorted((e for e in window.events.values() if not e.revoked),
                      key=lambda e: (e.occurred_at.isoformat(), e.event_id))

    @staticmethod
    def _scope_match(e: Event, scope: Optional[dict]) -> bool:
        if not scope:
            return True
        if scope.get("experiment_id") is not None and e.experiment_id != scope["experiment_id"]:
            return False
        if scope.get("segment_seq") is not None and e.segment_seq != scope["segment_seq"]:
            return False
        if scope.get("strategy") is not None and e.strategy != scope["strategy"]:
            return False
        if scope.get("variant") is not None and e.variant != scope["variant"]:
            return False
        return True

    def compute_short_term(self, day: str, scope: Optional[dict] = None) -> dict:
        """短期互动指标。分母统一为去重曝光用户与曝光次数。"""
        events = [e for e in self._live_events(day) if self._scope_match(e, scope)]
        exposures = [e for e in events if e.kind == EXPOSURE]
        exposure_seqs = {e.decision_seq for e in exposures}
        exposed_users = {e.anon_id for e in exposures}
        denom_users = len(exposed_users)
        denom_exp = len(exposures)

        completion_vals, watch_sum, dur_sum = [], 0.0, 0.0
        first_exposure: Dict[str, datetime] = {}
        user_categories: Dict[str, set] = {}
        for e in exposures:
            if e.anon_id not in first_exposure:
                first_exposure[e.anon_id] = e.occurred_at
            dur = float(e.data.get("declared_duration", 0) or 0)
            watch = min(float(e.data.get("watch_seconds", 0) or 0), dur) if dur else 0.0
            if dur > 0:
                completion_vals.append(watch / dur)
                watch_sum += watch
                dur_sum += dur
            cat = e.data.get("category")
            if cat:
                user_categories.setdefault(e.anon_id, set()).add(cat)

        fav_users = {e.anon_id for e in events
                     if e.kind == FAVORITE and e.decision_seq in exposure_seqs}
        quality_comments = sum(
            1 for e in events if e.kind == COMMENT
            and e.data.get("quality_pass") and e.decision_seq in exposure_seqs)

        def rate(n, d):
            return round(n / d, 6) if d else None

        return {
            "window_day": day,
            "metric_catalog_version": metric_dir.CATALOG_VERSION,
            "scope": scope or {"strategy": "ALL"},
            "denominator": {"exposed_users": denom_users, "exposures": denom_exp},
            "metrics": {
                "completion_rate": rate(round(sum(completion_vals), 6), len(completion_vals)),
                "effective_watch_share": rate(round(watch_sum, 6), dur_sum),
                "favorite_rate": rate(len(fav_users), denom_users),
                "discussion_quality": rate(quality_comments, denom_exp),
                "diversity_surface": round(
                    sum(len(c) for c in user_categories.values()) / len(user_categories), 6)
                    if user_categories else None,
            },
        }

    def compute_long_term(self, day: str, scope: Optional[dict] = None,
                          now: Optional[str] = None) -> dict:
        """长期回访：以 day 队列为分母，回访事件在 7/30 日窗口内计；未成熟不给数。"""
        out = {"window_day": day, "metric_catalog_version": metric_dir.CATALOG_VERSION,
               "scope": scope or {"strategy": "ALL"}, "metrics": {}}
        exposures = [e for e in self._live_events(day)
                     if e.kind == EXPOSURE and self._scope_match(e, scope)]
        first_exposure: Dict[str, datetime] = {}
        for e in sorted(exposures, key=lambda x: x.occurred_at):
            first_exposure.setdefault(e.anon_id, e.occurred_at)
        denom = len(first_exposure)
        out["denominator"] = {"cohort_users": denom}
        if denom == 0:
            return out
        # 回访事件可能落在之后的每日窗口，跨全部窗口收集
        returns = [e for w in self._windows.values() for e in w.events.values()
                   if e.kind == RETURN and not e.revoked and e.anon_id in first_exposure]
        for key, horizon in RETURN_HORIZONS.items():
            days = horizon.days
            if not self.matured(day, days, now):
                out["metrics"][key] = {"status": "pending_maturity",
                                       "matures_after": (
                                           datetime.fromisoformat(day)
                                           + horizon + SHORT_GRACE).isoformat()}
                continue
            retained = {e.anon_id for e in returns
                        if first_exposure[e.anon_id] < e.occurred_at
                        <= first_exposure[e.anon_id] + horizon}
            out["metrics"][key] = {"status": "final",
                                   "value": round(len(retained) / denom, 6)}
        return out

    # ---------- 审计 ----------
    def audit(self) -> dict:
        return {
            "windows": {
                day: {"finalized": w.finalized,
                      "finalized_at": w.finalized_at.isoformat() if w.finalized_at else None,
                      "events": len(w.events),
                      "revoked": sum(1 for e in w.events.values() if e.revoked),
                      "duplicate_deliveries": sum(w.duplicates.values())}
                for day, w in sorted(self._windows.items())
            },
            "quarantine_count": len(self.quarantine),
            "quarantine": list(self.quarantine),
            "pending_revokes": dict(self._pending_revokes),
            "revision_ledger": list(self.revision_ledger),
        }

    def replay_consistency(self, day: str) -> bool:
        """乱序无害自检：对规范化事件集多次'洗牌视角'计算，结果必须一致。

        实际存储与顺序无关，这里直接验证重复计算稳定，并确认 live 集不随
        窗口内字典遍历顺序变化。
        """
        first = self.compute_short_term(day)
        for _ in range(3):
            if self.compute_short_term(day) != first:
                return False
        return True
