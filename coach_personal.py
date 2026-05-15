"""
coach_personal.py — 個人ベンチマーク機能

自分の直近N試合を集計し、個人ベースラインを `.coach_personal.json` に保存。
レビュー時に「ランク基準」と「個人基準」両方と比較できるようにする。

CLI:
    python coach_personal.py            # 直近30試合で再計算
    python coach_personal.py --count 50 # 直近50試合で再計算
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
from datetime import datetime, timezone
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

from riot_api import RiotAPIClient, RiotAPIError
from match_review import build_review
import coach_profile

logger = logging.getLogger(__name__)


PERSONAL_PATH = Path(__file__).parent / ".coach_personal.json"
DEFAULT_COUNT = 30


# ---------------------------------------------------------------------------
# 計算
# ---------------------------------------------------------------------------

def compute_personal_benchmark(client: RiotAPIClient, puuid: str,
                                count: int = DEFAULT_COUNT,
                                queue: Optional[int] = None) -> dict:
    """直近N試合のADC試合のみを集計してベンチマーク dict を返す"""
    match_ids = client.get_match_ids(puuid, count=count, queue=queue)
    if not match_ids:
        return {}

    # 並列 + キャッシュで一気に取得
    matches = client.get_matches_parallel(match_ids)
    timelines = client.get_timelines_parallel(match_ids)

    statlist = []
    skipped = 0
    for mid, match, timeline in zip(match_ids, matches, timelines):
        if not match or not timeline:
            skipped += 1
            continue
        review = build_review(match, timeline, puuid)
        if not review:
            skipped += 1
            continue
        s = review.stats
        me = next(
            (p for p in match["info"]["participants"] if p["puuid"] == puuid),
            None,
        )
        if me and (me.get("teamPosition") or "").upper() != "BOTTOM":
            skipped += 1
            continue
        statlist.append(s)

    if not statlist:
        return {"sample_count": 0, "skipped": skipped}

    return {
        "computed_at":      datetime.now(timezone.utc).isoformat(),
        "sample_count":     len(statlist),
        "skipped":          skipped,
        "win_rate":         round(sum(1 for s in statlist if s.win) / len(statlist), 3),
        "cs_per_min":       round(statistics.mean(s.cs_per_min for s in statlist), 2),
        "cs_at_10":         round(statistics.mean(s.cs_at_10 for s in statlist), 1),
        "cs_at_15":         round(statistics.mean(s.cs_at_15 for s in statlist), 1),
        "kda":              round(statistics.mean(s.kda for s in statlist), 2),
        "deaths_avg":       round(statistics.mean(s.deaths for s in statlist), 2),
        "vision_score":     round(statistics.mean(s.vision_score for s in statlist), 1),
        "damage_share":     round(statistics.mean(s.damage_share for s in statlist), 3),
        "gold_diff_at_15":  round(statistics.mean(s.gold_diff_at_15 for s in statlist), 0),
        "objective_damage_share": round(statistics.mean(s.objective_damage_share for s in statlist), 3),
    }


def save_personal(data: dict) -> None:
    PERSONAL_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_personal() -> dict:
    if not PERSONAL_PATH.exists():
        return {}
    try:
        return json.loads(PERSONAL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def gap_to_rank(personal: dict, rank_benchmark: dict) -> dict:
    """個人 vs ランク目標の差分。プラスは超過、マイナスは未達。"""
    keys = ["cs_per_min", "cs_at_10", "cs_at_15", "kda",
            "vision_score", "damage_share", "gold_diff_at_15",
            "objective_damage_share"]
    out = {}
    for k in keys:
        if k in personal and k in rank_benchmark:
            out[k] = round(personal[k] - rank_benchmark[k], 3)
    if "deaths_avg" in personal and "deaths_max" in rank_benchmark:
        # deaths は少ない方が良いので符号反転
        out["deaths_vs_max"] = round(rank_benchmark["deaths_max"] - personal["deaths_avg"], 2)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="個人ベンチマークを再計算")
    p.add_argument("--riot-id", help='Riot ID "Name#TAG"。未指定時はプロファイル読み込み')
    p.add_argument("--count", type=int, default=DEFAULT_COUNT,
                   help=f"集計する直近試合数（既定 {DEFAULT_COUNT}）")
    p.add_argument("--queue", type=int, default=None,
                   help="キュー絞り（420=ranked solo, 未指定=全キュー）")
    p.add_argument("--platform", default=os.environ.get("RIOT_PLATFORM", "jp1"))
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not os.environ.get("RIOT_API_KEY"):
        print("ERROR: RIOT_API_KEY 未設定", file=sys.stderr)
        return 1

    riot_id = args.riot_id or coach_profile.get_riot_id()
    if not riot_id or "#" not in riot_id:
        print('ERROR: Riot ID "Name#TAG" を --riot-id か プロファイルで指定', file=sys.stderr)
        return 1

    name, tag = riot_id.split("#", 1)
    print(f"集計中… target={riot_id} count={args.count} (時間がかかります)")

    with RiotAPIClient(platform=args.platform) as c:
        try:
            account = c.get_account_by_riot_id(name.strip(), tag.strip())
        except RiotAPIError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

        data = compute_personal_benchmark(c, account["puuid"],
                                           count=args.count, queue=args.queue)

    if not data or data.get("sample_count", 0) == 0:
        print("有効な試合がありませんでした")
        return 1

    save_personal(data)

    print()
    print("=== 個人ベンチマーク（あなたの直近の実力） ===")
    print(f"  Sample:        {data['sample_count']} games (skipped {data['skipped']})")
    print(f"  Win Rate:      {int(data['win_rate'] * 100)}%")
    print(f"  CS/min:        {data['cs_per_min']}")
    print(f"  CS@10 / @15:   {data['cs_at_10']} / {data['cs_at_15']}")
    print(f"  KDA:           {data['kda']}")
    print(f"  Avg Deaths:    {data['deaths_avg']}")
    print(f"  Vision Score:  {data['vision_score']}")
    print(f"  DMG Share:     {int(data['damage_share'] * 100)}%")
    print(f"  Gold Δ@15:     {int(data['gold_diff_at_15']):+d}")
    print()
    print(f"Saved to: {PERSONAL_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
