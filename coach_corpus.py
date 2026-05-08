"""
coach_corpus.py — コーチ動画 transcript の自動取得 + LLM要約パイプライン

data/coaches/sources.json で指定された動画IDから transcript を取得し、
qwen3 (or 指定LLM) で「ADCコーチング上の重要発言・原則・戦術」を抽出して
data/coaches/{coach_name}.json として保存する。

CLI:
    python coach_corpus.py                   # 全コーチを処理
    python coach_corpus.py --coach ls        # 特定コーチのみ
    python coach_corpus.py --refresh         # 既存transcriptキャッシュを無視
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from coach_ai import coach_chat

logger = logging.getLogger(__name__)

DATA_DIR        = Path(__file__).parent / "data" / "coaches"
SOURCES_PATH    = DATA_DIR / "sources.json"
CACHE_DIR       = DATA_DIR / "_transcripts"

PLACEHOLDER_PREFIX = "PLACE_"   # placeholder ID を除外する判定
TRANSCRIPT_LANGS   = ["en", "ja"]
MAX_CHARS_PER_LLM  = 7000        # 1動画あたりLLMに投げる最大文字数


# ---------------------------------------------------------------------------
# Transcript取得
# ---------------------------------------------------------------------------

def fetch_transcript(video_id: str) -> Optional[str]:
    """video_id から字幕テキストを取得（en→ja優先順）。

    youtube-transcript-api を試して失敗（IPブロック等）したら yt-dlp に自動fallback。
    """
    # 1) youtube-transcript-api を試す
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=TRANSCRIPT_LANGS)
        text = " ".join(seg.text for seg in fetched.snippets)
        return text.strip()
    except ImportError:
        logger.error("youtube-transcript-api 未インストール")
    except Exception as e:
        logger.debug("youtube-transcript-api failed for %s, trying yt-dlp: %s", video_id, str(e)[:80])

    # 2) yt-dlp fallback
    return _fetch_transcript_via_ytdlp(video_id)


def _fetch_transcript_via_ytdlp(video_id: str) -> Optional[str]:
    """yt-dlp で字幕（en優先 → 自動字幕も可）を取得し、テキスト化"""
    import subprocess
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = _P(tmp)
        url = f"https://www.youtube.com/watch?v={video_id}"
        # 手動字幕→自動字幕→ja の順で fallback
        for sub_args in (
            ["--write-sub",      "--sub-lang", "en"],
            ["--write-auto-sub", "--sub-lang", "en"],
            ["--write-auto-sub", "--sub-lang", "ja"],
        ):
            try:
                subprocess.run(
                    ["yt-dlp", *sub_args, "--skip-download",
                     "--sub-format", "vtt",
                     "-o", str(tmp_dir / "%(id)s"),
                     "--quiet", "--no-warnings",
                     url],
                    check=True, timeout=120,
                )
            except Exception as e:
                logger.debug("yt-dlp call failed: %s", str(e)[:80])
                continue
            # vtt ファイル探索
            vtts = list(tmp_dir.glob("*.vtt"))
            if vtts:
                return _vtt_to_text(vtts[0].read_text(encoding="utf-8"))
    logger.warning("yt-dlp fallback also failed for %s", video_id)
    return None


_VTT_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}.*-->.*$")
_VTT_HEADER_RE    = re.compile(r"^(WEBVTT|Kind:|Language:|NOTE)\b")


def _vtt_to_text(vtt: str) -> str:
    """WebVTT 字幕からプレーンテキスト抽出（重複行除去）"""
    seen: list[str] = []
    seen_set: set = set()
    for line in vtt.splitlines():
        s = line.strip()
        if not s:
            continue
        if _VTT_TIMESTAMP_RE.match(s) or _VTT_HEADER_RE.match(s):
            continue
        # タグ除去
        s = re.sub(r"<[^>]+>", "", s)
        if s and s not in seen_set:
            seen_set.add(s)
            seen.append(s)
    return " ".join(seen)


def cached_transcript(video_id: str, refresh: bool = False) -> Optional[str]:
    """ローカル _transcripts/ キャッシュ経由で transcript を取得。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{video_id}.txt"
    if cache_path.exists() and not refresh:
        return cache_path.read_text(encoding="utf-8")
    text = fetch_transcript(video_id)
    if text:
        cache_path.write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------------------
