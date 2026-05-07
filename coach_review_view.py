"""
coach_review_view.py — 試合後レビューをNotion風HTMLボードでブラウザ表示

Reviewオブジェクト + 任意のLLMコメントから自己完結HTMLを生成し、
一時ファイルとして書き出してデフォルトブラウザで開く。

依存: 標準ライブラリのみ（html / tempfile / webbrowser / pathlib）。
"""

from __future__ import annotations

import html
import logging
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

from match_review import Review

logger = logging.getLogger(__name__)


SEVERITY_STYLE = {
    "critical": ("#ef5350", "重大"),
    "major":    ("#f5b942", "要改善"),
    "minor":    ("#5fa9d1", "余地あり"),
}

CATEGORY_ICON = {
    "cs":      "🌾",
    "deaths":  "💀",
    "vision":  "👁",
    "matchup": "⚔",
    "macro":   "🗺",
}


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _format_llm_comment(text: str) -> str:
    """LLM出力（プレーンテキスト/Markdownライク）を最低限HTMLにする。

    - 連続改行を <br> に
    - 番号付き行（1. / 2. / 3.）を見出し風に
    """
    if not text:
        return ""
    paragraphs = []
    for raw in text.split("\n\n"):
        block = _esc(raw.strip())
        if not block:
            continue
        block = block.replace("\n", "<br>")
        paragraphs.append(f"<p>{block}</p>")
    return "\n".join(paragraphs)


def _render_stat_card(label: str, value: str, target: Optional[str] = None,
                      good: Optional[bool] = None) -> str:
    badge = ""
    if good is True:
        badge = '<span class="badge ok">OK</span>'
    elif good is False:
        badge = '<span class="badge bad">改善</span>'
    target_html = f'<div class="target">target {_esc(target)}</div>' if target else ""
    return f"""
    <div class="stat-card">
      <div class="stat-label">{_esc(label)}</div>
      <div class="stat-value">{_esc(value)} {badge}</div>
      {target_html}
    </div>
    """


def _render_improvement(point) -> str:
    color, sev_label = SEVERITY_STYLE.get(point.severity, ("#8a8a8a", "info"))
    icon = CATEGORY_ICON.get(point.category, "•")
    return f"""
    <div class="improvement-card" style="border-left:4px solid {color}">
      <div class="imp-head">
        <span class="sev-badge" style="background:{color}">{sev_label}</span>
        <span class="cat-icon">{icon}</span>
        <span class="cat-name">{_esc(point.category)}</span>
      </div>
      <h3>{_esc(point.title)}</h3>
      <p class="detail">{_esc(point.detail)}</p>
      <div class="suggestion">→ {_esc(point.suggestion)}</div>
    </div>
    """


# ---------------------------------------------------------------------------
# HTML生成本体
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #191919;
  --bg-card: #252525;
  --bg-card-hover: #2d2d2d;
  --fg: #e8e8e8;
  --fg-muted: #9b9b9b;
  --accent: #5fd17a;
  --warn: #f5b942;
  --danger: #ef5350;
  --border: #333;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans",
               "Yu Gothic UI", Roboto, sans-serif;
  line-height: 1.55;
}
.container {
  max-width: 880px;
  margin: 0 auto;
  padding: 48px 32px 80px;
}
.header {
  border-bottom: 1px solid var(--border);
  padding-bottom: 24px;
  margin-bottom: 32px;
}
.header h1 {
  font-size: 36px;
  margin: 0 0 8px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.header .subtitle {
  color: var(--fg-muted);
  font-size: 15px;
}
.win-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  margin-left: 8px;
}
.win-badge.win { background: rgba(95,209,122,0.2); color: var(--accent); }
.win-badge.loss { background: rgba(239,83,80,0.2); color: var(--danger); }

.section {
  margin-top: 40px;
}
.section h2 {
  font-size: 20px;
  font-weight: 600;
  margin: 0 0 16px;
  color: var(--fg);
  display: flex;
  align-items: center;
  gap: 8px;
}
.section h2::before {
  content: "";
  width: 3px;
  height: 18px;
  background: var(--accent);
  border-radius: 2px;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;
}
.stat-card {
  background: var(--bg-card);
  border-radius: 8px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  transition: background 0.15s;
}
.stat-card:hover { background: var(--bg-card-hover); }
.stat-label {
  font-size: 11px;
  text-transform: uppercase;
  color: var(--fg-muted);
  letter-spacing: 0.05em;
  margin-bottom: 6px;
}
.stat-value {
  font-size: 22px;
  font-weight: 600;
  font-family: "SF Mono", Consolas, monospace;
}
.target {
  font-size: 11px;
  color: var(--fg-muted);
  margin-top: 4px;
}
.badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  margin-left: 6px;
  font-weight: 600;
  font-family: -apple-system, sans-serif;
}
.badge.ok { background: rgba(95,209,122,0.2); color: var(--accent); }
.badge.bad { background: rgba(245,185,66,0.2); color: var(--warn); }

