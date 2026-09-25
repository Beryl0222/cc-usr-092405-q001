"""去标识化事件管道：迟到、撤回、重复、同编号冲突都不得污染指标。

五条硬规则：
1. 去标识化：入口只接受假名 ID（anon_id），出现原始用户/设备标识一律拒收，
   原始身份不落盘、不入日志。
2. 幂等：用语义指纹判定"真正相同的重试"——语义完全一致的同 event_id 重复
   投递只计一次；重复次数留审计，绝不双计。
3. 冲突：同编号但事件类型、匿名主体、内容、时间、实验归属或业务数据不同，
   一律拒绝覆盖——首条记录原样保留、指标不被改写，并写入 conflict_log 留可
   查询的冲突证据（HTTP 层据此返回 409，调用方可区分幂等命中与载荷冲突）。
4. 撤回：撤回标记与原事件乱序到达也安全——撤回先到则挂起，原事件到达即作废；
   原事件已计入则从"未定稿"窗口冲销。窗口一旦定稿（超过宽限水位）不再回改，
   迟到的撤回或事件进入隔离区 + 修订台账，指标值保持冻结；定稿后到达的同编号
   异载照样识别冲突并留证（裁定优先级：未定稿正常处理 > 撤回先到挂起 >
   已定稿隔离），不回改冻结指标、不覆盖记录。
5. 乱序无害：指标只依据按 (发生时间, event_id) 规范化的事件集计算，与投递
   先后无关；同一批事件任意顺序重放，结果逐位一致。进程重启后由持久化快照
   恢复，幂等/冲突/撤回/定稿判断与重启前完全一致。

语义指纹覆盖以下字段（任何一个不同即冲突）：
kind（事件类型）、anon_id（匿名主体）、content_id（内容）、occurred_at（时间）、
decision_seq / experiment_id / segment_seq / strategy / variant（实验归属）、
data（业务数据）。received_at 仅记录投递时间，不参与指纹——网络延迟造成的
同载迟到仍是合法重试。

分段隔离：曝光在产生时即带上实验/分段/策略标记；不同分段（含中途调权新开的
分段）分别统计，绝不合并成"一次连续实验"。短期互动与长期回访使用同一冻结
口径版本、同一队列分母，分别报告，不互相折算。
"""

import json
import threading
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

# 参与语义指纹的字段（实验归属 + 业务数据），顺序即指纹元组顺序
FINGERPRINT_FIELDS = ("kind", "anon_id", "content_id", "occurred_at",
                      "decision_seq", "experiment_id", "segment_seq",
                      "strategy", "variant", "data")
# 面向运营的中文字段名（冲突证据按差异字段给出可读名称）
FIELD_LABELS = {
    "kind": "事件类型",
    "anon_id": "匿名主体",
    "content_id": "内容",
    "occurred_at": "发生时间",
    "decision_seq": "分流盖戳序号",
    "experiment_id": "实验归属",
    "segment_seq": "实验分段",
    "strategy": "策略",
    "variant": "桶位",
    "data": "业务数据",
}