# LLM要約
# ---------------------------------------------------------------------------

SUMMARIZE_SYSTEM = """\
あなたはLeague of Legends ADCコーチング動画 transcript を構造化要約するアシスタントです。
要約対象は「実プレイヤーがランクアップに使える発言・原則・具体戦術」であり、
雑談・冗談・自己紹介・ゲームプレイ垂れ流しは除外します。

出力形式: **JSONのみ**（前置き/後書き禁止、コードブロック不要、思考過程禁止）。
"""


SUMMARIZE_USER_TEMPLATE = """\
コーチ名: {coach_name}
動画トピック: {topic}

以下の transcript から、コーチング上で価値の高い情報のみを抽出して、下記スキーマの JSON で返してください。

要件:
- phrases: コーチ自身の口調が出ている短い印象的フレーズ（10〜80文字）。直接引用ではなく要約形でOK
- principles: 指導原則（フレームワーク的な教え）
- tactics: 具体的な戦術アクション（「○○の時は××する」）

スキーマ:
{{
  "phrases":    [{{ "quote": "string", "topic": "string" }}, ... ],
  "principles": [{{ "name":  "string", "summary": "string" }}, ... ],
  "tactics":    [{{ "scenario": "string", "action": "string" }}, ... ]
}}

各リスト最大件数: phrases 6, principles 4, tactics 5
重複を避ける。曖昧な精神論（「集中しよう」だけ等）は除外。

TRANSCRIPT:
\"\"\"{transcript}\"\"\"

/no_think
"""


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def summarize_transcript(coach_name: str, topic: str, transcript: str) -> Optional[dict]:
    """transcript を LLM で要約 → dict返す。失敗時 None。"""
    if not transcript:
        return None
    snippet = transcript[:MAX_CHARS_PER_LLM]
    user = SUMMARIZE_USER_TEMPLATE.format(
        coach_name=coach_name,
        topic=topic,
        transcript=snippet,
    )
    try:
        raw = coach_chat(SUMMARIZE_SYSTEM, user)
    except Exception as e:
        logger.warning("LLM summarize failed for topic=%s: %s", topic, e)
        return None
    return _parse_json_relaxed(raw)


def _parse_json_relaxed(text: str) -> Optional[dict]:
    """LLM出力に余計なテキストが混じってもJSONを救出する"""
    if not text:
        return None
    # まず素直に
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # コードブロック除去
    text2 = re.sub(r"```(?:json)?\s*|\s*```", "", text)
    try:
        return json.loads(text2.strip())
    except json.JSONDecodeError:
        pass
    # 最初の {...} を抽出
    m = _JSON_OBJECT_RE.search(text2)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# 集約 & 保存
# ---------------------------------------------------------------------------

def merge_summaries(summaries: list[dict]) -> dict:
    """複数動画の要約を結合し、phrases / principles / tactics を統合"""
    out = {"phrases": [], "principles": [], "tactics": []}
    seen_phrases = set()
    seen_principles = set()
    seen_tactics = set()

    for s in summaries:
        if not s:
            continue
        for p in s.get("phrases", []) or []:
            key = (p.get("quote") or "").strip()
            if not key or key in seen_phrases:
                continue
            seen_phrases.add(key)
            out["phrases"].append({"quote": key, "topic": (p.get("topic") or "").strip()})
        for pr in s.get("principles", []) or []:
            key = (pr.get("name") or "").strip()
            if not key or key in seen_principles:
                continue
            seen_principles.add(key)
            out["principles"].append({"name": key, "summary": (pr.get("summary") or "").strip()})
        for t in s.get("tactics", []) or []:
            key = (t.get("scenario") or "").strip()
            if not key or key in seen_tactics:
                continue
            seen_tactics.add(key)
            out["tactics"].append({"scenario": key, "action": (t.get("action") or "").strip()})

    # 件数上限
    out["phrases"]    = out["phrases"][:20]
    out["principles"] = out["principles"][:10]
    out["tactics"]    = out["tactics"][:15]
    return out


