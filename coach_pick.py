"""
coach_pick.py — 直近10試合をリスト表示してユーザーに選ばせる対話レビュー

start.bat メニュー[2] のバックエンド。
全キュー(queue指定なし)で最新10試合を取得し、番号入力で1試合を選んでレビュー → HTML表示。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

# Windows cp932 回避
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from riot_api import RiotAPIClient, RiotAPIError
from match_review import build_review
from coach_prompts import build_review_prompt
from coach_ai import coach_chat
from coach_review_view import open_review_in_browser
from coach_rank import resolve_target_rank
import coach_profile
import coach_kpi


JST = timezone(timedelta(hours=9))

QUEUE_LABELS: dict[int, str] = {
    420:  "RankedSolo",
    440:  "RankedFlex",
    400:  "NormalDraft",
    430:  "NormalBlind",
    450:  "ARAM",
    700:  "Clash",
    900:  "URF",
    1700: "Arena",
    490:  "Quickplay",
    480:  "SwiftPlay",
}


def queue_label(qid: int) -> str:
    return QUEUE_LABELS.get(qid, f"Q{qid}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pick a recent match to review")
    p.add_argument("--riot-id", default=None,
                   help='Riot ID "Name#TAG"。未指定時はプロファイル(.coach_profile.json)から読み込み')
    p.add_argument("--rank", default="auto",
                   help='目標ランク。"auto"または未指定なら現ランクから1つ上を自動設定')
    p.add_argument("--count", type=int, default=10, help="表示する直近試合数")
    p.add_argument("--no-llm", dest="llm", action="store_false", default=True)
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def resolve_riot_id(arg_riot_id: Optional[str]) -> Optional[str]:
    """--riot-id 引数 → プロファイル → 対話入力 の順で解決し、解決後は保存。"""
    saved = coach_profile.get_riot_id()
    if arg_riot_id:
        # 明示指定はプロファイルへ保存
        if arg_riot_id != saved:
            coach_profile.set_riot_id(arg_riot_id)
        return arg_riot_id
    if saved:
        print(f"Using saved Riot ID: {saved}  (change with --riot-id)")
        return saved
    # 対話入力
    entered = input('Riot ID (Name#TAG): ').strip()
    if entered:
        coach_profile.set_riot_id(entered)
        return entered
    return None


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.WARNING if not args.debug else logging.DEBUG,
        format="%(levelname)s: %(message)s",
    )

    if not os.environ.get("RIOT_API_KEY"):
        print("ERROR: RIOT_API_KEY が未設定です（.env を確認）", file=sys.stderr)
        return 1

    riot_id = resolve_riot_id(args.riot_id)
    if not riot_id or "#" not in riot_id:
        print('ERROR: Riot ID は "Name#TAG" 形式で指定してください', file=sys.stderr)
        return 1

    name, tag = riot_id.split("#", 1)
    platform = os.environ.get("RIOT_PLATFORM", "jp1")

    with RiotAPIClient(platform=platform) as client:
        try:
            account = client.get_account_by_riot_id(name.strip(), tag.strip())
        except RiotAPIError as e:
            print(f"ERROR: account取得失敗: {e}", file=sys.stderr)
            return 1
        puuid = account["puuid"]

        # ターゲットランクを自動決定
        target_rank, rank_info = resolve_target_rank(client, puuid, args.rank)
        print(rank_info)

        # 全キューで最新N件
        match_ids = client.get_match_ids(puuid, count=args.count, queue=None)
        if not match_ids:
            print("試合履歴が見つかりません")
            return 0

        # 各マッチのサマリ取得
        rows: list[tuple[str, str]] = []  # (matchId, summary_line)
        print(f"\n直近 {len(match_ids)} 試合 (target rank: {target_rank}):\n")
        print(f"  {'#':>2}  {'date':16}  {'result':4}  {'queue':12}  {'champ':>13}  {'pos':>7}  {'KDA':>10}  CS")
        print("  " + "-" * 80)
        for i, mid in enumerate(match_ids, 1):
            try:
                match = client.get_match(mid)
            except RiotAPIError as e:
                print(f"  {i:>2}  (取得失敗 {e})")
                continue
            info = match["info"]
            me = next((p for p in info["participants"] if p["puuid"] == puuid), None)
            if not me:
                continue
            ts = datetime.fromtimestamp(info["gameCreation"] / 1000, tz=JST)
            cs = me.get("totalMinionsKilled", 0) + me.get("neutralMinionsKilled", 0)
            kda = f"{me['kills']}/{me['deaths']}/{me['assists']}"
            result = "WIN " if me.get("win") else "LOSS"
            pos = me.get("teamPosition") or "?"
            print(f"  {i:>2}  {ts.strftime('%Y-%m-%d %H:%M')}  {result}  "
                  f"{queue_label(info['queueId']):12}  {me['championName']:>13}  "
                  f"{pos:>7}  {kda:>10}  {cs}")
            rows.append((mid, kda))

        if not rows:
            print("\nレビュー可能な試合がありません")
            return 0

        # 選択
        print()
        sel_input = input(f"レビューする試合番号を選択 [1-{len(rows)}] (空欄=1): ").strip()
        try:
            sel = int(sel_input) if sel_input else 1
        except ValueError:
            print("無効な入力です")
            return 1
        if not (1 <= sel <= len(rows)):
            print(f"範囲外です (1-{len(rows)})")
            return 1
        chosen_mid = rows[sel - 1][0]

        # レビュー生成
        try:
            match = client.get_match(chosen_mid)
            timeline = client.get_match_timeline(chosen_mid)
        except RiotAPIError as e:
            print(f"ERROR: マッチ取得失敗: {e}", file=sys.stderr)
            return 1

        review = build_review(match, timeline, puuid, rank=target_rank)
        if not review:
            print("自プレイヤーが見つかりません")
            return 1

        # 前回KPIの達成評価
        kpi_results = coach_kpi.evaluate_kpis(review.stats.match_id, review.stats)
        if kpi_results:
            print("\n--- 前回KPI評価 ---")
            for r in kpi_results:
                mark = "[OK]" if r["achieved"] else "[NG]"
                print(f"  {mark} {r['kpi_type']}: target={r['target']} actual={r['actual']}")

        # コンソール簡易出力
        s = review.stats
        print(f"\n=== {s.champion} {s.duration_min}min "
              f"{'WIN' if s.win else 'LOSS'} ===")
        print(f"  KDA: {s.kills}/{s.deaths}/{s.assists}  CS: {s.cs_total} ({s.cs_per_min}/min)")
        print(f"  CS@10: {s.cs_at_10}  CS@15: {s.cs_at_15}  Vision: {s.vision_score}")
        print(f"  Lane: vs {s.enemy_adc}/{s.enemy_support}, with {s.my_support}")
        print(f"  Improvement points: {len(review.points)}")

        # LLMコメント
        comment = ""
        if args.llm:
            print("\nコーチコメント生成中（30〜60秒かかる場合があります）…")
            try:
                system, user = build_review_prompt(review)
                comment = coach_chat(system, user)
            except Exception as e:
                print(f"  LLM呼び出し失敗: {e}", file=sys.stderr)

        # 新KPIをLLMコメントから抽出して保存
        if comment:
            new_kpis = coach_kpi.parse_kpis(comment)
            saved = coach_kpi.save_kpis(s.match_id, new_kpis)
            if saved:
                print(f"  次試合用KPI {saved} 件を保存しました")

        # HTML出力
        path = open_review_in_browser(review, comment or None, prev_kpi_results=kpi_results)
        print(f"\nHTMLレビューをブラウザで開きました: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
