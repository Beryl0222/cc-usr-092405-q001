"""隐私控制：假名化、画像关闭、兴趣重置。

要求与对应实现：
- 任何角色都不得反推出个人观看记录：
  * 全链路只使用假名 anon_id = HMAC(每纪元随机盐, 用户标识)，为单向映射；
  * 重置兴趣时轮换纪元并**销毁旧盐**，旧假名既无法反查身份，也无法与新假名
    关联（旧纪元事件在个体层面不可链接）；
  * 不提供任何 anon_id -> 用户 的解析接口；面向角色的报表只出 k 匿名聚合
    （见 channels.py）。
- 关闭画像：立即清空偏好，推荐与实验分流一律走非个性化基线，即便之后重新
  开启也是新的空画像，历史偏好不会回流。
- 重置兴趣：偏好纪元 +1、清空当前偏好、轮换假名；分流盖戳时只读取当前纪元
  的偏好，重置时间点之前的历史行为不得影响之后的任何决策。
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class PrivacyError(ValueError):
    pass


class Pseudonymizer:
    """带纪元轮换的单向假名化器。旧盐销毁即不可反推、不可链接。"""

    def __init__(self):
        # epoch -> salt；重置时旧 epoch 的盐被删除
        self._salts: Dict[int, bytes] = {1: secrets.token_bytes(32)}
        self.current_epoch = 1

    def rotate(self) -> int:
        self.current_epoch += 1
        self._salts[self.current_epoch] = secrets.token_bytes(32)
        # 销毁旧盐：旧假名此后无法再被计算或比对
        old = self.current_epoch - 1
        self._salts.pop(old, None)
        return self.current_epoch

    def anonymize(self, user_ref: str, epoch: Optional[int] = None) -> str:
        epoch = self.current_epoch if epoch is None else epoch
        salt = self._salts.get(epoch)
        if salt is None:
            raise PrivacyError(f"纪元 {epoch} 的盐已销毁，无法（重新）生成旧假名")
        digest = hmac.new(salt, user_ref.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"a{epoch}_{digest[:32]}"

    def epochs_live(self) -> List[int]:
        return sorted(self._salts)


@dataclass
class UserProfile:
    user_ref: str
    epoch: int = 1
    profiling_enabled: bool = True
    preferences: Dict[str, float] = field(default_factory=dict)
    reset_at: Optional[str] = None
    disabled_at: Optional[str] = None
    history: List[dict] = field(default_factory=list)  # 仅审计状态变更，不存观看明细


class PrivacyStore:
    def __init__(self):
        self.pseudo = Pseudonymizer()
        self._users: Dict[str, UserProfile] = {}

    def _user(self, user_ref: str) -> UserProfile:
        if user_ref not in self._users:
            self._users[user_ref] = UserProfile(user_ref)
        return self._users[user_ref]

    def anon_id(self, user_ref: str) -> str:
        """当前纪元假名。重置后与旧假名不可链接。"""
        u = self._user(user_ref)
        return self.pseudo.anonymize(user_ref, u.epoch)

    def disable_profiling(self, user_ref: str, now: str) -> dict:
        """关闭画像：清空偏好，之后只走非个性化基线。"""
        u = self._user(user_ref)
        if not u.profiling_enabled:
            return self.status(user_ref)
        u.profiling_enabled = False
        u.disabled_at = now
        u.preferences.clear()
        u.history.append({"at": now, "event": "关闭画像", "detail": "偏好已清空"})
        return self.status(user_ref)

    def enable_profiling(self, user_ref: str, now: str) -> dict:
        u = self._user(user_ref)
        if u.profiling_enabled:
            return self.status(user_ref)
        # 重新开启也是全新画像：轮换纪元，防止历史偏好借"重新开启"回流
        u.epoch = self.pseudo.rotate()
        u.profiling_enabled = True
        u.preferences.clear()
        u.history.append({"at": now, "event": "重新开启画像",
                          "detail": f"新纪元 {u.epoch}，历史偏好不恢复"})
        return self.status(user_ref)

    def reset_interests(self, user_ref: str, now: str) -> dict:
        """重置兴趣：新纪元 + 空偏好 + 新假名；旧行为不得影响之后决策。"""
        u = self._user(user_ref)
        u.epoch = self.pseudo.rotate()
        u.preferences.clear()
        u.reset_at = now
        u.history.append({"at": now, "event": "重置兴趣",
                          "detail": f"进入纪元 {u.epoch}，历史偏好失效"})
        return self.status(user_ref)

    def record_preference(self, user_ref: str, category: str, weight: float) -> None:
        """仅在画像开启时记录偏好；关闭状态下任何偏好写入都被拒绝。"""
        u = self._user(user_ref)
        if not u.profiling_enabled:
            raise PrivacyError("用户已关闭画像，禁止记录个性化偏好")
        u.preferences[category] = u.preferences.get(category, 0.0) + weight

    def effective_preferences(self, user_ref: str) -> Dict[str, float]:
        """返回当前生效偏好；画像关闭或重置后必为空，杜绝历史偏好继续影响。"""
        u = self._user(user_ref)
        if not u.profiling_enabled:
            return {}
        return dict(u.preferences)

    def status(self, user_ref: str) -> dict:
        u = self._user(user_ref)
        return {
            "anon_id": self.anon_id(user_ref),
            "epoch": u.epoch,
            "profiling_enabled": u.profiling_enabled,
            "preference_categories": sorted(u.preferences),
            "reset_at": u.reset_at,
            "disabled_at": u.disabled_at,
            "history": list(u.history),
        }
