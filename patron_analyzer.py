"""
patron_analyzer.py - バッチ分析モジュール（良き隣人システム）

50〜100件のログが蓄積された時点でOllamaを使用してユーザーの傾向を分析し、
UserProfileを更新する。セッション終了時にも同期実行する。

分析はバックグラウンドスレッドで行い、メインループをブロックしない。
Ollamaが利用不可の場合はルールベース分析にフォールバックする。
"""

import json
import logging
import re
import threading
import time
from typing import Optional

import requests

from patron_db import PatronDB
from user_profile import UserProfile

logger = logging.getLogger(__name__)

OLLAMA_API_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL   = "gemma2:2b"
OLLAMA_TIMEOUT = 30  # 分析は時間がかかるため長めに設定

# ルールベース分析用: スラングとみなす文字
_SLANG_CHARS = set("ｗｗｗwww草笑ｗ")


class PatronAnalyzer:
    """ユーザーログをLLMで分析してプロファイルを更新するクラス"""

    def __init__(self, db: PatronDB, profile: UserProfile) -> None:
        self.db      = db
        self.profile = profile
        self._running = False
        self._lock    = threading.Lock()

    # ------------------------------------------------------------------
    # 公開インターフェース
    # ------------------------------------------------------------------

    def analyze_async(self) -> None:
        """バックグラウンドスレッドで分析を実行する（ノンブロッキング）"""
        with self._lock:
            if self._running:
                logger.debug("Analysis already running, skipping")
                return
        t = threading.Thread(
            target=self._run_analysis,
            daemon=True,
            name="PatronAnalyzer",
        )
        t.start()

    def analyze_sync(self) -> None:
        """同期的に分析を実行する（セッション終了時用）"""
        self._run_analysis()

    # ------------------------------------------------------------------
    # 分析本体
    # ------------------------------------------------------------------

    def _run_analysis(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        try:
            logs = self.db.get_unanalyzed_logs(limit=100)
            if not logs:
                logger.debug("No unanalyzed logs — skipping analysis")
                return

            logger.info("Starting patron analysis (%d logs)", len(logs))
            updates = self._analyze_logs(logs)
            if updates:
                self._apply_updates(updates, len(logs))
                self.db.mark_analyzed([l["id"] for l in logs])
                logger.info("Patron analysis complete")
        except Exception as e:
            logger.error("PatronAnalyzer error: %s", e)
        finally:
            self._running = False

    def _analyze_logs(self, logs: list[dict]) -> Optional[dict]:
        """ログリストをLLMで分析してプロファイル更新値を返す"""
        log_text = self._format_logs(logs)

        prompt = (
            "以下はゲームプレイ中のユーザーとAIの会話ログです。\n"
            "このユーザーの特徴を分析してください。\n\n"
            "---\n"
            f"{log_text}\n"
            "---\n\n"
            "以下のJSON形式のみで回答してください（他のテキスト不要）:\n"
            "{\n"
            '  "response_length_preference": "short" or "medium" or "long",\n'
            '  "tone_preference": "casual" or "formal" or "energetic" or "calm",\n'
            '  "stress_tolerance": 0.0〜1.0,\n'
            '  "aggressiveness": 0.0〜1.0,\n'
            '  "slang_usage": 0.0〜1.0,\n'
            '  "talkativeness": 0.0〜1.0,\n'
            '  "dislikes": ["嫌いなこと"],\n'
            '  "summary": "30文字以内のユーザー傾向説明",\n'
            '  "confidence": 0.0〜1.0\n'
            "}\n\nJSON:"
        )

        try:
            res = requests.post(
                OLLAMA_API_URL,
                json={
                    "model":  OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 300},
                },
                timeout=OLLAMA_TIMEOUT,
            )
            if res.status_code != 200:
                logger.error("Ollama analysis HTTP %d", res.status_code)
                return self._simple_analyze(logs)

            raw = res.json().get("response", "")
            parsed = self._parse_json_response(raw)
            return parsed if parsed else self._simple_analyze(logs)

        except requests.exceptions.ConnectionError:
            logger.info("Ollama not available — using rule-based analysis")
            return self._simple_analyze(logs)
        except requests.exceptions.Timeout:
            logger.warning("Ollama analysis timed out — using rule-based analysis")
            return self._simple_analyze(logs)
        except Exception as e:
            logger.error("Analysis LLM error: %s", e)
            return self._simple_analyze(logs)

    def _parse_json_response(self, raw: str) -> Optional[dict]:
        """LLMの応答からJSONブロックを抽出してパースする"""
        # コードブロック除去
        raw = re.sub(r"```[a-z]*", "", raw).strip()
        # 最初の { から最後の } までを抽出
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            logger.warning("No JSON found in analysis response")
            return None
        try:
            return json.loads(match.group())
        except json.JSONDecodeError as e:
            logger.warning("JSON parse error in analysis: %s", e)
            return None

    def _simple_analyze(self, logs: list[dict]) -> dict:
        """
        LLMが利用不可の場合のルールベース分析。
        発言長・スラング率・発言頻度からプロファイル値を推定する。
        """
        user_inputs = [l["user_input"] for l in logs if l.get("user_input")]
        if not user_inputs:
            return {"confidence": 0.2, "summary": "データ不足"}

        avg_len = sum(len(t) for t in user_inputs) / len(user_inputs)
        slang_score = sum(
            any(c in t for c in _SLANG_CHARS) for t in user_inputs
        ) / len(user_inputs)
        talk_ratio = len(user_inputs) / max(len(logs), 1)

        return {
            "response_length_preference": (
                "short" if avg_len < 15 else ("long" if avg_len > 40 else "medium")
            ),
            "slang_usage":   min(1.0, slang_score * 1.5),
            "talkativeness": min(1.0, talk_ratio  * 1.5),
            "confidence":    0.4,
            "summary":       f"発言{avg_len:.0f}文字・スラング{slang_score:.0%}",
        }

    # ------------------------------------------------------------------
    # プロファイル更新
    # ------------------------------------------------------------------

    def _apply_updates(self, updates: dict, log_count: int) -> None:
        """分析結果をUserProfileに適用する"""

        # カテゴリ型の好み
        if "response_length_preference" in updates:
            self.profile.update_preference(
                "response_length", updates["response_length_preference"]
            )
        if "tone_preference" in updates:
            self.profile.update_preference("likes_tone", updates["tone_preference"])

        # 数値スコア（加重平均で緩やかに更新）
        numeric_mapping = [
            ("stress_tolerance", ["personality", "stress_tolerance"]),
            ("aggressiveness",   ["personality", "aggressiveness"]),
            ("slang_usage",      ["speech_style", "slang"]),
            ("talkativeness",    ["personality", "talkativeness"]),
        ]
        for src_key, dst_path in numeric_mapping:
            if src_key in updates:
                try:
                    self.profile.update_numeric(dst_path, float(updates[src_key]), weight=0.2)
                except (TypeError, ValueError):
                    pass

        # 嫌いなこと
        for item in updates.get("dislikes", []):
            if item and isinstance(item, str):
                self.profile.add_dislike(item)

        # 成長観察メモ
        if updates.get("summary"):
            self.profile.add_growth_observation(updates["summary"])

        # 要約をDBに保存
        confidence = float(updates.get("confidence", 0.5))
        summary    = updates.get("summary", "分析完了")
        self.db.add_summary(summary, confidence, log_count)

        # 減衰適用（固定化防止）
        self.profile.apply_decay()

        # ファイルに保存
        self.profile.save()

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------

    def _format_logs(self, logs: list[dict]) -> str:
        """ログリストを分析用テキストに変換する（最新30件・トークン節約）"""
        lines = []
        for log in logs[-30:]:
            user  = log.get("user_input", "")
            ai    = log.get("ai_response", "")
            etype = log.get("event_type", "")
            if user:
                lines.append(f"[{etype}] U:「{user}」→ AI:「{ai}」")
            else:
                lines.append(f"[{etype}] AI:「{ai}」")
        return "\n".join(lines)
