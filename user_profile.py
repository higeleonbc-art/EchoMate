"""
user_profile.py - ユーザープロファイル管理モジュール（良き隣人システム）

ユーザーの好み・性格・話し方をJSONで永続保存し、
加重平均で段階的に更新する。古い傾向の固定化を防ぐため
定期的に減衰を適用する。
"""

import json
import logging
import time
import threading
from typing import Any

logger = logging.getLogger(__name__)

PROFILE_PATH = "user_profile.json"
DECAY_FACTOR = 0.95   # 古い傾向の固定化防止（中心値0.5へ引き戻す）
CENTER_VALUE  = 0.5   # 減衰の収束先

_DEFAULT_PROFILE: dict = {
    "preferences": {
        "response_length": "medium",   # short / medium / long
        "likes_tone":      "casual",   # casual / formal / energetic / calm
        "dislikes":        [],
    },
    "personality": {
        "stress_tolerance": 0.5,       # 0.0=低耐性 〜 1.0=高耐性
        "aggressiveness":   0.5,       # 0.0=穏やか 〜 1.0=攻撃的
        "talkativeness":    0.5,       # 0.0=無口   〜 1.0=おしゃべり
    },
    "speech_style": {
        "slang":       0.5,            # 0.0=丁寧   〜 1.0=スラング多め
        "brevity":     0.5,            # 0.0=長文   〜 1.0=短文
        "exclamation": 0.5,            # 感嘆符使用率
    },
    "session_stats": {
        "total_sessions": 0,
        "total_logs":     0,
        "last_updated":   0.0,
    },
    "growth_observations": [],         # 最新3件のみ保持
}


def _deep_merge(base: dict, override: dict) -> dict:
    """ネストされたdictを再帰的にマージする（overrideがbaseを上書き）"""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _deep_copy(d: dict) -> dict:
    return json.loads(json.dumps(d, ensure_ascii=False))


class UserProfile:
    """ユーザープロファイルの読み書きと更新を管理するクラス"""

    def __init__(self, path: str = PROFILE_PATH) -> None:
        self.path = path
        self._data: dict = {}
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # ロード / セーブ
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            self._data = _deep_merge(_DEFAULT_PROFILE.copy(), loaded)
            logger.info("UserProfile loaded from %s", self.path)
        except FileNotFoundError:
            self._data = _deep_copy(_DEFAULT_PROFILE)
            logger.info("UserProfile not found, using defaults")
        except json.JSONDecodeError as e:
            logger.warning("UserProfile corrupt: %s — using defaults", e)
            self._data = _deep_copy(_DEFAULT_PROFILE)

    def save(self) -> None:
        """プロファイルをJSONファイルに保存する"""
        with self._lock:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                logger.debug("UserProfile saved to %s", self.path)
            except OSError as e:
                logger.error("Failed to save UserProfile: %s", e)

    def get(self) -> dict:
        """プロファイルのコピーを返す（スレッドセーフ）"""
        with self._lock:
            return _deep_copy(self._data)

    # ------------------------------------------------------------------
    # 更新系
    # ------------------------------------------------------------------

    def update_numeric(self, path: list[str], new_value: float, weight: float = 0.2) -> None:
        """
        数値プロパティを加重平均で更新する。
        weight=0.2 → 新値20%・旧値80%の割合で更新（緩やかな変化）
        """
        with self._lock:
            target = self._data
            for key in path[:-1]:
                target = target.setdefault(key, {})
            old = target.get(path[-1], CENTER_VALUE)
            target[path[-1]] = round(old * (1 - weight) + new_value * weight, 4)

    def update_preference(self, key: str, value: Any) -> None:
        """preferences の値を直接更新する"""
        with self._lock:
            self._data["preferences"][key] = value

    def add_dislike(self, item: str) -> None:
        """dislikes に追加する（重複なし・最大10件）"""
        with self._lock:
            dislikes = self._data["preferences"].setdefault("dislikes", [])
            if item not in dislikes:
                dislikes.append(item)
                if len(dislikes) > 10:
                    dislikes.pop(0)

    def apply_decay(self) -> None:
        """
        性格スコアに減衰を適用する（固定化防止）。
        すべての数値スコアを中心値0.5へ向かってわずかに引き戻す。
        """
        with self._lock:
            for category in ("personality", "speech_style"):
                for key, val in self._data.get(category, {}).items():
                    if isinstance(val, float):
                        new_val = val * DECAY_FACTOR + CENTER_VALUE * (1 - DECAY_FACTOR)
                        self._data[category][key] = round(new_val, 4)

    def add_growth_observation(self, text: str) -> None:
        """成長観察メモを追加する（最大3件）"""
        with self._lock:
            obs = self._data.setdefault("growth_observations", [])
            obs.append({"text": text, "timestamp": time.time()})
            if len(obs) > 3:
                obs.pop(0)

    def get_latest_growth_observation(self) -> str | None:
        """最新の成長観察テキストを返す（なければNone）"""
        with self._lock:
            obs = self._data.get("growth_observations", [])
            return obs[-1]["text"] if obs else None

    def pop_latest_growth_observation(self) -> str | None:
        """最新の成長観察を取り出して削除する（表示後の重複防止）"""
        with self._lock:
            obs = self._data.get("growth_observations", [])
            if obs:
                return obs.pop()["text"]
            return None

    def increment_session(self, log_count: int) -> None:
        """セッション統計を更新する"""
        with self._lock:
            stats = self._data.setdefault("session_stats", {})
            stats["total_sessions"] = stats.get("total_sessions", 0) + 1
            stats["total_logs"]     = stats.get("total_logs", 0) + log_count
            stats["last_updated"]   = time.time()

    # ------------------------------------------------------------------
    # プロンプト用サマリー
    # ------------------------------------------------------------------

    def get_summary_for_prompt(self) -> str:
        """
        RAGプロンプトに埋め込む簡潔なプロファイルサマリーを返す。
        LLMへのトークン消費を抑えるため50文字以内に収める。
        """
        with self._lock:
            p   = self._data.get("preferences", {})
            per = self._data.get("personality", {})
            ss  = self._data.get("speech_style", {})

            length  = p.get("response_length", "medium")
            tone    = p.get("likes_tone", "casual")
            dislikes_raw = p.get("dislikes", [])
            dislikes = "、".join(dislikes_raw[:3]) if dislikes_raw else "特になし"

            stress  = per.get("stress_tolerance", 0.5)
            aggr    = per.get("aggressiveness", 0.5)
            slang   = ss.get("slang", 0.5)

            stress_label  = "高" if stress > 0.6 else ("低" if stress < 0.4 else "中")
            aggr_label    = "高" if aggr   > 0.6 else ("低" if aggr   < 0.4 else "中")
            slang_label   = "多" if slang  > 0.6 else ("少" if slang  < 0.4 else "普通")

            return (
                f"【ユーザー特性】"
                f"応答希望:{length}/{tone}  "
                f"嫌い:{dislikes}  "
                f"ストレス耐性:{stress_label}  "
                f"攻撃性:{aggr_label}  "
                f"スラング:{slang_label}"
            )
