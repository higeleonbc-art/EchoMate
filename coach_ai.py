"""
coach_ai.py — コーチング専用の薄いOllamaクライアント

ai.py（旧EchoMate相棒AI）はキャラクター人格・会話力学と密結合のため、
コーチ用には独立した最小クライアントを用意する。
"""

from __future__ import annotations

import logging
import os

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


OLLAMA_CHAT_URL = os.environ.get("OLLAMA_CHAT_API_URL", "http://localhost:11434/api/chat")

# コーチング用は独立変数を使う。旧 LLM_MODEL（echomate-base 等のキャラAI用FTモデル）を
# 流用するとコーチング向けの応答にならないため。デフォルトは素のqwen3:8b。
COACH_MODEL     = os.environ.get("COACH_MODEL", "qwen3:8b")

# 旧 OLLAMA_TIMEOUT (20秒) を継承するとモデル初回ロードに足りない。コーチ用は別変数。
COACH_TIMEOUT   = float(os.environ.get("COACH_TIMEOUT", "180"))


def coach_chat(
    system: str,
    user: str,
    model: str = COACH_MODEL,
    temperature: float = 0.4,
    timeout: float = COACH_TIMEOUT,
) -> str:
    """Ollama /api/chat を1回呼び出してアシスタント応答テキストを返す"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": 600,
        },
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(OLLAMA_CHAT_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return (data.get("message") or {}).get("content", "").strip()


def coach_chat_streaming(system: str, user: str, model: str = COACH_MODEL):
    """ストリーミング版（生成チャンクをyield）"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream": True,
        "options": {"temperature": 0.4, "num_predict": 600},
    }
    with httpx.Client(timeout=COACH_TIMEOUT) as client:
        with client.stream("POST", OLLAMA_CHAT_URL, json=payload) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                import json as _json
                try:
                    chunk = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                msg = (chunk.get("message") or {}).get("content")
                if msg:
                    yield msg
                if chunk.get("done"):
                    break
