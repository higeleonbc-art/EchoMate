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
OLLAMA_TIMEOUT      = int(os.getenv("OLLAMA_TIMEOUT", "120"))


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
        result = text[:3000]
        logger.info("scrape_text: extracted %d chars from %s", len(result), url)
        return result  # トークン節約のため先頭3000文字

    except Exception as e:
        logger.error("Scraping error for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# LLM要約
# ---------------------------------------------------------------------------

def summarize_with_llm(word: str, text: str) -> Optional[str]:
    """ページ本文をLLMで100文字以内に要約する。失敗時はNoneを返す。"""
    prompt = (
        "/no_think\n"
        f"「{word}」についての以下の文章を、100文字以内で簡潔に要約してください。\n"
        "ゲーム内での意味や使われ方を中心に説明してください。\n\n"
        f"---\n{text[:2000]}\n---\n\n"
        "要約（100文字以内）:"
    )
    logger.info("summarize_with_llm: calling Ollama (model=%s, timeout=%ds)", OLLAMA_MODEL, OLLAMA_TIMEOUT)
    try:
        resp = httpx.post(
            OLLAMA_API_URL,
            json={
                "model":  OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.error("LLM summarize HTTP %d: %s", resp.status_code, resp.text[:200])
            return None
        raw = resp.json().get("response", "").strip()
        logger.info("summarize_with_llm: raw response length=%d", len(raw))
        # qwen3 等の思考モデルが出力する <think>...</think> ブロックを除去
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        first_line = raw.split("\n")[0].strip()
        return first_line[:100] if first_line else None

    except httpx.ConnectError:
        logger.error("LLM summarize error: Ollama not running at %s", OLLAMA_API_URL)
        raise RuntimeError(f"Ollama に接続できません（{OLLAMA_API_URL}）。起動しているか確認してください。")
    except httpx.TimeoutException:
        logger.error("LLM summarize error: timeout after %ds", OLLAMA_TIMEOUT)
        raise RuntimeError(f"Ollama の応答がタイムアウトしました（{OLLAMA_TIMEOUT}s）。OLLAMA_TIMEOUT 環境変数で延長できます。")
    except Exception as e:
        logger.error("LLM summarize error: %s", e)
        return None


# ---------------------------------------------------------------------------
# 名称変更
# ---------------------------------------------------------------------------

def clear_curiosity_list() -> int:
    """curiosity_list.json を空にする。削除した件数を返す。"""
    items = load_curiosity_list()
    count = len(items)
    save_curiosity_list([])
    logger.info("curiosity_list cleared (%d items removed)", count)
    return count


def clear_game_knowledge() -> int:
    """game_knowledge.json を空にする。削除した件数を返す。"""
    knowledge = load_game_knowledge()
    count = len(knowledge)
    save_game_knowledge({})
    logger.info("game_knowledge cleared (%d items removed)", count)
    return count


def rename_curiosity(old_word: str, new_word: str) -> bool:
    """curiosity_list.json の単語名を変更する。変更成功時 True を返す。"""
    new_word = new_word.strip()
    if not new_word or new_word == old_word:
        return False
    items = load_curiosity_list()
    for item in items:
        if item.get("word") == old_word:
            item["word"] = new_word
            save_curiosity_list(items)
            logger.info("Curiosity renamed: '%s' -> '%s'", old_word, new_word)
            return True
    return False


def rename_knowledge(old_word: str, new_word: str) -> bool:
    """game_knowledge.json のキー名を変更する。変更成功時 True を返す。"""
    new_word = new_word.strip()
    if not new_word or new_word == old_word:
        return False
    knowledge = load_game_knowledge()
    if old_word not in knowledge:
        return False
    knowledge[new_word] = knowledge.pop(old_word)
    save_game_knowledge(knowledge)
    logger.info("Knowledge renamed: '%s' -> '%s'", old_word, new_word)
    return True


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


def learn_from_note(word: str, note_text: str) -> tuple[bool, str]:
    """
    単語についてユーザー入力テキストから直接学習する（Webスクレイピング不要）。

    Returns:
        (success: bool, message: str)
    """
    note_text = note_text.strip()
    if not note_text:
        return False, "ノートの内容が空です"

    logger.info("WebLearner: learning '%s' from note (%d chars)", word, len(note_text))

    summary = summarize_with_llm(word, note_text)
    if not summary:
        summary = note_text[:100]

    knowledge = load_game_knowledge()
    knowledge[word] = summary
    save_game_knowledge(knowledge)

    curiosities = load_curiosity_list()
    curiosities = [c for c in curiosities if c.get("word") != word]
    save_curiosity_list(curiosities)

    logger.info("WebLearner: learned '%s' from note -> %s", word, summary)
    return True, summary
