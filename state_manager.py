"""
state_manager.py - プレイヤー状態管理モジュール

単発イベントを「状態」に変換し、AI へ文脈（HP 状態・連続キル・テンション）を提供する。
EventManager とは独立して StateManager が状態を保持する。

状態遷移:
    kill      → momentum++, tension+0.20
    death     → momentum=0, hp=SAFE, tension+0.40
    low_hp    → hp LOW→CRITICAL, tension+0.30
    big_play  → momentum++, tension+0.15

テンションは最後のイベントから TENSION_DECAY_START 秒後に自動減衰する。
"""

import threading
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# HP 状態定数
HP_SAFE     = "SAFE"
HP_LOW      = "LOW"
HP_CRITICAL = "CRITICAL"

# 戦闘状態定数
COMBAT_IDLE   = "IDLE"
COMBAT_ACTIVE = "IN_COMBAT"

# テンション減衰パラメータ
TENSION_DECAY_RATE  = 0.02   # 1サイクルあたりの減衰量
TENSION_DECAY_START = 10.0   # 最後のイベントから何秒後に減衰を開始するか


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class PlayerState:
    """プレイヤーの現在状態スナップショット"""
    hp_state:        str   = HP_SAFE
    combat_state:    str   = COMBAT_IDLE
    momentum:        int   = 0          # 連続キル数
    tension:         float = 0.3        # 0.0〜1.0（高いほど興奮・緊張）
    input_intensity: float = 0.0        # 0.0〜1.0（操作量の多さ・激しさ）
    last_event_time: float = field(default_factory=time.time)
    last_event_type: str   = ""

    @property
    def input_label(self) -> str:
        """操作強度を人間が読める文字列に変換する"""
        if self.input_intensity >= 0.75:
            return "非常に激しい"
        if self.input_intensity >= 0.5:
            return "激しい"
        if self.input_intensity >= 0.2:
            return "安定"
        return "ほぼ静止"

    def summary(self) -> str:
        """AI プロンプトに埋め込む状態サマリ文字列を返す"""
        tension_label = (
            "非常に高い" if self.tension >= 0.8 else
            "高い"       if self.tension >= 0.6 else
            "普通"        if self.tension >= 0.3 else
            "低い"
        )
        momentum_str = f"{self.momentum}連続キル中" if self.momentum >= 2 else (
            "1キル" if self.momentum == 1 else "なし"
        )
        return (
            f"HP状態: {self.hp_state}\n"
            f"戦闘状態: {self.combat_state}\n"
            f"連続キル: {momentum_str}\n"
            f"テンション: {tension_label}（{self.tension:.2f}）\n"
            f"操作強度: {self.input_label}（{self.input_intensity:.2f}）"
        )

    def to_dict(self) -> dict:
        """スナップショット用辞書（mini conversation 等で使用）"""
        return {
            "hp_state":        self.hp_state,
            "combat_state":    self.combat_state,
            "momentum":        self.momentum,
            "tension":         round(self.tension, 2),
            "input_intensity": round(self.input_intensity, 2),
        }


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

class StateManager:
    """
    ゲームイベントを受け取り PlayerState を更新するクラス。

    使い方:
        sm = StateManager()
        state = sm.update("kill")       # イベント受信時
        sm.record_input_event()         # InputMonitor から操作イベントを受信
        sm.tick()                        # 定期的に呼び出してテンション減衰・操作強度更新
        state = sm.get_state()
    """

    # イベントごとのテンション変化量
    _TENSION_DELTA: dict[str, float] = {
        "kill":     +0.20,
        "death":    +0.40,
        "low_hp":   +0.30,
        "big_play": +0.15,
    }

    # 1 tick（3秒）あたりの操作イベント数の上限（これで intensity=1.0 になる）
    _MAX_EVENTS_PER_TICK = 100
    # 指数移動平均の平滑化係数（大きいほど反応が速い）
    _EMA_ALPHA = 0.4

    def __init__(self) -> None:
        self.state = PlayerState()
        self._lock = threading.Lock()
        self._input_count: int = 0          # 最後の tick から蓄積した操作イベント数
        self._last_input_tick: float = time.time()

    def update(self, event_type: str) -> PlayerState:
        """
        イベント種別を受け取り状態を更新して返す。
        EventProcessor から各イベント処理後に呼び出す。
        StateTick スレッドと EventProcessor スレッドの両方から呼ばれるため
        ロックで保護する。
        """
        with self._lock:
            return self._update_locked(event_type)

    def _update_locked(self, event_type: str) -> PlayerState:
        """ロック取得済みの状態で呼ぶ内部更新メソッド"""
        s = self.state
        s.last_event_type = event_type
        s.last_event_time = time.time()

        if event_type == "kill":
            s.momentum += 1
            s.combat_state = COMBAT_ACTIVE

        elif event_type == "death":
            s.momentum = 0
            s.hp_state  = HP_SAFE     # リスポーン後は HP がリセットされる想定
            s.combat_state = COMBAT_IDLE

        elif event_type == "low_hp":
            # LOW → CRITICAL へ段階的に遷移
            s.hp_state     = HP_CRITICAL if s.hp_state == HP_LOW else HP_LOW
            s.combat_state = COMBAT_ACTIVE

        elif event_type == "big_play":
            s.momentum += 1
            s.combat_state = COMBAT_ACTIVE

        # テンション更新（クランプ 0.0〜1.0）
        delta = self._TENSION_DELTA.get(event_type, 0.0)
        s.tension = min(1.0, max(0.0, s.tension + delta))

        logger.debug(
            "State updated [%s] → hp=%s combat=%s momentum=%d tension=%.2f",
            event_type, s.hp_state, s.combat_state, s.momentum, s.tension,
        )
        return s

    def record_input_event(self) -> None:
        """InputMonitor からマウス・キーボードイベントを受信してカウントする。"""
        with self._lock:
            self._input_count += 1

    def tick(self) -> None:
        """
        定期的に呼び出してテンション減衰・操作強度更新を行う。
        最後のイベントから TENSION_DECAY_START 秒以上経過していれば減衰。
        操作強度は秒間操作数を EMA で平滑化して更新する。
        """
        with self._lock:
            s = self.state
            now = time.time()
            idle_sec = now - s.last_event_time

            if idle_sec > TENSION_DECAY_START:
                s.tension = max(0.0, s.tension - TENSION_DECAY_RATE * (idle_sec / 10.0))

            if idle_sec > 30.0 and s.combat_state == COMBAT_ACTIVE:
                s.combat_state = COMBAT_IDLE
                logger.debug("CombatState → IDLE (idle %.0fs)", idle_sec)

            # 操作強度を EMA で更新
            raw = min(1.0, self._input_count / self._MAX_EVENTS_PER_TICK)
            s.input_intensity = (
                self._EMA_ALPHA * raw
                + (1.0 - self._EMA_ALPHA) * s.input_intensity
            )
            logger.debug(
                "InputIntensity updated: count=%d raw=%.2f ema=%.2f label=%s",
                self._input_count, raw, s.input_intensity, s.input_label,
            )
            self._input_count = 0
            self._last_input_tick = now

    def get_state(self) -> PlayerState:
        with self._lock:
            return self.state