def save_coach_corpus(coach_key: str, coach_meta: dict, merged: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "coach_key":  coach_key,
            "coach_name": coach_meta.get("name", coach_key),
            "channel":    coach_meta.get("channel"),
            "generated_by": "coach_corpus.py",
        },
        **merged,
    }
    out = DATA_DIR / f"{coach_key}.json"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def process_coach(coach_key: str, coach_meta: dict, refresh: bool) -> dict:
    summaries: list[dict] = []
    videos = coach_meta.get("videos", [])
    skipped: list[str] = []

    for v in videos:
        vid = v.get("id", "")
        topic = v.get("topic", "")
        if not vid or vid.startswith(PLACEHOLDER_PREFIX):
            skipped.append(f"{vid} (placeholder)")
            continue
        print(f"  [{coach_key}] {vid}  topic={topic}")
        text = cached_transcript(vid, refresh=refresh)
        if not text:
            skipped.append(f"{vid} (no transcript)")
            continue
        print(f"    transcript: {len(text)} chars  → LLM summarize…")
        summary = summarize_transcript(coach_meta.get("name", coach_key), topic, text)
        if summary:
            summaries.append(summary)
            print(f"    extracted: phrases={len(summary.get('phrases', []))} "
                  f"principles={len(summary.get('principles', []))} "
                  f"tactics={len(summary.get('tactics', []))}")
        else:
            skipped.append(f"{vid} (summarize failed)")
        time.sleep(0.5)  # YouTube rate-limit avoidance

    if not summaries:
        print(f"  [{coach_key}] no usable videos. Skipped: {skipped}")
        return {"merged": None, "skipped": skipped}

    merged = merge_summaries(summaries)
    out = save_coach_corpus(coach_key, coach_meta, merged)
    print(f"  [{coach_key}] saved: {out}")
    print(f"    total: phrases={len(merged['phrases'])} "
          f"principles={len(merged['principles'])} "
          f"tactics={len(merged['tactics'])}")
    if skipped:
        print(f"    skipped {len(skipped)} videos: {skipped}")
    return {"merged": merged, "skipped": skipped, "path": str(out)}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Coach corpus builder")
    p.add_argument("--coach", help="特定コーチキー (ls / curtis 等) のみ処理")
    p.add_argument("--refresh", action="store_true",
                   help="ローカルtranscriptキャッシュを無視して再取得")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not SOURCES_PATH.exists():
        print(f"ERROR: {SOURCES_PATH} not found", file=sys.stderr)
        return 1

    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    coaches = {k: v for k, v in sources.items() if not k.startswith("_")}

    if args.coach:
        if args.coach not in coaches:
            print(f"ERROR: coach '{args.coach}' not in sources", file=sys.stderr)
            return 1
        coaches = {args.coach: coaches[args.coach]}

    print(f"Processing {len(coaches)} coach(es): {list(coaches.keys())}")
    print()

    any_processed = False
    for key, meta in coaches.items():
        print(f"=== {key} ({meta.get('name')}) ===")
        result = process_coach(key, meta, refresh=args.refresh)
        if result.get("merged"):
            any_processed = True
        print()

    if not any_processed:
        print()
        print("⚠ どのコーチでも有効動画ゼロ。data/coaches/sources.json の"
              "PLACE_* プレースホルダーを実際の動画IDに置換してください。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
