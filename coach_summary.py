"""
coach_summary.py — 複数試合サマリレビュー

直近N試合の MatchStats を集計して、傾向を可視化する。
- 平均 CS/min, KDA, デス, 視界
- 勝率
- チャンプ別パフォーマンス
- LS派 / Curtis派 指標の推移グラフ
"""

from __future__ import annotations

import html
import json as _json
import logging
import statistics
import tempfile
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from match_review import MatchStats

logger = logging.getLogger(__name__)


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


@dataclass
class MultiMatchSummary:
    matches: list[MatchStats] = field(default_factory=list)
    target_rank: str = "GOLD"
    benchmark: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.matches)

    @property
    def win_rate(self) -> float:
        if not self.matches:
            return 0.0
        return sum(1 for m in self.matches if m.win) / len(self.matches)

    @property
    def avg_cs_per_min(self) -> float:
        return statistics.mean(m.cs_per_min for m in self.matches) if self.matches else 0.0

    @property
    def avg_cs_at_10(self) -> float:
        return statistics.mean(m.cs_at_10 for m in self.matches) if self.matches else 0.0

    @property
    def avg_kda(self) -> float:
        return statistics.mean(m.kda for m in self.matches) if self.matches else 0.0

    @property
    def avg_deaths(self) -> float:
        return statistics.mean(m.deaths for m in self.matches) if self.matches else 0.0

    @property
    def avg_vision(self) -> float:
        return statistics.mean(m.vision_score for m in self.matches) if self.matches else 0.0

    @property
    def avg_dmg_share(self) -> float:
        return statistics.mean(m.damage_share for m in self.matches) if self.matches else 0.0

    @property
    def avg_gold_diff_15(self) -> float:
        return statistics.mean(m.gold_diff_at_15 for m in self.matches) if self.matches else 0.0

    @property
    def champion_breakdown(self) -> dict[str, dict]:
        """チャンプ別: {champ: {"games": N, "wins": M, "avg_kda": ...}}"""
        out: dict[str, dict] = {}
        for m in self.matches:
            c = out.setdefault(m.champion, {"games": 0, "wins": 0, "kdas": [], "cs_per_min": []})
            c["games"] += 1
            if m.win:
                c["wins"] += 1
            c["kdas"].append(m.kda)
            c["cs_per_min"].append(m.cs_per_min)
        # 統計化
        for c in out.values():
            c["avg_kda"] = round(statistics.mean(c["kdas"]), 2) if c["kdas"] else 0
            c["avg_cs_per_min"] = round(statistics.mean(c["cs_per_min"]), 2) if c["cs_per_min"] else 0
            c["win_rate"] = c["wins"] / max(1, c["games"])
            del c["kdas"], c["cs_per_min"]
        return out


# ---------------------------------------------------------------------------
# HTML render
# ---------------------------------------------------------------------------

SUMMARY_CSS = """
:root {
  --bg: #191919; --bg-card: #252525; --fg: #e8e8e8; --fg-muted: #9b9b9b;
  --accent: #5fd17a; --warn: #f5b942; --danger: #ef5350; --border: #333;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans",
               "Yu Gothic UI", Roboto, sans-serif;
  line-height: 1.55;
}
.container { max-width: 980px; margin: 0 auto; padding: 48px 32px 80px; }
.header { border-bottom: 1px solid var(--border); padding-bottom: 24px; margin-bottom: 32px; }
.header h1 { font-size: 32px; margin: 0 0 8px; font-weight: 700; }
.header .subtitle { color: var(--fg-muted); font-size: 14px; }

.section { margin-top: 36px; }
.section h2 {
  font-size: 18px; font-weight: 600; margin: 0 0 14px;
  display: flex; align-items: center; gap: 8px;
}
.section h2::before {
  content: ""; width: 3px; height: 16px; background: var(--accent); border-radius: 2px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;
}
.s-card {
  background: var(--bg-card); border-radius: 8px; padding: 14px 18px;
  border: 1px solid var(--border);
}
.s-label {
  font-size: 10px; text-transform: uppercase; color: var(--fg-muted);
  letter-spacing: 0.05em; margin-bottom: 6px;
}
.s-value {
  font-size: 22px; font-weight: 600; font-family: "SF Mono", Consolas, monospace;
}
.s-target { font-size: 11px; color: var(--fg-muted); margin-top: 4px; }
.s-card.bad { border-left: 3px solid var(--danger); }
.s-card.warn { border-left: 3px solid var(--warn); }
.s-card.ok { border-left: 3px solid var(--accent); }

.matches-table {
  width: 100%; border-collapse: collapse; background: var(--bg-card);
  border-radius: 8px; overflow: hidden; border: 1px solid var(--border);
  font-size: 13px;
}
.matches-table th, .matches-table td {
  padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border);
}
.matches-table th {
  background: rgba(255,255,255,0.03); color: var(--fg-muted);
  font-weight: 600; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.05em;
}
.matches-table tr:last-child td { border-bottom: none; }
.matches-table .win { color: var(--accent); font-weight: 600; }
.matches-table .loss { color: var(--danger); font-weight: 600; }
.matches-table td.num { font-family: "SF Mono", Consolas, monospace; text-align: right; }

.champ-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px;
}
.champ-card {
  background: var(--bg-card); border-radius: 8px; padding: 14px 18px;
  border: 1px solid var(--border);
}
.champ-name { font-size: 16px; font-weight: 600; margin-bottom: 4px; }
.champ-stats { font-size: 12px; color: var(--fg-muted); font-family: "SF Mono", Consolas, monospace; }

.chart-card {
  background: var(--bg-card); border-radius: 8px; padding: 20px;
  border: 1px solid var(--border);
}
.chart-card canvas { width: 100% !important; height: 240px !important; }

footer {
  margin-top: 60px; padding-top: 20px; border-top: 1px solid var(--border);
  color: var(--fg-muted); font-size: 12px; text-align: center;
}
"""


