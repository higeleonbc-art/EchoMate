"""
web_learner.py - Webソース提供型学習モジュール（Task6c）

プレイヤーが気になる単語のURLを提供した際に:
1. httpx + BeautifulSoup でページ本文を取得
2. LLM で100文字以内に要約
3. game_knowledge.json に保存し curiosity_list.json から削除する
"""

import json
import logging
import os
import re
from typing import Optional

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

CURIOSITY_LIST_PATH = "curiosity_list.json"
GAME_KNOWLEDGE_PATH = "game_knowledge.json"
OLLAMA_API_URL      = os.getenv("OLLAMA_API_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL        = os.getenv("LLM_MODEL", "qwen3:8b")
OLLAMA_TIMEOUT      = int(os.getenv("OLLAMA_TIMEOUT", "30"))


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------

def load_curiosity_list() -> list[dict]:
    """curiosity_list.json を読み込む。存在しない場合は空リストを返す。"""
    try:
        with open(CURIOSITY_LIST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_curiosity_list(items: list[dict]) -> None:
    with open(CURIOSITY_LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def load_game_knowledge() -> dict:
    """game_knowledge.json を読み込む。存在しない場合は空dictを返す。"""
    try:
        with open(GAME_KNOWLEDGE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_game_knowledge(knowledge: dict) -> None:
    with open(GAME_KNOWLEDGE_PATH, "w", encoding="utf-8") as f:
        json.dump(knowledge, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# スクレイピング
# ---------------------------------------------------------------------------

def scrape_text(url: str) -> Optional[str]:
    """URLからページ本文テキストを取得する。取得失敗時はNoneを返す。"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("beautifulsoup4 not installed. Run: pip install beautifulsoup4")
        return None

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; EchoMate/1.0)"}
        resp = httpx.get(url, headers=headers, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # ノイズタグを除去
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        # 連続する空行を圧縮
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:3000]  # トークン節約のため先頭3000文字

    except Exception as e:
        logger.error("Scraping error for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# LLM要約
# ---------------------------------------------------------------------------

def summarize_with_llm(word: str, text: str) -> Optional[str]:
    """ページ本文をLLMで100文字以内に要約する。失敗時はNoneを返す。"""
    prompt = (
        f"「{word}」についての以下の文章を、100文字以内で簡潔に要約してください。\n"
        "ゲーム内での意味や使われ方を中心に説明してください。\n\n"
        f"---\n{text[:2000]}\n---\n\n"
        "要約（100文字以内）:"
    )
    try:
        resp = httpx.post(
            OLLAMA_API_URL,
            json={
                "model":  OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 150},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.error("LLM summarize HTTP %d", resp.status_code)
            return None
        raw = resp.json().get("response", "").strip()
        first_line = raw.split("\n")[0].strip()
        return first_line[:100] if first_line else None

    except Exception as e:
        logger.error("LLM summarize error: %s", e)
        return None


# ---------------------------------------------------------------------------
# メインAPI
# ---------------------------------------------------------------------------

def learn_from_url(word: str, url: str) -> tuple[bool, str]:
    """
    単語についてURLからWebページを取得・要約して学習する。

    Returns:
        (success: bool, message: str)
    """
    logger.info("WebLearner: learning '%s' from %s", word, url)

    text = scrape_text(url)
    if not text:
        return False, "ページの取得に失敗しました"

    summary = summarize_with_llm(word, text)
    if not summary:
        return False, "要約の生成に失敗しました"

    # game_knowledge.json に保存
    knowledge = load_game_knowledge()
    knowledge[word] = summary
    save_game_knowledge(knowledge)

    # curiosity_list.json から該当単語を削除
    curiosities = load_curiosity_list()
    curiosities = [c for c in curiosities if c.get("word") != word]
    save_curiosity_list(curiosities)

    logger.info("WebLearner: learned '%s' -> %s", word, summary)
    return True, summary
