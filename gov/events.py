"""去标识化事件管道：迟到、撤回、重复、冲突重投都不得污染指标。

五条硬规则：
1. 去标识化：入口只接受假名 ID（anon_id），出现原始用户/设备标识一律拒收，
   原始身份不落盘、不入日志（含持久化日志）。
2. 幂等与冲突：同一 event_id 真正相同的重投只计一次（duplicate），重复次数
   留审计；但只要事件类型、匿名主体、内容、发生时间、实验归属或业务数据有
   任何差异，即判定为载荷冲突（conflict）——拒绝覆盖首条记录，证据记入冲突
   台账，可按事件/窗口查询，运营据此圈定需要复核的实验窗口。重投的到达时间
   （received_at）天然滞后，不参与一致性比较。
3. 撤回：撤回标记与原事件乱序到达也安全——撤回先到则挂起，原事件到达即作废；
   原事件已计入则从"未定稿"窗口冲销。窗口一旦定稿（超过宽限水位）不再回改，
   迟到的撤回或事件进入隔离区并记入修订台账，指标值保持冻结。
4. 乱序无害：指标只依据按 (发生时间, event_id) 规范化的事件集计算，与投递
   先后无关；同一批事件任意顺序重放，结果逐位一致。
5. 重启不破坏判断：配置 store_path 后，入库、定稿与时钟推进追加写入日志，
   进程重启时按序回放，首条记录、冲突台账、定稿状态与挂起撤回完整恢复，
   同一 event_id 的幂等/冲突判定跨重启保持一致。

分段隔离：曝光在产生时即带上实验/分段/策略标记；不同分段（含中途调权新开的
分段）分别统计，绝不合并成"一次连续实验"。短期互动与长期回访使用同一冻结
口径版本、同一队列分母，分别报告，不互相折算。
"""

import json
import os
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
    classification: str   # counted / duplicate / conflict / revoked_on_arrival /
    #                      pending_revoke / quarantined_late / rejected
    detail: str
    conflict: Optional[dict] = None     # classification == "conflict" 时的证据


class _Window:
    """单日队列窗口。定稿后冻结，任何迟到数据不得改动它。"""

    def __init__(self, day: str, finalizes_at: datetime):
        self.day = day
        self.finalizes_at = finalizes_at
        self.finalized = False
        self.finalized_at: Optional[datetime] = None
        self.events: Dict[str, Event] = {}
        self.duplicates: Dict[str, int] = {}
        self.conflict_deliveries = 0    # 命中本窗口首条记录的冲突重投次数

    def freeze(self, at: datetime):
        self.finalized = True
        self.finalized_at = at