.improvement-card {
  background: var(--bg-card);
  border-radius: 8px;
  padding: 16px 20px;
  margin-bottom: 12px;
  border: 1px solid var(--border);
}
.improvement-card:hover { background: var(--bg-card-hover); }
.imp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  margin-bottom: 8px;
}
.sev-badge {
  padding: 2px 8px;
  border-radius: 3px;
  color: #fff;
  font-weight: 600;
  font-size: 10px;
  text-transform: uppercase;
}
.cat-icon { font-size: 14px; }
.cat-name { color: var(--fg-muted); text-transform: uppercase; letter-spacing: 0.05em; }
.improvement-card h3 {
  font-size: 16px;
  margin: 4px 0 6px;
  font-weight: 600;
}
.improvement-card .detail {
  color: var(--fg-muted);
  font-size: 13px;
  margin: 0 0 10px;
  font-family: "SF Mono", Consolas, monospace;
}
.improvement-card .suggestion {
  background: rgba(95,209,122,0.08);
  border-left: 2px solid var(--accent);
  padding: 8px 12px;
  border-radius: 0 4px 4px 0;
  font-size: 14px;
  color: var(--fg);
}

.coach-comment {
  background: var(--bg-card);
  border-radius: 8px;
  padding: 24px 28px;
  border: 1px solid var(--border);
}
.coach-comment p {
  margin: 0 0 12px;
  font-size: 15px;
}
.coach-comment p:last-child { margin-bottom: 0; }

.deaths-list {
  font-family: "SF Mono", Consolas, monospace;
  font-size: 13px;
  color: var(--fg-muted);
}
.deaths-list span {
  display: inline-block;
  background: rgba(239,83,80,0.15);
  padding: 3px 8px;
  border-radius: 3px;
  margin-right: 6px;
  margin-bottom: 4px;
}

