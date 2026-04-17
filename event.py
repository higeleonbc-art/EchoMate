"""
event.py - イベント管理モジュール

ゲームイベントの定義・優先度管理・メモリ管理を担当する。
将来的にOpenCVなど外部ソースからのイベント注入にも対応可能な構造。
"""

import queue
import json
import threading
import random
import time
import logging
import uuid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# イベント優先度定義
# 数値が小さいほど優先度が高い（PriorityQueueは最小値優先）
# ---------------------------------------------------------------------------
EVENT_PRIORITY: dict[str, int] = {
    "player_speech": 0,  # 最高優先度：プレイヤー発言
    "death": 1,          # 高優先度：デス
    "kill": 1,           # 高優先度：キル
    "low_hp": 2,         # 中優先度：HP低下
    "big_play": 2,       # 中優先度：大技
}

# ダミーイベント生成用の重み（テスト用）
DUMMY_EVENT_WEIGHTS = {
    "kill": 0.30,
    "death": 0.30,
    "low_hp": 0.20,
    "big_play": 0.20,
}


class GameEvent:
    """ゲームイベントを表すデータクラス"""

    def __init__(self, event_type: str, data: dict | None = None):
        self.id = str(uuid.uuid4())          # ミニ会話トラッキング用の一意ID
        self.event_type = event_type
        self.data = data or {}
        self.priority = EVENT_PRIORITY.get(event_type, 3)
        self.timestamp = time.time()

    def __lt__(self, other: "GameEvent") -> bool:
        """PriorityQueue用の比較。優先度→タイムスタンプの順で評価"""
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.timestamp < other.timestamp

    def __repr__(self) -> str:
        return f"GameEvent(type={self.event_type}, priority={self.priority})"


class EventManager:
    """
    イベントキューとメモリを管理するクラス。

    - PriorityQueue でイベントを優先度管理
    - JSON ベースの軽量メモリを保持
    - スレッドセーフ
    """

    MEMORY_DEFAULTS: dict = {
        "last_event": "",
        "player_tendency": "",
        "recent_topics": [],
    }

    def __init__(self) -> None:
        self.event_queue: queue.PriorityQueue = queue.PriorityQueue()
        self.memory: dict = self.MEMORY_DEFAULTS.copy()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # イベント操作
    # ------------------------------------------------------------------

    def add_event(self, event: GameEvent) -> None:
        """イベントをキューに追加する"""
        if event.event_type == "player_speech":
            # 古い player_speech は文脈がズレるので捨てて最新だけ残す
            items: list = []
            while True:
                try:
                    items.append(self.event_queue.get_nowait())
                except queue.Empty:
                    break
            stale = sum(1 for _, _, e in items if e.event_type == "player_speech")
            if stale:
                logger.info("Dropped %d stale player_speech event(s) from queue", stale)
            for item in (x for x in items if x[2].event_type != "player_speech"):
                self.event_queue.put(item)
        self.event_queue.put((event.priority, event.timestamp, event))
        logger.debug("Event added: %s (priority=%d)", event.event_type, event.priority)

    def get_event(self, timeout: float = 0.1) -> GameEvent | None:
        """キューからイベントを取得する。タイムアウト時は None を返す"""
        try:
            _, _, event = self.event_queue.get(timeout=timeout)
            logger.debug("Event dequeued: %s", event.event_type)
            return event
        except queue.Empty:
            return None

    def queue_size(self) -> int:
        return self.event_queue.qsize()

    # ------------------------------------------------------------------
    # メモリ操作
    # ------------------------------------------------------------------

    def update_memory(self, event_type: str, topic: str | None = None) -> None:
        """直近イベントと話題リストを更新する"""
        with self._lock:
            self.memory["last_event"] = event_type
            if topic:
                self.memory["recent_topics"].append(topic)
                # 直近5件に制限
                if len(self.memory["recent_topics"]) > 5:
                    self.memory["recent_topics"].pop(0)

    def update_player_tendency(self, tendency: str) -> None:
        """プレイヤー傾向を更新する"""
        with self._lock:
            self.memory["player_tendency"] = tendency

    def get_memory(self) -> dict:
        """メモリのコピーを返す（スレッドセーフ）"""
        with self._lock:
            return self.memory.copy()

    def save_memory(self, filepath: str = "memory.json") -> None:
        """メモリを JSON ファイルに保存する"""
        with self._lock:
            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(self.memory, f, ensure_ascii=False, indent=2)
                logger.info("Memory saved to %s", filepath)
            except OSError as e:
                logger.error("Failed to save memory: %s", e)

    def load_memory(self, filepath: str = "memory.json") -> None:
        """JSON ファイルからメモリを読み込む"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            with self._lock:
                self.memory.update(loaded)
            logger.info("Memory loaded from %s", filepath)
        except FileNotFoundError:
            logger.info("Memory file not found, using defaults: %s", filepath)
        except json.JSONDecodeError as e:
            logger.warning("Memory file corrupt, using defaults: %s", e)


# ---------------------------------------------------------------------------
# ダミーイベント生成（テスト・デバッグ用）
# ---------------------------------------------------------------------------

def generate_dummy_event() -> GameEvent:
    """
    テスト用のランダムゲームイベントを生成する。
    将来的にはここを OpenCV 等の実装に置き換える。
    """
    events = list(DUMMY_EVENT_WEIGHTS.keys())
    weights = list(DUMMY_EVENT_WEIGHTS.values())
    event_type = random.choices(events, weights=weights, k=1)[0]
    logger.debug("Dummy event generated: %s", event_type)
    return GameEvent(event_type)