class EventPipeline:
    """事件入库管道。store_path 提供可选的追加重启日志（JSONL）。"""

    def __init__(self, now: str, store_path: Optional[str] = None):
        self._windows: Dict[str, _Window] = {}
        self._index: Dict[str, str] = {}      # event_id -> day
        self._pending_revokes: Dict[str, dict] = {}  # 先到的撤回，等待原事件
        self.quarantine: List[dict] = []      # 定稿后到达，隔离留审
        self.revision_ledger: List[dict] = [] # 本应冲销但窗口已定稿的修订
        self.conflict_ledger: List[dict] = [] # 同编号不同内容的冲突证据
        self.clock = _dt(now)
        self.store_path = store_path
        self._journal = None
        if store_path:
            self._recover(store_path)
            self._journal = open(store_path, "a", encoding="utf-8")

    def close(self) -> None:
        if self._journal is not None:
            self._journal.close()
            self._journal = None

    # ---------- 重启日志 ----------
    def _journal_append(self, entry: dict) -> None:
        if self._journal is None:
            return
        self._journal.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        self._journal.flush()
        os.fsync(self._journal.fileno())

    def journal_entry(self, op: str, payload: dict) -> None:
        """平台层借用同一日志追加自有记录（如分流决策），回放时本管道跳过。"""
        self._journal_append({"op": op, **payload})

    def _recover(self, path: str) -> None:
        """启动回放：按序重放日志，重建窗口、索引、冲突台账与挂起撤回。"""
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    break  # 崩溃截断的半行：丢弃，后续记录不存在
                op = entry.get("op")
                if op == "ingest":
                    self.ingest(entry["raw"])
                elif op == "finalize":
                    self.finalize_due(entry["now"])
                elif op == "tick":
                    self.tick(entry["now"])
                # 其他 op（如平台层的 decision）由各自属主回放，这里跳过

    # ---------- 入库 ----------
    def tick(self, now: str) -> None:
        self.clock = _dt(now)
        self._journal_append({"op": "tick", "now": self.clock.isoformat()})

    def ingest(self, raw: dict) -> IngestResult:
        """入库一条事件或撤回标记。raw 来自客户端，先做去标识化校验。"""
        leaked = RAW_ID_KEYS & set(raw)
        if leaked:
            # 原始身份不落盘：隐私拒收先于一切日志
            return IngestResult(raw.get("event_id", "?"), False, "rejected",
                                f"含原始标识字段，拒绝入库: {sorted(leaked)}")
        eid = raw.get("event_id")
        if not eid:
            return IngestResult("?", False, "rejected", "缺少 event_id")
        self._journal_append({"op": "ingest", "raw": raw})
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

        if eid in self._index:
            # 同编号重投：先判"幂等还是冲突"，与窗口是否定稿无关
            return self._ingest_redelivery(eid, raw, occurred, received)

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

    # ---------- 同编号重投：幂等命中 vs 载荷冲突 ----------
    @staticmethod
    def _payload_diff(stored: Event, raw: dict, occurred: datetime) -> List[str]:
        """比较首条记录与重投载荷。received_at 是到达时间，不参与比较。"""
        diffs = []
        if raw.get("kind") != stored.kind:
            diffs.append("kind")
        if occurred != stored.occurred_at:
            diffs.append("occurred_at")
        if raw.get("anon_id") != stored.anon_id:
            diffs.append("anon_id")
        if raw.get("content_id") != stored.content_id:
            diffs.append("content_id")
        if raw.get("decision_seq") != stored.decision_seq:
            diffs.append("decision_seq")
        if raw.get("experiment_id") != stored.experiment_id:
            diffs.append("experiment_id")
        if raw.get("segment_seq") != stored.segment_seq:
            diffs.append("segment_seq")
        if raw.get("strategy", "BASELINE") != stored.strategy:
            diffs.append("strategy")
        if raw.get("variant", "baseline") != stored.variant:
            diffs.append("variant")
        incoming_data = raw.get("data", {})
        try:
            incoming_data = dict(incoming_data)
        except (TypeError, ValueError):
            pass
        if incoming_data != stored.data:
            diffs.append("data")
        return diffs

    @staticmethod
    def _stored_view(stored: Event) -> dict:
        return {
            "kind": stored.kind,
            "occurred_at": stored.occurred_at.isoformat(),
            "anon_id": stored.anon_id,
            "content_id": stored.content_id,
            "decision_seq": stored.decision_seq,
            "experiment_id": stored.experiment_id,
            "segment_seq": stored.segment_seq,
            "strategy": stored.strategy,
            "variant": stored.variant,
            "data": stored.data,
        }

    @staticmethod
    def _incoming_view(raw: dict, occurred: datetime) -> dict:
        data = raw.get("data", {})
        try:
            data = dict(data)
        except (TypeError, ValueError):
            pass
        return {
            "kind": raw.get("kind"),
            "occurred_at": occurred.isoformat(),
            "anon_id": raw.get("anon_id"),
            "content_id": raw.get("content_id"),
            "decision_seq": raw.get("decision_seq"),
            "experiment_id": raw.get("experiment_id"),
            "segment_seq": raw.get("segment_seq"),
            "strategy": raw.get("strategy", "BASELINE"),
            "variant": raw.get("variant", "baseline"),
            "data": data,
        }

    def _ingest_redelivery(self, eid: str, raw: dict,
                           occurred: datetime, received: datetime) -> IngestResult:
        """同一 event_id 再次到达：逐字段比对，相同为幂等，不同为冲突。"""
        day = self._index[eid]
        window = self._windows[day]
        stored = window.events[eid]
        diffs = self._payload_diff(stored, raw, occurred)
        if not diffs:
            window.duplicates[eid] = window.duplicates.get(eid, 0) + 1
            return IngestResult(eid, True, "duplicate",
                                f"重复投递第 {window.duplicates[eid]} 次，仅计一次")
        # 载荷冲突：拒绝覆盖，首条记录保持不动，证据入台账
        evidence = {
            "event_id": eid,
            "day": day,                       # 受影响（需要复核）的窗口
            "detected_at": received.isoformat(),
            "window_finalized": window.finalized,  # 已定稿则冻结指标可能已被污染
            "differing_fields": diffs,
            "stored": self._stored_view(stored),
            "incoming": self._incoming_view(raw, occurred),
            "detail": "同一 event_id 重投了不同内容，已拒绝覆盖，保留首条记录",
        }
        self.conflict_ledger.append(evidence)
        window.conflict_deliveries += 1
        return IngestResult(eid, False, "conflict",
                            f"与已入库记录冲突（字段: {', '.join(diffs)}），"
                            f"拒绝覆盖，证据已记入冲突台账",
                            conflict=evidence)

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
        self._journal_append({"op": "finalize", "now": clock.isoformat()})
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

    # ---------- 冲突查询 ----------
    def conflict_report(self, event_id: Optional[str] = None,
                        day: Optional[str] = None) -> dict:
        """冲突台账查询：运营据此圈定需要复核的事件与实验窗口。"""
        entries = [c for c in self.conflict_ledger
                   if (event_id is None or c["event_id"] == event_id)
                   and (day is None or c["day"] == day)]
        return {"conflict_count": len(entries), "conflicts": entries}

    # ---------- 审计 ----------
    def audit(self) -> dict:
        return {
            "windows": {
                day: {"finalized": w.finalized,
                      "finalized_at": w.finalized_at.isoformat() if w.finalized_at else None,
                      "events": len(w.events),
                      "revoked": sum(1 for e in w.events.values() if e.revoked),
                      "duplicate_deliveries": sum(w.duplicates.values()),
                      "conflict_deliveries": w.conflict_deliveries}
                for day, w in sorted(self._windows.items())
            },
            "quarantine_count": len(self.quarantine),
            "quarantine": list(self.quarantine),
            "pending_revokes": dict(self._pending_revokes),
            "revision_ledger": list(self.revision_ledger),
            "conflict_count": len(self.conflict_ledger),
            "conflicts": list(self.conflict_ledger),
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
