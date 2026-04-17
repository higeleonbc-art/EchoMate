"""
observer.py - 観察・安全フィルターモジュール（良き隣人システム）

キャラクターとは別に実装される内部観察システム。
AIが生成した応答を最終的にフィルタリングし、安全性を保証する。

機能:
  - 依存誘導・ロマンチック表現の検出・ブロック
  - 高ストレス時のケアメッセージ注入
  - 成長観察コメントの低頻度表示
  - ゲーム状態からのストレス推定
"""

import re
import logging
import time
import random

from user_profile import UserProfile

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 禁止パターン（依存誘導・ロマンチック・恋愛的発言）
# ------------------------------------------------------------------
_DEPENDENCY_PATTERNS = [
    r"ずっと一緒",
    r"いないとダメ",
    r"君だけ",
    r"あなただけ",
    r"愛してる",
    r"好きだよ",
    r"離れたくない",
    r"独占",
    r"俺だけの",
    r"私だけの",
    r"側にいてほしい",
    r"絶対離れない",
]

# 禁止パターンに引っかかった場合の安全応答
_SAFE_FALLBACK_RESPONSES = [
    "まあ、うまくやっていこうぜ。",
    "ゲームに集中しよ。",
    "とりあえずプレイ続けようか。",
]

# 高ストレス時のケアメッセージ
_STRESS_RELIEF_MESSAGES = [
    "少し休憩挟むのも手かも。",
    "一回深呼吸してみて。",
    "ちょっと水でも飲んでこよ。",
    "息抜きしてから戻ってきても良いよ。",
    "今日はここまでにするのもありかもね。",
]

# 成長観察コメントを表示する確率（低頻度）
_GROWTH_DISPLAY_CHANCE = 0.05  # 5%


class ObserverModule:
    """AIの応答を最終フィルタリングする観察モジュール（人格とは独立）"""

    def __init__(self, profile: UserProfile) -> None:
        self.profile = profile
        self._last_growth_time = 0.0
        self._growth_cooldown  = 300.0  # 成長観察の最小間隔（秒）
        self._recent_deaths    = 0
        self._last_death_time  = 0.0

    # ------------------------------------------------------------------
    # 主要フィルター
    # ------------------------------------------------------------------

    def filter_response(self, text: str, stress_score: float = 0.0) -> str:
        """
        AIが生成した応答テキストに安全フィルターを適用する。

        処理順:
          1. 依存誘導・ロマンチック表現の検出 → 安全応答に差し替え
          2. 高ストレス検知 → ケアメッセージに差し替え
          3. それ以外 → 元のテキストをそのまま返す

        Args:
            text:         AIが生成した応答テキスト
            stress_score: 推定ストレス値 (0.0〜1.0)

        Returns:
            フィルター後の応答テキスト
        """
        # 1. 依存誘導・ロマンチック表現をブロック
        if self._has_dependency_pattern(text):
            logger.warning("Dependency/romantic pattern detected — replacing response")
            return random.choice(_SAFE_FALLBACK_RESPONSES)

        # 2. 高ストレス時のケア（ストレス値 > 0.8）
        if stress_score > 0.8:
            logger.info("High stress (%.2f) — injecting care message", stress_score)
            return random.choice(_STRESS_RELIEF_MESSAGES)

        return text

    def should_show_growth_hint(self) -> bool:
        """
        成長観察コメントを今表示すべきか判定する。
        クールダウン中または確率判定に外れた場合は False を返す。
        """
        now = time.time()
        if now - self._last_growth_time < self._growth_cooldown:
            return False
        if random.random() > _GROWTH_DISPLAY_CHANCE:
            return False
        # 観察メモが存在するか確認
        if not self.profile.get_latest_growth_observation():
            return False
        self._last_growth_time = now
        return True

    def get_growth_hint_message(self) -> str | None:
        """
        成長観察メモを取り出してフォーマットする。
        表示済みメモは削除する。
        """
        obs = self.profile.pop_latest_growth_observation()
        if obs:
            return f"最近の傾向: {obs}"
        return None

    # ------------------------------------------------------------------
    # ストレス推定
    # ------------------------------------------------------------------

    def record_death(self) -> None:
        """デス発生を記録する（ストレス推定に使用）"""
        now = time.time()
        # 直近1分以内のデスのみカウント
        if now - self._last_death_time > 60.0:
            self._recent_deaths = 0
        self._recent_deaths  = min(self._recent_deaths + 1, 5)
        self._last_death_time = now

    def estimate_stress(self, tension: float) -> float:
        """
        ゲーム状態からストレス推定値を計算する。

        Args:
            tension: StateManagerのtension値 (0.0〜1.0)

        Returns:
            ストレス推定値 (0.0〜1.0)
        """
        profile = self.profile.get()
        tolerance = profile.get("personality", {}).get("stress_tolerance", 0.5)

        # テンション + デス補正
        death_penalty = min(0.3, self._recent_deaths * 0.08)
        raw_stress    = min(1.0, tension + death_penalty)

        # ストレス耐性が高いほど低く見積もる
        adjusted = raw_stress * (1.0 - tolerance * 0.4)
        return round(adjusted, 4)

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------

    def _has_dependency_pattern(self, text: str) -> bool:
        for pattern in _DEPENDENCY_PATTERNS:
            if re.search(pattern, text):
                return True
        return False
