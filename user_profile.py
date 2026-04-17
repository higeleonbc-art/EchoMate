"""
user_profile.py - ユーザープロファイル管理モジュール（良き隣人システム）

ユーザーの好み・性格・話し方をJSONで永続保存し、
加重平均で段階的に更新する。古い傾向の固定化を防ぐため
定期的に減衰を適用する。
"""

import json
import logging
import os
import tempfile
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
    "recent_context_summary": "",     # 長期会話の要約（LLMで自動生成）
    # ── 良き隣人システム拡張フィールド ──
    "bond_level":        0.0,          # 0.0〜1.0 親密度の蓄積
    "playstyle_labels":  [],           # ["ゴリ押し", "慎重"] など
    "memorable_episodes": [],          # 印象的な出来事の記憶（最大10件）
    "current_game":      "",           # 現在のゲームタイトル
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

    def __init__(self, path: str = PROFILE_PATH, patron_db=None) -> None:
        self.path = path
        self._data: dict = {}
        self._lock = threading.Lock()
        self._patron_db = patron_db  # PatronDB インスタンス（SQLite移行用）
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

        # PatronDB が接続されている場合、既存 JSON データを SQLite へ移行する
        if self._patron_db is not None:
            self._migrate_to_db()

    def save(self) -> None:
        """プロファイルをJSONファイルにアトミックに保存する。

        一時ファイルへ書き出し → os.replace() でリネームすることで、
        書き込み中のプロセス終了・フリーズによる JSON 破損を防ぐ。
        書き込み前にディスク上の最新状態を再ロードしてマージすることで、
        PatronAnalyzer（別スレッド）との同時書き込みによる先祖返りも防ぐ。
        """
        with self._lock:
            try:
                # ディスク上の最新状態を再ロードしてマージ（in_memory が優先）
                try:
                    with open(self.path, encoding="utf-8") as f:
                        on_disk = json.load(f)
                    to_write = _deep_merge(on_disk, self._data)
                except (FileNotFoundError, json.JSONDecodeError):
                    to_write = self._data

                # アトミック書き込み: 同一ディレクトリの一時ファイル → リネーム
                dir_name = os.path.dirname(os.path.abspath(self.path))
                tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        json.dump(to_write, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, self.path)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
                logger.debug("UserProfile saved to %s (atomic)", self.path)
            except OSError as e:
                logger.error("Failed to save UserProfile: %s", e)

    def _migrate_to_db(self) -> None:
        """JSON 内の episodes / observations を SQLite へ一回だけ移行する"""
        try:
            episodes = self._data.get("memorable_episodes", [])
            if episodes:
                self._patron_db.migrate_episodes_from_list(episodes)
                self._data["memorable_episodes"] = []
                logger.info("Migrated %d episodes to SQLite", len(episodes))

            obs = self._data.get("growth_observations", [])
            if obs:
                self._patron_db.migrate_observations_from_list(obs)
                self._data["growth_observations"] = []
                logger.info("Migrated %d growth_observations to SQLite", len(obs))
        except Exception as e:
            logger.warning("Migration to SQLite failed: %s", e)

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
        """成長観察メモを追加する。PatronDB 接続時は SQLite へ、未接続時は JSON へ。"""
        if self._patron_db is not None:
            try:
                self._patron_db.add_growth_observation(text)
                return
            except Exception as e:
                logger.warning("add_growth_observation to DB failed: %s", e)
        with self._lock:
            obs = self._data.setdefault("growth_observations", [])
            obs.append({"text": text, "timestamp": time.time()})
            if len(obs) > 3:
                obs.pop(0)

    def get_latest_growth_observation(self) -> str | None:
        """最新の成長観察テキストを返す（なければNone）"""
        if self._patron_db is not None:
            try:
                rows = self._patron_db.get_growth_observations(limit=1)
                return rows[0]["text"] if rows else None
            except Exception as e:
                logger.warning("get_latest_growth_observation from DB failed: %s", e)
        with self._lock:
            obs = self._data.get("growth_observations", [])
            return obs[-1]["text"] if obs else None

    def pop_latest_growth_observation(self) -> str | None:
        """最新の成長観察を取り出して消費済みにする（表示後の重複防止）"""
        if self._patron_db is not None:
            try:
                return self._patron_db.pop_latest_growth_observation()
            except Exception as e:
                logger.warning("pop_latest_growth_observation from DB failed: %s", e)
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
    # 良き隣人システム拡張 setter
    # ------------------------------------------------------------------

    def update_context_summary(self, summary: str) -> None:
        """LLMが生成した長期会話の要約を保存する"""
        with self._lock:
            self._data["recent_context_summary"] = summary

    def get_context_summary(self) -> str:
        """保存済みの会話要約を返す（なければ空文字）"""
        with self._lock:
            return self._data.get("recent_context_summary", "")

    def add_bond(self, amount: float = 0.01) -> None:
        """親密度を加算する（上限 1.0）"""
        with self._lock:
            cur = self._data.get("bond_level", 0.0)
            self._data["bond_level"] = round(min(1.0, cur + amount), 4)

    def set_current_game(self, game_name: str) -> None:
        """現在のゲームタイトルを更新する"""
        with self._lock:
            self._data["current_game"] = game_name

    def get_current_game(self) -> str:
        """現在のゲームタイトルを返す"""
        with self._lock:
            return self._data.get("current_game", "")

    def get_bond_level(self) -> float:
        """親密度を返す"""
        with self._lock:
            return float(self._data.get("bond_level", 0.0))

    def add_playstyle_label(self, label: str) -> None:
        """プレイスタイルラベルを追加する（重複なし・最大5件）"""
        with self._lock:
            labels = self._data.setdefault("playstyle_labels", [])
            if label and label not in labels:
                labels.append(label)
                if len(labels) > 5:
                    labels.pop(0)

    def set_playstyle_labels(self, labels: list[str]) -> None:
        """プレイスタイルラベルを一括置換する（最大5件）"""
        with self._lock:
            self._data["playstyle_labels"] = [l for l in labels if isinstance(l, str)][:5]

    def add_memorable_episode(self, text: str, game: str = "") -> None:
        """印象的な出来事を記録する。PatronDB 接続時は SQLite へ、未接続時は JSON へ。"""
        resolved_game = game or self.get_current_game()
        if self._patron_db is not None:
            try:
                self._patron_db.add_episode(text, resolved_game)
                return
            except Exception as e:
                logger.warning("add_memorable_episode to DB failed: %s", e)
        with self._lock:
            episodes = self._data.setdefault("memorable_episodes", [])
            episodes.append({
                "text":      text,
                "game":      resolved_game,
                "timestamp": time.time(),
            })
            if len(episodes) > 10:
                episodes.pop(0)

    def get_memorable_episodes(self) -> list[dict]:
        """memorable_episodes のコピーを返す"""
        if self._patron_db is not None:
            try:
                return self._patron_db.get_episodes(limit=10)
            except Exception as e:
                logger.warning("get_memorable_episodes from DB failed: %s", e)
        with self._lock:
            return list(self._data.get("memorable_episodes", []))

    # ------------------------------------------------------------------
    # プロンプト用サマリー
    # ------------------------------------------------------------------

    def get_growth_summary_for_prompt(self) -> str:
        """
        過去セッションとの成長比較サマリーをRAGプロンプト用に返す。
        「前よりエイム良くなってるね！」「今日はデス多めだけど大丈夫？」
        といった発言を引き出すためのコンテキストとして使用する。
        """
        # PatronDB 接続時は SQLite から最新観察を取得
        if self._patron_db is not None:
            try:
                db_obs = self._patron_db.get_growth_observations(limit=2)
            except Exception:
                db_obs = []
        else:
            db_obs = []

        with self._lock:
            obs = db_obs if db_obs else self._data.get("growth_observations", [])
            stats = self._data.get("session_stats", {})
            personality = self._data.get("personality", {})

            parts = []

            sessions = stats.get("total_sessions", 0)
            if sessions >= 2:
                parts.append(f"{sessions}セッション目")

            # 成長観察メモ（最新2件）
            for o in obs[-2:]:
                text = o.get("text", "")
                if text:
                    parts.append(text)

            # ストレス耐性が低い場合は苦手パターンとして言及
            stress = personality.get("stress_tolerance", 0.5)
            if stress < 0.35:
                parts.append("デス後にストレスがかかりやすい")

            if not parts:
                return ""

            return "【成長観察】" + " / ".join(parts)

    def summarize_long_term(self, llm_fn) -> bool:
        """
        古くなった成長記録が10件以上ある場合、LLMで1つの物語に要約して
        recent_context_summary を更新し、使用済み観察を圧縮する。

        llm_fn: (prompt: str) -> str のシグネチャを持つLLM呼び出し関数。
        Returns: 要約を実行した場合 True。
        """
        if self._patron_db is None:
            return False
        try:
            obs = self._patron_db.get_growth_observations(limit=20)
            if len(obs) < 10:
                return False

            texts = [o["text"] for o in reversed(obs)]  # 古い順
            combined = "\n".join(f"- {t}" for t in texts)

            prompt = (
                "以下はゲームプレイヤーの成長観察記録です。\n"
                "これらを1つのまとまった物語として、100文字以内の日本語で要約してください。\n"
                "プレイヤーの変化・傾向・特徴を凝縮して表現してください。\n\n"
                f"記録:\n{combined}\n\n"
                "要約（100文字以内）:"
            )

            summary = llm_fn(prompt)
            if not summary:
                return False

            self.update_context_summary(summary)
            consumed = self._patron_db.consume_observations(len(obs))
            self.save()
            logger.info(
                "Long-term memory summarized: %d observations compressed (%d chars)",
                consumed, len(summary),
            )
            return True
        except Exception as e:
            logger.error("summarize_long_term error: %s", e)
            return False

    def get_summary_for_prompt(self) -> str:
        """
        RAGプロンプトに埋め込む簡潔なプロファイルサマリーを返す。
        bond_level・playstyle_labels・memorable_episodes も含める。
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

            base = (
                f"【ユーザー特性】"
                f"応答希望:{length}/{tone}  "
                f"嫌い:{dislikes}  "
                f"ストレス耐性:{stress_label}  "
                f"攻撃性:{aggr_label}  "
                f"スラング:{slang_label}"
            )

            # 親密度
            bond = float(self._data.get("bond_level", 0.0))
            bond_label = "高い" if bond >= 0.7 else ("中程度" if bond >= 0.4 else "低い")
            base += f"  親密度:{bond:.2f}({bond_label})"

            # プレイスタイル
            labels = self._data.get("playstyle_labels", [])
            if labels:
                base += f"  プレイスタイル:{'・'.join(labels[:3])}"

            return base