def _eval_card(actual: float, target: float, lower_is_better: bool = False,
                tolerance: float = 0.05) -> str:
    """ベンチマーク差で OK/warn/bad のクラスを返す"""
    if not target:
        return ""
    delta_ratio = (actual - target) / target
    if lower_is_better:
        delta_ratio = -delta_ratio
    if delta_ratio >= tolerance:
        return "ok"
    if delta_ratio >= -tolerance:
        return "warn"
    return "bad"


def render_summary_html(summary: MultiMatchSummary) -> str:
    bm = summary.benchmark or {}
    if not summary.matches:
        return "<html><body>No matches</body></html>"

    # サマリカード
    cards = []
    cards.append((
        "Win Rate",
        f"{int(summary.win_rate * 100)}%",
        f"{summary.matches[0].duration_min}min avg",
        _eval_card(summary.win_rate, 0.5),
    ))
    cards.append((
        "CS / min",
        f"{summary.avg_cs_per_min:.2f}",
        f"target {bm.get('cs_per_min', '?')}",
        _eval_card(summary.avg_cs_per_min, bm.get("cs_per_min", 7.0)),
    ))
    cards.append((
        "CS @10",
        f"{summary.avg_cs_at_10:.0f}",
        f"target {bm.get('cs_at_10', '?')}",
        _eval_card(summary.avg_cs_at_10, bm.get("cs_at_10", 75)),
    ))
    cards.append((
        "KDA",
        f"{summary.avg_kda:.2f}",
        f"target {bm.get('kda', '?')}",
        _eval_card(summary.avg_kda, bm.get("kda", 2.0)),
    ))
    cards.append((
        "Avg Deaths",
        f"{summary.avg_deaths:.1f}",
        f"max {bm.get('deaths_max', '?')}",
        _eval_card(summary.avg_deaths, bm.get("deaths_max", 5), lower_is_better=True),
    ))
    cards.append((
        "Vision",
        f"{summary.avg_vision:.0f}",
        f"min {bm.get('vision_score_min', '?')}",
        _eval_card(summary.avg_vision, bm.get("vision_score_min", 22)),
    ))
    cards.append((
        "DMG Share",
        f"{int(summary.avg_dmg_share * 100)}%",
        f"target {int(bm.get('damage_share', 0.27) * 100)}%",
        _eval_card(summary.avg_dmg_share, bm.get("damage_share", 0.27)),
    ))
    cards.append((
        "Gold Δ@15",
        f"{int(summary.avg_gold_diff_15):+d}",
        f"target {bm.get('gold_diff_at_15', '?'):+d}" if bm.get('gold_diff_at_15') else "",
        _eval_card(summary.avg_gold_diff_15, max(1, abs(bm.get("gold_diff_at_15", 100)))),
    ))

    cards_html = "".join(
        f'<div class="s-card {cls}">'
        f'<div class="s-label">{_esc(label)}</div>'
        f'<div class="s-value">{_esc(value)}</div>'
        f'<div class="s-target">{_esc(target)}</div>'
        f'</div>'
        for label, value, target, cls in cards
    )

    # 試合一覧テーブル
    rows = []
    for i, m in enumerate(summary.matches, 1):
        result_cls = "win" if m.win else "loss"
        result_label = "WIN" if m.win else "LOSS"
        rows.append(f"""
        <tr>
          <td>{i}</td>
          <td>{_esc(m.champion)}</td>
          <td class="{result_cls}">{result_label}</td>
          <td class="num">{m.kills}/{m.deaths}/{m.assists}</td>
          <td class="num">{m.kda:.2f}</td>
          <td class="num">{m.cs_at_10}</td>
          <td class="num">{m.cs_per_min}</td>
          <td class="num">{m.vision_score}</td>
          <td>vs {_esc(m.enemy_adc or "?")}</td>
        </tr>
        """)
    table_html = f"""
    <table class="matches-table">
      <thead>
        <tr>
          <th>#</th><th>Champion</th><th>Result</th>
          <th>KDA</th><th>Ratio</th>
          <th>CS@10</th><th>CS/min</th><th>Vision</th>
          <th>Lane vs</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """

    # チャンプ別
    champ_html = ""
    for c, st in sorted(summary.champion_breakdown.items(), key=lambda x: -x[1]["games"]):
        champ_html += f"""
        <div class="champ-card">
          <div class="champ-name">{_esc(c)}</div>
          <div class="champ-stats">
            {st["games"]} games · {int(st["win_rate"]*100)}% WR<br>
            KDA {st["avg_kda"]:.2f} · CS/min {st["avg_cs_per_min"]:.2f}
          </div>
        </div>
        """

    # 推移グラフ用データ
    chart_data = {
        "labels": list(range(1, summary.n + 1)),
        "champion": [m.champion for m in summary.matches],
        "cs_per_min": [m.cs_per_min for m in summary.matches],
        "kda": [round(m.kda, 2) for m in summary.matches],
        "deaths": [m.deaths for m in summary.matches],
        "vision": [m.vision_score for m in summary.matches],
        "cs_target": bm.get("cs_per_min", 7.0),
        "kda_target": bm.get("kda", 2.0),
        "vision_target": bm.get("vision_score_min", 22),
    }

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="UTF-8">
<title>ADC Coach Summary — {summary.n} matches</title>
<style>{SUMMARY_CSS}</style>
</head><body>
<div class="container">

  <header class="header">
    <h1>直近 {summary.n} 試合サマリ</h1>
    <div class="subtitle">target rank: {_esc(summary.target_rank)} ・ LS+Curtis 哲学 ・ generated {timestamp}</div>
  </header>

  <div class="section">
    <h2>Aggregate Stats vs Benchmark</h2>
    <div class="summary-grid">{cards_html}</div>
  </div>

  <div class="section">
    <h2>Match List</h2>
    {table_html}
  </div>

  <div class="section">
    <h2>Trend</h2>
    <div class="chart-card">
      <canvas id="trendChart"></canvas>
    </div>
  </div>

  <div class="section">
    <h2>Champion Breakdown</h2>
    <div class="champ-grid">{champ_html}</div>
  </div>

  <footer>Generated {timestamp} · LoL ADC Coach</footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