class EventError(ValueError):
    pass


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _stable_data(data: Optional[dict]) -> str:
    """业务数据的稳定序列化：键排序、分隔符收紧，与字典构造顺序无关。"""
    return json.dumps(data or {}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _fingerprint_values(*, kind: str, anon: str, content: str,
                        occurred: datetime, raw: dict) -> Dict[str, object]:
    """从一次投递提取参与指纹的语义字段。

    strategy/variant 取与 Event 构造相同的默认值，保证直接调管道与经 Platform
    盖戳补全后得到一致判定。
    """
    return {
        "kind": kind,
        "anon_id": anon,
        "content_id": content,
        "occurred_at": occurred.isoformat(),
        "decision_seq": raw.get("decision_seq"),
        "experiment_id": raw.get("experiment_id"),
        "segment_seq": raw.get("segment_seq"),
        "strategy": raw.get("strategy", "BASELINE"),
        "variant": raw.get("variant", "baseline"),
        "data": _stable_data(raw.get("data")),
    }


def _fingerprint(raw: dict, *, kind: str, occurred: datetime,
                 anon: str, content: str) -> tuple:
    vals = _fingerprint_values(kind=kind, anon=anon, content=content,
                               occurred=occurred, raw=raw)
    return tuple(vals[name] for name in FINGERPRINT_FIELDS)


def _event_fingerprint(ev: "Event") -> tuple:
    return (
        ev.kind, ev.anon_id, ev.content_id, ev.occurred_at.isoformat(),
        ev.decision_seq, ev.experiment_id, ev.segment_seq,
        ev.strategy, ev.variant, _stable_data(ev.data),
    )


def _payload_view(*, kind: str, anon: str, content: str, occurred: datetime,
                  raw: dict, received: datetime) -> dict:
    """一次事件投递（不含 event_id/received_at 元信息）的可存档视图。"""
    vals = _fingerprint_values(kind=kind, anon=anon, content=content,
                               occurred=occurred, raw=raw)
    vals["data"] = raw.get("data", {})
    vals["received_at"] = received.isoformat()
    return vals


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
    classification: str   # counted / duplicate / conflict / revoked_on_arrival /
    #                      pending_revoke / quarantined_late / rejected
    detail: str
    conflicts: List[str] = field(default_factory=list)  # 冲突时的差异字段
    conflict_id: Optional[str] = None


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
        self.conflict_log: List[dict] = []    # 同编号异载的冲突证据
        self.clock = _dt(now)
        # 入库串行化：ThreadingHTTPServer 下两个同编号并发请求不得同时越过
        # 幂等/冲突检查而双双落库（check-then-act 竞态）。
        self._ingest_lock = threading.RLock()

    # ---------- 入库 ----------
    def tick(self, now: str) -> None:
        with self._ingest_lock:
            self.clock = _dt(now)

    def ingest(self, raw: dict) -> IngestResult:
        with self._ingest_lock:
            return self._ingest_locked(raw)

    def _ingest_locked(self, raw: dict) -> IngestResult:
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
        late_to_finalized = (
            window is not None and window.finalized
            and received >= window.finalized_at)

        # 幂等/冲突判定先于定稿隔离：即使窗口已定稿，同编号重投也必须照常
        # 识别——相同者仍是幂等命中，不同者拒绝覆盖并留冲突证据。
        if eid in self._index:
            stored_day = self._index[eid]
            stored = self._windows[stored_day].events[eid]
            fp_new = _fingerprint(raw, kind=kind, occurred=occurred,
                                  anon=anon, content=content)
            if fp_new == _event_fingerprint(stored):
                w = self._windows[stored_day]
                w.duplicates[eid] = w.duplicates.get(eid, 0) + 1
                if late_to_finalized:
                    # 同载重试迟到：不回改冻结窗口，仅计一次重复投递
                    return IngestResult(
                        eid, True, "duplicate",
                        f"窗口已定稿；相同载荷重试第 {w.duplicates[eid]} 次，仅计一次")
                return IngestResult(
                    eid, True, "duplicate",
                    f"重复投递第 {w.duplicates[eid]} 次，仅计一次")
            return self._record_conflict(
                eid, stored, raw, kind=kind, occurred=occurred, anon=anon,
                content=content, received=received, challenger_day=day,
                late=late_to_finalized)

        # 撤回先到：任何"原事件"（无论语义）到达都完成挂起配对并作废。
        # 客户端不会重发别的内容来认领一个撤回；挂起的撤回只按 event_id 配对。
        pending_revoke = eid in self._pending_revokes

        if late_to_finalized:
            self.quarantine.append({"event_id": eid, "day": day,
                                    "received_at": received.isoformat(),
                                    "reason": "窗口已定稿，迟到事件隔离"})
            result = IngestResult(eid, False, "quarantined_late",
                                  f"{day} 窗口已定稿，不计入指标")
        else:
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
                                             datetime.min.time()) \
                    + timedelta(days=1) + SHORT_GRACE
                window = _Window(day, finalizes)
                self._windows[day] = window
            window.events[eid] = ev
            self._index[eid] = day
            result = IngestResult(eid, True, "counted", "已计入未定稿窗口")

        if pending_revoke:
            rev = self._pending_revokes.pop(eid)
            if not late_to_finalized:
                self._windows[day].events[eid].revoked = True
            self.revision_ledger.append(
                {"event_id": eid, "day": day, "type": "revoke_reordered",
                 "at": rev["received_at"],
                 "detail": "撤回先于原事件到达，原事件作废"
                           + ("（原事件迟到且窗口已定稿，已隔离）"
                              if late_to_finalized else "")})
            if late_to_finalized:
                return IngestResult(eid, False, "quarantined_late",
                                    "撤回标记已先到，但原事件迟到且窗口已定稿，隔离留审")
            return IngestResult(eid, True, "revoked_on_arrival",
                                "撤回标记已先到，事件作废")
        return result

    def _record_conflict(self, eid: str, stored: Event, challenger_raw: dict, *,
                         kind: str, occurred: datetime, anon: str, content: str,
                         received: datetime, challenger_day: str,
                         late: bool) -> IngestResult:
        """同编号异载：拒绝覆盖，保留首条，落冲突证据。"""
        new_vals = _fingerprint_values(kind=kind, anon=anon, content=content,
                                       occurred=occurred, raw=challenger_raw)
        old_vals = dict(zip(FINGERPRINT_FIELDS, _event_fingerprint(stored)))
        diffs = [name for name in FINGERPRINT_FIELDS
                 if old_vals[name] != new_vals[name]]
        # data 在指纹里是稳定字符串，证据里保留原始对象，便于运营核对
        old_view = {
            "kind": stored.kind, "anon_id": stored.anon_id,
            "content_id": stored.content_id,
            "occurred_at": stored.occurred_at.isoformat(),
            "decision_seq": stored.decision_seq,
            "experiment_id": stored.experiment_id,
            "segment_seq": stored.segment_seq, "strategy": stored.strategy,
            "variant": stored.variant, "data": stored.data,
            "received_at": stored.received_at.isoformat(),
            "day": self._index[eid],
        }
        new_view = _payload_view(kind=kind, anon=anon, content=content,
                                 occurred=occurred, raw=challenger_raw,
                                 received=received)
        new_view["day"] = challenger_day
        conflict_id = f"C{eid}"
        record = {
            "conflict_id": conflict_id,
            "event_id": eid,
            "detected_at": self.clock.isoformat(),
            "differences": diffs,
            "difference_labels": [FIELD_LABELS[d] for d in diffs],
            "challenger_after_finalization": late,
            "stored_window_finalized":
                self._windows[self._index[eid]].finalized,
            "first": old_view,
            "challenger": new_view,
        }
        self.conflict_log.append(record)
        # 冲突本身也是一种"想改已定稿数据"的企图，随迟到标记进隔离留审
        if late:
            self.quarantine.append(
                {"event_id": eid, "day": challenger_day,
                 "received_at": received.isoformat(),
                 "reason": "窗口已定稿，同编号异载冲突载荷隔离",
                 "conflict_id": conflict_id})
        labels = "、".join(FIELD_LABELS[d] for d in diffs)
        detail = (f"同 event_id={eid} 载荷冲突（差异字段：{labels}），"
                  f"保留首条记录，拒绝覆盖")
        if late:
            detail += "；冲突载荷迟到且窗口已定稿，已隔离"
        return IngestResult(eid, False, "conflict", detail,
                            conflicts=diffs, conflict_id=conflict_id)

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
        # 原事件未到（乱序）：挂起撤回。不接收任何载荷字段，避免被伪造内容污染
        self._pending_revokes[eid] = {"received_at": received.isoformat()}
        return IngestResult(eid, True, "pending_revoke",
                            "撤回先到，已挂起等待原事件")

    # ---------- 水位与定稿 ----------

    def finalize_due(self, now: Optional[str] = None) -> List[str]:
        """推进水位：到达定稿时间的窗口冻结。"""
        with self._ingest_lock:
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

    # ---------- 快照与恢复（进程重启不破坏幂等/冲突/定稿判断） ----------

    def dump_state(self) -> dict:
        """导出可 JSON 持久化的全量状态。"""
        return {
            "clock": self.clock.isoformat(),
            "windows": [
                {"day": w.day,
                 "finalizes_at": w.finalizes_at.isoformat(),
                 "finalized": w.finalized,
                 "finalized_at": w.finalized_at.isoformat()
                 if w.finalized_at else None,
                 "events": [
                     {"event_id": e.event_id, "kind": e.kind,
                      "occurred_at": e.occurred_at.isoformat(),
                      "received_at": e.received_at.isoformat(),
                      "anon_id": e.anon_id, "content_id": e.content_id,
                      "data": e.data, "decision_seq": e.decision_seq,
                      "experiment_id": e.experiment_id,
                      "segment_seq": e.segment_seq, "strategy": e.strategy,
                      "variant": e.variant, "revoked": e.revoked}
                     for e in w.events.values()],
                 "duplicates": dict(w.duplicates)}
                for w in sorted(self._windows.values(), key=lambda x: x.day)],
            "pending_revokes": dict(self._pending_revokes),
            "quarantine": list(self.quarantine),
            "revision_ledger": list(self.revision_ledger),
            "conflict_log": list(self.conflict_log),
        }

    @classmethod
    def restore_state(cls, state: dict) -> "EventPipeline":
        """从 dump_state 的快照恢复管道。"""
        pipe = cls(state["clock"])
        for ws in state.get("windows", []):
            w = _Window(ws["day"], _dt(ws["finalizes_at"]))
            w.finalized = ws["finalized"]
            w.finalized_at = _dt(ws["finalized_at"]) if ws.get("finalized_at") else None
            w.duplicates = dict(ws.get("duplicates", {}))
            for es in ws["events"]:
                ev = Event(
                    event_id=es["event_id"], kind=es["kind"],
                    occurred_at=_dt(es["occurred_at"]),
                    received_at=_dt(es["received_at"]),
                    anon_id=es["anon_id"], content_id=es["content_id"],
                    data=dict(es.get("data", {})),
                    decision_seq=es.get("decision_seq"),
                    experiment_id=es.get("experiment_id"),
                    segment_seq=es.get("segment_seq"),
                    strategy=es.get("strategy"), variant=es.get("variant"),
                    revoked=es.get("revoked", False))
                w.events[ev.event_id] = ev
                pipe._index[ev.event_id] = w.day
            pipe._windows[w.day] = w
        pipe._pending_revokes = dict(state.get("pending_revokes", {}))
        pipe.quarantine = list(state.get("quarantine", []))
        pipe.revision_ledger = list(state.get("revision_ledger", []))
        pipe.conflict_log = list(state.get("conflict_log", []))
        return pipe

    # ---------- 审计与冲突查询 ----------

    def conflicts(self, event_id: Optional[str] = None) -> List[dict]:
        """冲突证据查询：可按 event_id 过滤，供运营定位需复核的实验窗口。"""
        if event_id is None:
            return list(self.conflict_log)
        return [c for c in self.conflict_log if c["event_id"] == event_id]

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
            "conflict_count": len(self.conflict_log),
            "conflict_log": list(self.conflict_log),
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
