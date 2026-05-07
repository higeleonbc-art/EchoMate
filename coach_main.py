"""
coach_main.py — LoL ADC Coach エントリーポイント（最小CLI）

機能:
  - Riot ID または PUUID から最新マッチを取得
  - match + timeline → build_review でImprovementPoint抽出
  - coach_prompts + Ollama でコーチコメント生成
  - コンソールに出力

使い方:
    python coach_main.py --riot-id "Hide on bush#KR1" --rank GOLD
    python coach_main.py --puuid <puuid> --count 3 --rank PLATINUM

環境変数（.env推奨）:
    RIOT_API_KEY    — 必須
    RIOT_PLATFORM   — jp1 / kr / na1 等（既定 jp1）
    RIOT_REGION     — asia / americas / europe（platformから自動判定）
    COACH_MODEL     — Ollamaモデル名（既定 qwen3:8b）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Windows の cp932 で全角文字がprintできないのを回避
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from riot_api import RiotAPIClient, RiotAPIError
from match_review import build_review, Review
from coach_prompts import build_review_prompt
from coach_ai import coach_chat
from coach_review_view import open_review_in_browser
from coach_rank import resolve_target_rank


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LoL ADC Coach - post-match review / live overlay")
    p.add_argument("--live", action="store_true",
                   help="インゲームLiveオーバーレイモード（Riot API不要・LoL試合中前提）")
    p.add_argument("--riot-id", help='Riot ID 形式: "Name#TAG"（試合後レビュー時必須）')
    p.add_argument("--puuid", help="puuidを直接指定（試合後レビュー時）")

    p.add_argument("--count", type=int, default=1, help="レビューする試合数（既定1）")
    p.add_argument("--rank", default="auto",
                   help='目標ベンチマークランク。"auto"または未指定なら現ランクから1つ上を自動設定')
    p.add_argument("--queue", type=int, default=None,
                   help="キューID。未指定=全キュー。420=RankedSolo / 440=RankedFlex / 400=NormalDraft / 490=Quickplay")
    p.add_argument("--platform", default=os.environ.get("RIOT_PLATFORM", "jp1"))
    p.add_argument("--view", choices=["console", "html", "both"], default="both",
                   help="レビュー出力先（console/html/both、既定both）")
    p.add_argument("--no-llm", dest="llm", action="store_false", default=True,
                   help="LLMコーチコメント生成をスキップ（ルールベース出力のみ）")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# レビュー出力
# ---------------------------------------------------------------------------

def render_review(review: Review) -> str:
    s = review.stats
    lines = [
        "",
        "=" * 60,
        f"Match {s.match_id}  {s.champion}  {'WIN' if s.win else 'LOSS'}  {s.duration_min}min",
        "=" * 60,
        f"  KDA: {s.kills}/{s.deaths}/{s.assists} (KDA={s.kda:.2f})",
        f"  CS:  total={s.cs_total}  @10={s.cs_at_10}  @15={s.cs_at_15}  /min={s.cs_per_min}",
        f"  Vision: {s.vision_score}",
        f"  Lane: vs {s.enemy_adc}/{s.enemy_support}, with {s.my_support}",
        f"  Deaths at: {s.death_timestamps_min}",
        "",
        f"-- 改善ポイント（target rank: {review.target_rank}） --",
    ]
    if not review.points:
        lines.append("  （ルールベース検出なし）")
    for i, p in enumerate(review.points, 1):
        lines.append(f"  {i}. [{p.severity}/{p.category}] {p.title}")
        lines.append(f"     → {p.suggestion}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def resolve_puuid(client: RiotAPIClient, args: argparse.Namespace) -> str:
    if args.puuid:
        return args.puuid
    if "#" not in args.riot_id:
        sys.exit('--riot-id は "Name#TAG" 形式で指定してください')
    name, tag = args.riot_id.split("#", 1)
    account = client.get_account_by_riot_id(name.strip(), tag.strip())
    return account["puuid"]


def run_review(args: argparse.Namespace) -> int:
    if not (args.riot_id or args.puuid):
        print("ERROR: --riot-id または --puuid を指定してください（--live 以外）",
              file=sys.stderr)
        return 1

    if not os.environ.get("RIOT_API_KEY"):
        print("ERROR: 環境変数 RIOT_API_KEY が未設定です。.env または export してください。",
              file=sys.stderr)
        return 1

    show_console = args.view in ("console", "both")
    show_html = args.view in ("html", "both")

    with RiotAPIClient(platform=args.platform) as client:
        try:
            puuid = resolve_puuid(client, args)
        except RiotAPIError as e:
            print(f"ERROR: puuid解決失敗: {e}", file=sys.stderr)
            return 1

        if show_console:
            print(f"PUUID: {puuid}")

        # ターゲットランクを自動決定
        target_rank, rank_info = resolve_target_rank(client, puuid, args.rank)
        if show_console:
            print(rank_info)

        match_ids = client.get_match_ids(puuid, count=args.count, queue=args.queue)
        if not match_ids:
            print("対象試合がありません")
            return 0

        for mid in match_ids:
            try:
                match = client.get_match(mid)
                timeline = client.get_match_timeline(mid)
            except RiotAPIError as e:
                print(f"  {mid}: 取得失敗 {e}", file=sys.stderr)
                continue

            review = build_review(match, timeline, puuid, rank=target_rank)
            if not review:
                print(f"  {mid}: 自プレイヤーが見つかりません（観戦試合？）")
                continue

            if show_console:
                print(render_review(review))

            comment: str = ""
            if args.llm:
                system, user = build_review_prompt(review)
                if show_console:
                    print("\n-- コーチコメント（生成中…） --")
                try:
                    comment = coach_chat(system, user)
                    if show_console:
                        print(comment)
                except Exception as e:
                    print(f"  LLM呼び出し失敗: {e}", file=sys.stderr)

            if show_html:
                path = open_review_in_browser(review, comment or None)
                print(f"  HTMLレビュー: {path}")

    return 0


def run_live_mode(args: argparse.Namespace) -> int:
    """インゲームLiveオーバーレイ起動"""
    from coach_live import run_live
    print(f"Live overlay 起動 (target rank: {args.rank})  ESCで終了")
    run_live(args.rank)
    return 0


def main() -> int:
    args = parse_args()
    setup_logging(args.debug)
    if args.live:
        return run_live_mode(args)
    return run_review(args)


if __name__ == "__main__":
    sys.exit(main())
