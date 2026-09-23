"""推荐策略提案与审批状态机。

规则（来自编辑部治理约定）：
- 产品人员提交：目标、权重、适用人群、有效期；四项缺一不可。
- 必须经"内容负责人"与"风险负责人"双方批准，才能进入小流量分发。
- 小流量有硬性上限（rollout_cap），超过即拒绝，防茧房与误伤扩大。
- 批准后策略不可就地修改；要调权重必须新建策略并另开实验分段
  （见 experiments.py），不得伪装成一次连续实验。
- 任何时候可被紧急回滚（见 experiments.rollback），回滚有据可查。

状态：草拟 -> 待批准 -> 已批准 -> 已归档；批准前可撤回；已批准可回滚。
"""

from dataclasses import dataclass, field
from itertools import count
from typing import Dict, List, Optional

from . import metrics as metric_dir

DRAFT = "草拟"
PENDING = "待批准"
APPROVED = "已批准"
REJECTED = "已拒绝"
ROLLED_BACK = "已回滚"
ARCHIVED = "已结束"

CONTENT_OWNER = "内容负责人"
RISK_OWNER = "风险负责人"
REQUIRED_APPROVERS = frozenset({CONTENT_OWNER, RISK_OWNER})

ROLLOUT_CAP = 0.10  # 小流量硬上限：10%

_VALID_TRANSITIONS = {
    DRAFT: {PENDING},
    PENDING: {APPROVED, REJECTED, DRAFT},
    APPROVED: {ROLLED_BACK, ARCHIVED},
    REJECTED: {DRAFT},
    ROLLED_BACK: {ARCHIVED},
    ARCHIVED: set(),
}


class PolicyError(ValueError):
    pass


@dataclass
class Approval:
    role: str
    approver: str
    reason: str
    decided_at: str


@dataclass
class Policy:
    id: str
    title: str
    goal: str                       # 目标：要解决什么（如"纠正完播率单一指标"）
    weights: Dict[str, float]       # 目标权重，键来自冻结指标目录
    audience: dict                  # 适用人群：{"include": {...}, "exclude": {...}}
    effective_from: str             # ISO 时间，有效期起
    effective_to: str               # ISO 时间，有效期止
    owner: str                      # 提交的产品人员
    rollout: float                  # 小流量比例，<= ROLLOUT_CAP
    status: str = DRAFT
    catalog_version: str = metric_dir.CATALOG_VERSION
    approvals: List[Approval] = field(default_factory=list)
    timeline: List[dict] = field(default_factory=list)
    reject_reason: Optional[str] = None

    def public(self) -> dict:
        return {
            "id": self.id, "title": self.title, "goal": self.goal,
            "weights": dict(self.weights),
            "audience": self.audience,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "owner": self.owner, "rollout": self.rollout,
            "status": self.status,
            "catalog_version": self.catalog_version,
            "approvals": [vars(a) for a in self.approvals],
            "timeline": list(self.timeline),
            "reject_reason": self.reject_reason,
        }


class PolicyRegistry:
    def __init__(self):
        self._items: Dict[str, Policy] = {}
        self._ids = count(1)

    def _transition(self, p: Policy, to: str) -> None:
        if to not in _VALID_TRANSITIONS[p.status]:
            raise PolicyError(f"策略 {p.id} 不能从 {p.status} 转为 {to}")
        p.status = to

    def submit(self, *, title, goal, weights, audience,
               effective_from, effective_to, owner, rollout, now) -> Policy:
        """产品人员提交提案。校验四项要素、权重口径、人群与有效期、流量上限。"""
        missing = [k for k, v in {
            "goal": goal, "weights": weights, "audience": audience,
            "effective_from": effective_from, "effective_to": effective_to,
        }.items() if not v]
        if missing:
            raise PolicyError(f"策略提案缺少必填要素: {missing}")
        if effective_to <= effective_from:
            raise PolicyError("有效期止必须晚于有效期起")
        metric_dir.validate_weights(weights)  # 口径校验，拒绝自创指标
        if not isinstance(audience, dict) or "include" not in audience:
            raise PolicyError("适用人群必须包含 include 条件")
        if not isinstance(rollout, (int, float)) or not (0 < rollout <= ROLLOUT_CAP):
            raise PolicyError(f"小流量比例必须在 (0, {ROLLOUT_CAP}] 之间")
        pid = f"PL{next(self._ids):04d}"
        p = Policy(
            id=pid, title=title or pid, goal=goal, weights=dict(weights),
            audience=audience, effective_from=effective_from,
            effective_to=effective_to, owner=owner, rollout=float(rollout),
        )
        p.timeline.append({"at": now, "event": "创建草稿", "by": owner})
        self._items[pid] = p
        return p

    def send_for_approval(self, pid: str, now: str) -> Policy:
        p = self._get(pid)
        self._transition(p, PENDING)
        p.timeline.append({"at": now, "event": "提交审批"})
        return p

    def approve(self, pid: str, *, role, approver, reason, now) -> Policy:
        """内容负责人与风险负责人分别批准；两人都批准才生效。"""
        p = self._get(pid)
        if role not in REQUIRED_APPROVERS:
            raise PolicyError(f"无权审批角色: {role}；需要 {sorted(REQUIRED_APPROVERS)}")
        if p.status != PENDING:
            raise PolicyError(f"仅'待批准'策略可审批，当前 {p.status}")
        if any(a.role == role for a in p.approvals):
            raise PolicyError(f"{role} 已批准，不可重复批准")
        p.approvals.append(Approval(role, approver, reason, now))
        p.timeline.append({"at": now, "event": f"{role}批准", "by": approver})
        if REQUIRED_APPROVERS <= {a.role for a in p.approvals}:
            self._transition(p, APPROVED)
            p.timeline.append({"at": now, "event": "双批准生效，允许小流量分发"})
        return p

    def reject(self, pid: str, *, role, approver, reason, now) -> Policy:
        p = self._get(pid)
        if role not in REQUIRED_APPROVERS:
            raise PolicyError(f"无权审批角色: {role}")
        if p.status != PENDING:
            raise PolicyError(f"仅'待批准'策略可驳回，当前 {p.status}")
        self._transition(p, REJECTED)
        p.reject_reason = f"[{role}/{approver}] {reason}"
        p.timeline.append({"at": now, "event": "驳回", "by": approver, "detail": reason})
        return p

    def mark_rolled_back(self, pid: str, now: str, reason: str) -> None:
        p = self._get(pid)
        if p.status == ROLLED_BACK:
            return
        self._transition(p, ROLLED_BACK)
        p.timeline.append({"at": now, "event": "紧急回滚", "detail": reason})

    def get(self, pid: str) -> Policy:
        return self._get(pid)

    def _get(self, pid: str) -> Policy:
        if pid not in self._items:
            raise PolicyError(f"策略不存在: {pid}")
        return self._items[pid]

    def active_for(self, *, now: str) -> List[Policy]:
        """当前时间窗口内已批准（未回滚）的策略。"""
        return [p for p in self._items.values()
                if p.status == APPROVED
                and p.effective_from <= now < p.effective_to]

    def list(self) -> List[Policy]:
        return list(self._items.values())
