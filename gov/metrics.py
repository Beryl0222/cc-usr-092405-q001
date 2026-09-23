"""冻结的指标口径目录。

背景：完播率曾作为唯一核心指标，导致数小时的知识讲解、非遗慢直播和
经典课文影像几乎没有曝光。治理后以多信号组合评估，但每一次实验对比
只能使用同一版本的口径（metric_catalog_version），口径变更必须升版本，
禁止在一次对比中偷换。

指标分两类：
- 短期互动：曝光后短窗内发生的行为（完播、收藏、评论质量）。
- 长期回访：跨会话的回访行为，需要更长的观察窗。
长内容（如慢直播）不会因"时长短"在完播口径上吃亏——完播率按内容自身
声明的时长归一化，且长内容另有"有效观看时长占比"口径。
"""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    horizon: str          # "short_term" 或 "long_term"
    version: str
    formula: str
    note: str


CATALOG_VERSION = "v2.0"

# 权重键必须来自此目录，策略提案引用键而不是自定义名称。
METRICS: Tuple[Metric, ...] = (
    Metric(
        key="completion_rate",
        name="完播率（按时长归一化）",
        horizon="short_term",
        version=CATALOG_VERSION,
        formula="有效观看时长 / 内容自身声明时长",
        note="不按绝对秒数比较，长内容与短视频在同一归一化口径下评估。",
    ),
    Metric(
        key="effective_watch_share",
        name="有效观看时长占比",
        horizon="short_term",
        version=CATALOG_VERSION,
        formula="有效观看时长 / 内容时长；慢直播按其声明的完整时段计",
        note="保护知识长讲解、非遗慢直播等长内容。",
    ),
    Metric(
        key="favorite_rate",
        name="收藏率",
        horizon="short_term",
        version=CATALOG_VERSION,
        formula="去重收藏用户数 / 去重曝光用户数",
        note="代表'以后再看'的长期意图，补齐完播率的盲区。",
    ),
    Metric(
        key="discussion_quality",
        name="讨论质量分",
        horizon="short_term",
        version=CATALOG_VERSION,
        formula="通过质量模型的评论数 / 曝光数（模型版本另行登记）",
        note="只计有效讨论，不计刷量与攻击言论。",
    ),
    Metric(
        key="diversity_surface",
        name="多样性展现",
        horizon="short_term",
        version=CATALOG_VERSION,
        formula="单次会话内内容品类的去重数及跨品类覆盖",
        note="用于抑制兴趣茧房；是约束项而非单纯越高越好。",
    ),
    Metric(
        key="return_visit_d7",
        name="7 日回访率",
        horizon="long_term",
        version=CATALOG_VERSION,
        formula="曝光后 7 日内再次主动打开的去重用户数 / 曝光去重用户数",
        note="长期价值信号，与短期互动分开报告，不得相互折算。",
    ),
    Metric(
        key="return_visit_d30",
        name="30 日回访率",
        horizon="long_term",
        version=CATALOG_VERSION,
        formula="曝光后 30 日内再次主动打开的去重用户数 / 曝光去重用户数",
        note="长期价值信号。",
    ),
)

METRIC_KEYS = frozenset(m.key for m in METRICS)
SHORT_TERM_KEYS = frozenset(m.key for m in METRICS if m.horizon == "short_term")
LONG_TERM_KEYS = frozenset(m.key for m in METRICS if m.horizon == "long_term")


def get(key: str) -> Metric:
    for m in METRICS:
        if m.key == key:
            return m
    raise KeyError(f"未登记的指标口径: {key}")


def validate_weights(weights: dict) -> None:
    """校验策略权重：键必须登记、值非负、总和为 1（允许误差）。"""
    if not weights:
        raise ValueError("权重不能为空")
    unknown = set(weights) - METRIC_KEYS
    if unknown:
        raise ValueError(f"权重引用了未登记指标: {sorted(unknown)}")
    bad = {k: v for k, v in weights.items() if not isinstance(v, (int, float)) or v < 0}
    if bad:
        raise ValueError(f"权重必须为非负数: {bad}")
    total = round(sum(float(v) for v in weights.values()), 9)
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"权重之和必须为 1，当前为 {total}")


def catalog_snapshot() -> dict:
    """返回口径目录快照，实验分段记录所用版本以保证可比。"""
    return {
        "catalog_version": CATALOG_VERSION,
        "metrics": [
            {"key": m.key, "name": m.name, "horizon": m.horizon,
             "version": m.version, "formula": m.formula, "note": m.note}
            for m in METRICS
        ],
    }