(function() {{
  var d = {_json.dumps(chart_data)};
  var ctx = document.getElementById('trendChart');
  if (!ctx) return;
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: d.labels,
      datasets: [
        {{ label: 'CS/min', data: d.cs_per_min, borderColor: '#5fd17a', backgroundColor: 'rgba(95,209,122,0.1)', yAxisID: 'y', tension: 0.25 }},
        {{ label: 'KDA',    data: d.kda,         borderColor: '#f5b942', backgroundColor: 'rgba(245,185,66,0.1)', yAxisID: 'y', tension: 0.25 }},
        {{ label: 'Vision', data: d.vision,      borderColor: '#5fa9d1', backgroundColor: 'rgba(95,169,209,0.1)', yAxisID: 'y2', tension: 0.25, borderDash: [4,4] }},
        {{ label: 'Deaths', data: d.deaths,      borderColor: '#ef5350', backgroundColor: 'rgba(239,83,80,0.1)', yAxisID: 'y', tension: 0.25, borderDash: [2,4] }}
      ]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ labels: {{ color: '#e8e8e8' }} }} }},
      scales: {{
        x: {{ ticks: {{ color: '#9b9b9b' }}, grid: {{ color: '#2a2a2a' }},
              title: {{ display: true, text: 'match #', color: '#9b9b9b' }} }},
        y:  {{ position: 'left', ticks: {{ color: '#9b9b9b' }}, grid: {{ color: '#2a2a2a' }} }},
        y2: {{ position: 'right', ticks: {{ color: '#9b9b9b' }}, grid: {{ display: false }} }}
      }},
      interaction: {{ mode: 'index', intersect: false }}
    }}
  }});
}})();
</script>
</body></html>
"""


def write_summary_html(summary: MultiMatchSummary, out_dir: Optional[Path] = None) -> Path:
    out_dir = out_dir or Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"adc_summary_{summary.n}games.html"
    path.write_text(render_summary_html(summary), encoding="utf-8")
    return path


def open_summary_in_browser(summary: MultiMatchSummary, out_dir: Optional[Path] = None) -> Path:
    path = write_summary_html(summary, out_dir)
    webbrowser.open(path.as_uri())
    return path