footer {
  margin-top: 60px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
  color: var(--fg-muted);
  font-size: 12px;
  text-align: center;
}
"""


def render_review_html(review: Review, llm_comment: Optional[str] = None) -> str:
    s = review.stats
    bm = review.benchmark

    win_class = "win" if s.win else "loss"
    win_label = "WIN" if s.win else "LOSS"

    # CS@10 比較
    cs10_target = bm.get("cs_at_10")
    cs10_good = (cs10_target is not None) and (s.cs_at_10 >= cs10_target)
    cs15_target = bm.get("cs_at_15")
    cs15_good = (cs15_target is not None) and (s.cs_at_15 >= cs15_target)
    csm_target = bm.get("cs_per_min")
    csm_good = (csm_target is not None) and (s.cs_per_min >= csm_target)
    deaths_max = bm.get("deaths_max")
    deaths_good = (deaths_max is not None) and (s.deaths <= deaths_max)
    vs_target = bm.get("vision_score_min")
    vs_good = (vs_target is not None) and (s.vision_score >= vs_target)
    kda_target = bm.get("kda")
    kda_good = (kda_target is not None) and (s.kda >= kda_target)

    stats_grid = (
        _render_stat_card("KDA", f"{s.kills}/{s.deaths}/{s.assists}",
                          target=str(kda_target) if kda_target else None, good=kda_good)
        + _render_stat_card("KDA Ratio", f"{s.kda:.2f}",
                            target=str(kda_target) if kda_target else None, good=kda_good)
        + _render_stat_card("CS @10min", str(s.cs_at_10),
                            target=str(cs10_target) if cs10_target else None, good=cs10_good)
        + _render_stat_card("CS @15min", str(s.cs_at_15),
                            target=str(cs15_target) if cs15_target else None, good=cs15_good)
        + _render_stat_card("CS / min", str(s.cs_per_min),
                            target=str(csm_target) if csm_target else None, good=csm_good)
        + _render_stat_card("CS Total", str(s.cs_total))
        + _render_stat_card("Deaths", str(s.deaths),
                            target=f"≤ {deaths_max}" if deaths_max else None, good=deaths_good)
        + _render_stat_card("Vision Score", str(s.vision_score),
                            target=f"≥ {vs_target}" if vs_target else None, good=vs_good)
    )

    deaths_html = ""
    if s.death_timestamps_min:
        spans = "".join(f"<span>{t}min</span>" for t in s.death_timestamps_min)
        deaths_html = f'<div class="section"><h2>Death Timestamps</h2><div class="deaths-list">{spans}</div></div>'

    if review.points:
        improvements_html = "\n".join(_render_improvement(p) for p in review.points)
    else:
        improvements_html = '<p style="color:var(--fg-muted)">ルールベース検出での改善ポイントはありません。</p>'

    coach_section = ""
    if llm_comment:
        coach_section = f"""
        <div class="section">
          <h2>Coach Comment</h2>
          <div class="coach-comment">
            {_format_llm_comment(llm_comment)}
          </div>
        </div>
        """

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>ADC Coach Review — {_esc(s.match_id)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

  <header class="header">
    <h1>{_esc(s.champion)} <span class="win-badge {win_class}">{win_label}</span></h1>
    <div class="subtitle">
      Match {_esc(s.match_id)} ・ {s.duration_min}分 ・
      vs {_esc(s.enemy_adc or "?")} / {_esc(s.enemy_support or "?")} ・
      with {_esc(s.my_support or "?")} ・
      target rank: {_esc(review.target_rank)}
    </div>
  </header>

  <div class="section">
    <h2>Stats vs Benchmark</h2>
    <div class="stats-grid">
      {stats_grid}
    </div>
  </div>

  {deaths_html}

  <div class="section">
    <h2>Improvement Points</h2>
    {improvements_html}
  </div>

  {coach_section}

  <footer>
    Generated {timestamp} · LoL ADC Coach
  </footer>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 公開関数: ファイル出力 + ブラウザオープン
# ---------------------------------------------------------------------------

def write_review_html(review: Review, llm_comment: Optional[str] = None,
                      out_dir: Optional[Path] = None) -> Path:
    """HTMLを書き出してパスを返す（ブラウザは開かない）"""
    html_text = render_review_html(review, llm_comment)
    out_dir = out_dir or Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c for c in review.stats.match_id if c.isalnum() or c in "_-")
    path = out_dir / f"adc_review_{safe_id or 'latest'}.html"
    path.write_text(html_text, encoding="utf-8")
    return path


def open_review_in_browser(review: Review, llm_comment: Optional[str] = None,
                           out_dir: Optional[Path] = None) -> Path:
    """HTML生成 → デフォルトブラウザで開く。書き出し先パスを返す。"""
    path = write_review_html(review, llm_comment, out_dir)
    webbrowser.open(path.as_uri())
    return path


# ---------------------------------------------------------------------------
# スタンドアロン: ダミーレビューでHTML確認
# ---------------------------------------------------------------------------

def _demo() -> None:
    from match_review import MatchStats, ImprovementPoint, Review as R
    stats = MatchStats(
        match_id="JP1_DEMO",
        champion="Caitlyn",
        win=False,
        duration_min=30.0,
        kills=7, deaths=6, assists=4,
        cs_total=155, cs_at_10=50, cs_at_15=90,
        cs_per_min=5.17, vision_score=18,
        death_timestamps_min=[8.0, 12.0, 17.5, 22.0, 26.0, 28.5],
        enemy_adc="Lucian", enemy_support="Thresh", my_support="Lulu",
    )
    points = [
        ImprovementPoint(
            category="cs", severity="critical",
            title="CS@10が基準を大幅に下回る（50 / 目標70）",
            detail="差: -20",
            suggestion="ラストヒットの基礎練習を1日10分。プラクティスツールでミニオン全取りを目指す。",
        ),
        ImprovementPoint(
            category="matchup", severity="major",
            title="不利マッチアップ Caitlyn vs Lucian で過剰なデス",
            detail="マッチアップ評価: -1",
            suggestion="Lv2でLucian側が強い。Lv2を先に取らせない立ち回りを。",
        ),
        ImprovementPoint(
            category="vision", severity="minor",
            title="視界スコアが低い（18 / 目標20+）",
            detail="コントロールワード購入とトリンケット切れ目を意識する。",
            suggestion="ベースに戻る度にコントロールワードを買い、敵ジャングル侵入時に置く習慣をつける。",
        ),
    ]
    review = R(stats=stats, target_rank="GOLD",
               benchmark={"cs_per_min": 6.5, "cs_at_10": 70, "cs_at_15": 105,
                          "kda": 2.0, "deaths_max": 6, "vision_score_min": 20},
               points=points)
    sample_comment = (
        "1. 【何を変えるか】CS@10で20差を埋める。レーニング3分目までは対面と関係なくCSのみ意識する。\n"
        "2. 【なぜそれが最優先か】CS差20=金1500近くの差。中盤の戦闘力差に直結する最大の損失。\n"
        "3. 【次の試合での具体アクション】プラクティスツールで10分80CSを3回連続達成してから次ランク戦に入る。\n\n"
        "次試合の最優先KPI: CS@10 ≥ 65"
    )
    path = open_review_in_browser(review, sample_comment)
    print(f"Opened: {path}")


if __name__ == "__main__":
    _demo()
