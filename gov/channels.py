"""创作者独立通道解释与 k 匿名聚合。

- 创作者应知道自己的内容"为何进入或退出独立通道"：每次进出都登记
  可读原因、触发信号、依据策略/实验分段与时间，创作者凭内容可查。
- 任何角色不得反推出个人观看记录：对外报表只发布 k 匿名聚合；
  分组人数低于 k 的格子被抑制（suppressed），而不是给出 0 或 1 这样可
  定位个人的值。抑制记录留审计但不暴露个体。
"""

from dataclasses import dataclass, field
from itertools import count
from typing import Dict, List, Optional

DEFAULT_K = 5


class ChannelError(ValueError):
    pass


@dataclass
class ChannelRecord:
    content_id: str
    creator_id: str
    in_channel: bool
    reason: str
    signals: dict
    basis: str            # 策略 id / 实验分段 / 人工规则
    at: str
    seq: int

    def public(self) -> dict:
        return vars(self)


class ChannelRegistry:
    def __init__(self, channel_name: str = "文化长内容独立通道"):
        self.channel_name = channel_name
        self._records: List[ChannelRecord] = []
        self._current: Dict[str, ChannelRecord] = {}  # content_id -> 最新状态
        self._seq = count(1)

    def _latest(self, content_id: str) -> Optional[ChannelRecord]:
        return self._current.get(content_id)

    def enter(self, *, content_id, creator_id, reason, signals, basis, now) -> ChannelRecord:
        latest = self._latest(content_id)
        if latest is not None and latest.in_channel:
            raise ChannelError(f"内容 {content_id} 已在独立通道内，无需重复进入")
        rec = ChannelRecord(content_id, creator_id, True, reason,
                            dict(signals), basis, now, next(self._seq))
        self._records.append(rec)
        self._current[content_id] = rec
        return rec

    def exit(self, *, content_id, reason, signals, basis, now) -> ChannelRecord:
        latest = self._latest(content_id)
        if latest is None or not latest.in_channel:
            raise ChannelError(f"内容 {content_id} 不在独立通道内，无法退出")
        rec = ChannelRecord(latest.content_id, latest.creator_id, False, reason,
                            dict(signals), basis, now, next(self._seq))
        self._records.append(rec)
        self._current[content_id] = rec
        return rec

    def explain(self, content_id: str) -> dict:
        """创作者查询：当前是否在通道内 + 完整进出原因链。"""
        history = [r.public() for r in self._records if r.content_id == content_id]
        if not history:
            raise ChannelError(f"内容 {content_id} 无通道记录")
        return {
            "channel": self.channel_name,
            "content_id": content_id,
            "currently_in_channel": self._current[content_id].in_channel,
            "current_reason": self._current[content_id].reason,
            "history": history,
        }

    def for_creator(self, creator_id: str) -> List[dict]:
        return [r.public() for r in self._records
                if r.creator_id == creator_id and r.seq ==
                max(x.seq for x in self._records if x.content_id == r.content_id)]


def kanonymize(groups: Dict[str, dict], k: int = DEFAULT_K) -> dict:
    """对分组聚合做 k 匿名。

    groups: {分组键: {"users": 人数, "metrics": {...}}}
    人数 < k 的格子被抑制；输出只含 released 与被抑制格子的键名清单，
    不泄露被抑制格子的任何数值。
    """
    released, suppressed = {}, []
    for key, payload in sorted(groups.items()):
        users = payload.get("users", 0)
        if users < k:
            suppressed.append(key)
        else:
            released[key] = {"users": users, "metrics": payload.get("metrics", {})}
    return {
        "k": k,
        "released": released,
        "suppressed_groups": suppressed,
        "note": f"人数低于 {k} 的分组被抑制，以防反推出个人记录",
    }
