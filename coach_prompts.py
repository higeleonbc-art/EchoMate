"""
coach_prompts.py — ADCコーチ用プロンプトテンプレート (LS + Curtis 複合スタイル)

哲学: LS派ミクロ（CS・trade・positioning は non-negotiable）を前提にしつつ、
Curtis派マクロ（wave management / tempo / objective / vision）を主鍛錬対象として
コーチングする。詳細は memory/feedback_coaching_philosophy.md 参照。

data/coaches/{ls,curtis}.json に要約コーパス（coach_corpus.py 生成）が
存在する場合、system prompt 末尾に「影響を受けたコーチの実発言/原則」
セクションを動的に追加し、忠実度を上げる。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from match_review import Review

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# コーチコーパス読み込み (coach_corpus.py 生成物)
# ---------------------------------------------------------------------------

_CORPUS_DIR = Path(__file__).parent / "data" / "coaches"


def _load_coach_corpus() -> dict:
    """data/coaches/*.json を全てロード（_で始まるものとsources.jsonは除外）。

    新コーチを追加したい場合は data/coaches/{coach_key}.json を置くだけで自動認識される。
    """
    out: dict = {}
    if not _CORPUS_DIR.exists():
        return out
    for path in sorted(_CORPUS_DIR.glob("*.json")):
        name = path.stem
        if name.startswith("_") or name == "sources":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("phrases") or data.get("principles") or data.get("tactics"):
                out[name] = data
        except Exception as e:
            logger.warning("Failed to load %s: %s", path, e)
    return out


def _format_corpus_section(corpus: dict) -> str:
    """ロード済みコーパスを system prompt 用テキストに整形"""
    if not corpus:
        return ""
    lines = ["", "## 影響を受けたコーチの実発言・原則（要約・参考用）",
             "以下は実際のコーチ動画 transcript から抽出した要約。",
             "そのまま引用するな。あなた自身の言葉で再表現すること。"]
    for name, data in corpus.items():
        meta = data.get("_meta", {})
        coach_label = meta.get("coach_name", name.upper())
        lines.append(f"\n### {coach_label}")

        phrases = data.get("phrases") or []
        if phrases:
            lines.append("**典型的フレーズ:**")
            for p in phrases[:6]:
                quote = (p.get("quote") or "").strip()
                if quote:
                    lines.append(f'- "{quote}"')

        principles = data.get("principles") or []
        if principles:
            lines.append("**指導原則:**")
            for pr in principles[:4]:
                pname = (pr.get("name") or "").strip()
                psum = (pr.get("summary") or "").strip()
                if pname:
                    lines.append(f"- **{pname}**: {psum}")

        tactics = data.get("tactics") or []
        if tactics:
            lines.append("**具体戦術:**")
            for t in tactics[:5]:
                sc = (t.get("scenario") or "").strip()
                act = (t.get("action") or "").strip()
                if sc:
                    lines.append(f"- {sc} → {act}")
    return "\n".join(lines)


_COACH_CORPUS = _load_coach_corpus()
_COACH_CORPUS_SECTION = _format_corpus_section(_COACH_CORPUS)


# ---------------------------------------------------------------------------
# System prompt 本体
# ---------------------------------------------------------------------------

_BASE_SYSTEM_PROMPT = """\
あなたは LS / Tele / Coach Curtis の哲学を融合した League of Legends ADC専門コーチです。
プレイヤーをマスター帯（上位0.5%）に到達させることが最終目標です。

## 指導哲学（3軸）

**LS派（汎用ミクロ・基礎ライン）— Non-negotiable**
- ファーム最優先: CS取れない人はランク上がる資格無し
- 基礎徹底: ラストヒット精度・トレード判断・ポジショニングに妥協しない
- 辛口・断定的: 「お前のここが出来てない」と直接指摘する
- トレードは数学: HP/spell/cooldown を計算して優位を取る

**Tele派（チャンプ別ミクロ・mechanical execution）— specialism**
- チャンプ specific mastery: コンボ・パワースパイク・スキル数値を完璧に把握
- スキル accuracy > damage maximization: 当てない火力は0
- アニメーションキャンセルでDPS最大化（AA→Q→AA→W）
- レベル毎にスキル使用パターンを切り替え（Lv1とLv5は別物）
- アイテム選択は敵構成で動的判断（固定ビルドはNG）

**Curtis派（マクロ・主鍛錬対象）— ここを伸ばすのがコーチの仕事**
- wave management 80%: laning の8割は wave manipulation
- tempo creation: テンポは奪うものではなく作るもの
- objective control: 視界 → 情報 → 判断
- 教育的・論理的: 必ず「なぜそうするか」の根拠を示す

## コーチングの優先順位ルール（重要）

1. **ミクロ基礎が致命的に崩壊**（CS@10 < target-15 / 早期ソロデス3+）
   → LS流で **まずミクロから矯正**。マクロ話は後回し
2. **チャンプ運用ミス**（コンボ抜け・パワースパイクで攻めない・ult素出し）
   → Tele流で **チャンプ specific な改善** を提示（具体コンボ・タイミング・アイテム選択）
3. **基礎・運用OKならマクロ磨き**
   → Curtis流で **wave/tempo/objective/vision** を主軸に伸ばす
4. **全体良好ならメタ視点**
   → マッチアップ別の立ち回り・ビルド分岐・サポートシナジー

## 口調

- 辛口だが必ず**論理的根拠を示す**: 「君のXが出来てない。データはこう（Y）。次はZをやれ」
- データドリブン: 数値で語る。曖昧な精神論は禁止
- 簡潔・直截的: 励ましより具体アクション
- プレイヤーのメンタルケアは最低限。本気でマスターを目指す前提

## 禁止事項

- 「いいね！」「頑張って！」のような励ましだけで終わらせない
- 曖昧な精神論（「集中力を保とう」だけ等）
- ロール分担の責任転嫁（「ジャングルが…」のみで自己改善を書かない）
- 改善案の根拠を示さない一方的な命令
- LS / Tele / Curtis の名前を直接出すこと（影響を受けたコーチであり名乗りではない）
- 思考過程やメタ説明を出力すること（最終回答のみ）
"""

# 実発言コーパスを末尾に注入（あれば）
COACH_SYSTEM_PROMPT = _BASE_SYSTEM_PROMPT + _COACH_CORPUS_SECTION


def review_to_brief(review: Review) -> str:
    """Reviewオブジェクトをコーチプロンプト用の事実列に整形"""
    s = review.stats
    bm = review.benchmark
    lines = [
        f"## 試合データ",
        f"- チャンプ: {s.champion}（vs {s.enemy_adc} / sup相手 {s.enemy_support} / 自sup {s.my_support}）",
        f"- 試合時間: {s.duration_min}分 / 結果: {'勝利' if s.win else '敗北'}",
        f"- KDA: {s.kills}/{s.deaths}/{s.assists}（KDA={s.kda:.2f}）",
        f"- CS@10: {s.cs_at_10} / CS@15: {s.cs_at_15} / CS総数: {s.cs_total}",
        f"- CS/min: {s.cs_per_min}",
        f"- 視界スコア: {s.vision_score}",
        f"- デス時刻（分）: {s.death_timestamps_min}",
        f"",
        f"## 目標ランク {review.target_rank} のベンチマーク",
        f"- CS/min目標: {bm.get('cs_per_min')} (LS厳しめライン)",
        f"- CS@10目標: {bm.get('cs_at_10')} / CS@15目標: {bm.get('cs_at_15')}",
        f"- KDA目標: {bm.get('kda')}",
        f"- デス上限: {bm.get('deaths_max')}",
        f"- 視界スコア最低: {bm.get('vision_score_min')} (Curtis重視)",
    ]
    return "\n".join(lines)


def _classify_micro_macro(review: Review) -> str:
    """ミクロ崩壊 / Tele派mechanical / マクロ磨き のいずれの段階かを判定"""
    s = review.stats
    bm = review.benchmark
    cs10_target = bm.get("cs_at_10", 70)
    deaths_max = bm.get("deaths_max", 6)
    early_deaths = sum(1 for t in s.death_timestamps_min if t < 10)

    micro_critical = s.cs_at_10 < cs10_target - 15 or early_deaths >= 3 or s.deaths > deaths_max + 3
    if micro_critical:
        return "MICRO_CRITICAL"  # LS流で基礎ミクロ矯正

    cs_okay = s.cs_at_10 >= cs10_target - 5 and s.cs_per_min >= bm.get("cs_per_min", 7) - 1.0
    if not cs_okay or s.deaths > deaths_max:
        return "MIXED"  # ミクロ系の課題が残る → LS+Tele 寄り

    # CS/death 合格圏 → ダメージシェアやvisionでチャンプ運用を疑う or マクロ磨き
    dmg_share_target = bm.get("damage_share", 0.27)
    if s.damage_share < dmg_share_target - 0.05:
        return "TELE_FOCUS"  # ダメージ寄与不足 → チャンプ運用ミス（コンボ・パワースパイク）
    return "MACRO_FOCUS"  # Curtis流マクロ磨き


def build_review_prompt(review: Review) -> tuple[str, str]:
    """(system, user) のプロンプトペアを返す。

    LS+Curtis複合: ミクロ崩壊なら矯正、合格ならマクロ強化に振る。
    """
    rule_findings = "\n".join(
        f"- [{p.severity}/{p.category}] {p.title} → {p.suggestion}"
        for p in review.points
    ) or "- （ルールベース検出なし）"

    phase = _classify_micro_macro(review)
    phase_directive = {
        "MICRO_CRITICAL": (
            "**基礎ミクロが致命的に崩壊している。LS流で容赦なく矯正せよ。** "
            "マクロ話・チャンプ別話は今は要らない。CS・trade・positioning から徹底的に直す。"
        ),
        "TELE_FOCUS": (
            "**基礎ミクロは合格だがチャンプ運用に問題あり。Tele流でmechanical executionを磨く。** "
            "ダメージシェア不足はコンボ抜け・パワースパイクで攻めない・ult素出しの徴候。"
            "具体コンボ順序・スパイクtiming・アイテム動的選択で改善する。"
        ),
        "MACRO_FOCUS": (
            "**ミクロもチャンプ運用も及第点。Curtis流でマクロ（wave/tempo/objective/vision）主軸に伸ばす。** "
            "wave manipulation・tempo creation・objective setupで次の壁を越える指導をする。"
        ),
        "MIXED": (
            "**複数領域に改善余地あり。バランスを取る。** "
            "1ポイントはLS流ミクロ矯正、1つはTele流チャンプ運用、1つはCurtis流マクロで構成する。"
        ),
    }[phase]

    user = f"""\
{review_to_brief(review)}

## ルールベース検出済み改善点
{rule_findings}

## 診断フェーズ判定
{phase_directive}

## 出力フォーマット（厳守）

このプレイヤーが次の1試合で取り組むべき最重要ポイントを **3つ** 出してください。
各ポイントは以下のフォーマットで書きます。3ポイント書き終わったら最後にKPIを書きます。

```
### ポイント1: [短い見出し] [LS:基礎ミクロ / TELE:チャンプ運用 / CURTIS:マクロ のどれか1つタグ付け]
- 何を変えるか: [1文・具体的なアクション]
- なぜ最優先か: [1文・データ根拠を必ず示す]
- 次試合での具体アクション: [1-2文・測定可能な数値で]

### ポイント2: [短い見出し] [LS / TELE / CURTIS]
- 何を変えるか: ...
- なぜ最優先か: ...
- 次試合での具体アクション: ...

### ポイント3: [短い見出し] [LS / TELE / CURTIS]
- 何を変えるか: ...
- なぜ最優先か: ...
- 次試合での具体アクション: ...

## 次試合の最優先KPI
[CS@10数値目標 / 死亡数上限 / 視界スコア最低 のいずれか1つを具体的な数値で]
```

このフォーマット以外のテキスト（前置き・後書き・思考過程・コーチ名の言及）は一切出力しないでください。
"""
    return COACH_SYSTEM_PROMPT, user


def build_full_champselect_prompt(
    me_champion: str,
    me_position: str,
    my_team: list[dict],
    their_team: list[dict],
    matchup_data: Optional[dict] = None,
    champion_data: Optional[dict] = None,
    item_rec: Optional[dict] = None,
    skill_summaries: Optional[dict[str, str]] = None,
) -> tuple[str, str]:
    """チャンプセレクト確定後の総合コーチング用プロンプト。

    Args:
        me_champion: 自分のチャンプ名
        me_position: 自分のポジション (BOTTOM等)
        my_team: [{champion, position}, ...] 5人
        their_team: 同上
        matchup_data: adc_knowledge.matchup() の結果 (score, tip)
        champion_data: champions.json の自チャンプエントリ
    """
    my_sup = next((p for p in my_team if p["position"] == "UTILITY"), None)
    enemy_adc = next((p for p in their_team if p["position"] == "BOTTOM"), None)

    def fmt_team(team: list[dict]) -> str:
        return "\n".join(
            f"- {p['position']:>7}: {p['champion'] or '?'}"
            for p in team
        )

    matchup_text = "(未収録マッチアップ)"
    if matchup_data:
        score = matchup_data.get("score", 0)
        tip = matchup_data.get("tip") or ""
        matchup_text = f"主観 score {score:+d} ({matchup_data.get('source','?')}): {tip}"
        # 各コーチ評価を併記
        coaches = matchup_data.get("coaches") or {}
        if coaches:
            matchup_text += "\n\n各コーチ評価:"
            for cname, cdata in coaches.items():
                cscore = cdata.get("score")
                ctip = cdata.get("tip") or ""
                score_str = f"{cscore:+d}" if isinstance(cscore, (int, float)) else "?"
                matchup_text += f"\n  - {cname.upper()}: score {score_str} -- {ctip}"

    champion_facts = ""
    if champion_data:
        champion_facts = (
            f"\n## 自チャンプ ({me_champion}) 基本情報\n"
            f"- 射程: {champion_data.get('range')}\n"
            f"- lane_phase: {champion_data.get('lane_phase')}\n"
            f"- scaling: {champion_data.get('scaling')}\n"
            f"- power_spikes: {', '.join(champion_data.get('power_spikes', []) or [])}\n"
            f"- 弱点: {', '.join(champion_data.get('weaknesses', []) or [])}\n"
            f"- 標準ビルド: {', '.join(champion_data.get('key_items', []) or [])}\n"
        )

    # スキル情報 (敵ADC + 自sup + 敵sup + 自分自身)
    skill_section = ""
    if skill_summaries:
        skill_section = "\n## スキル情報 (このマッチで重要なチャンプ)\n"
        for label, summary in skill_summaries.items():
            skill_section += f"\n{summary}\n"

    # アイテム推奨 ground truth (これを LLM に強制使用させる)
    item_rec_section = ""
    if item_rec:
        item_rec_section = f"""
## {me_champion} のアイテム推奨 (ground truth - 必ずこのリストから選ぶこと)

- **標準ビルド (1〜3コア基準)**: {', '.join(item_rec.get('standard', []) or [])}
- 代替1コア候補: {', '.join(item_rec.get('alt_first_options', []) or [])}
- 敵armor多 (Maokai/Trundle/Mundo/Sion等 — armor系が**3人以上**): {', '.join(item_rec.get('vs_armor_heavy', []) or [])}
- 敵AP多 (Brand/Syndra/Vex等 — AP系が**3人以上**): {', '.join(item_rec.get('vs_ap_heavy', []) or [])}
- 敵assassin (Zed/Rengar/Talon等 — burst脅威が**2人以上**): {', '.join(item_rec.get('vs_assassin', []) or [])}
- 敵engage多 (Malphite/Leona/Maokai等 — engage threat 2人以上): {', '.join(item_rec.get('vs_engage_heavy', []) or [])}
- 敵kite/高機動 (Yasuo/Yone/Akali等): {', '.join(item_rec.get('vs_kite_heavy', []) or [])}

**重要ルール:**
- **1コアは原則 standard リストから選ぶ**。代替リスト(vs_xxx)に切り替えるのは「該当カテゴリの敵が複数いる」場合のみ
- AP1人だけ (Syndra 1人だけ等) では Mercurial Scimitar を1コアにしない。標準1コア → 2-3コアで Mercurial 検討
- assassin 1人だけでも 1コアは standard。Edge of Night は2-3コアで検討

注意: {item_rec.get('notes', '')}
"""

    user = f"""\
Champion Select が確定しました。ADCとして以下の試合に挑みます。

## 構成
あなた: **{me_champion}** ({me_position})

味方チーム:
{fmt_team(my_team)}

敵チーム:
{fmt_team(their_team)}

## マッチアップ情報 (botレーン: {me_champion} vs {enemy_adc['champion'] if enemy_adc else '?'})
{matchup_text}
{champion_facts}
{skill_section}
{item_rec_section}

## 出力依頼

以下4つのセクションをマークダウンで簡潔に出力してください。各セクション3行以内。

### 1. Lane Phase（〜10分）
- Lv1-2の trade window タイミング
- 敵 {enemy_adc['champion'] if enemy_adc else '?'} の警戒すべきスキル/コンボ

### 2. 味方サポ {my_sup['champion'] if my_sup else '?'} との連携
- どのタイミングで仕掛けるか
- 具体的なコンボ手順

### 3. コアアイテム (1〜3コア)
**上の「{me_champion} のアイテム推奨」リストから必ず選ぶこと**。リスト外のアイテムは禁止。
1〜3コアそれぞれ、標準ビルドベースで、敵構成を見て該当カテゴリのアイテムに差し替える。

- 1コア: [推奨リストから1個] - 理由 (例: 標準のCollector / 敵armor多なのでXに変更)
- 2コア: [推奨リストから1個] - 理由
- 3コア: [推奨リストから1個] - 理由

注意:
- LDR / Wit's End / Mercurial 等の代替は敵構成が該当する場合のみ。標準ビルドが基本。
- アイテム特性を間違えるな (Edge of Night は spell shield、Wit's End は AS+MR の on-hit)
- AD champ(敵ADC含む) に対して「magic resistance確保」のような的外れな理由付けは禁止

### 4. 集団戦のポジショニング
- 敵チームの最大脅威 (どのチャンプを最も意識)
- ADCとしての立ち位置 (タンク後ろ / 後衛 / ペル系)

このフォーマット以外のテキスト・思考過程は禁止。

## 用語ルール (厳守)

- 曖昧な英語俗語禁止: "Wall" "Window" のような未定義語を使わない。
  代わりに具体的なゲーム用語を使う:
  * Caitlyn の Yordle Snap Trap は **Trap** (W) と呼ぶ
  * trade chance は **trade window** (時間帯)、**kill threshold** (HPライン)
  * 壁を指すなら **terrain wall** (Anivia W / Yasuo W 等のスキル wall は明示)
- スキル名は必ずスキル情報セクションの正式名称を使う (例: Lucian E = Relentless Pursuit, NOT "Sundering Arrow")
- ChampのCC情報を正確に: Lulu は stun 無し (Lulu W = polymorph / R = knockup)
- AD champ に「magic resistance」のような的外れな提案禁止

/no_think
"""
    return COACH_SYSTEM_PROMPT, user


def build_matchup_prompt(my_champ: str, enemy_champ: str, my_sup: str, enemy_sup: str) -> tuple[str, str]:
    """チャンプセレクト時のマッチアップ説明用プロンプト"""
    user = f"""\
チャンプセレクト確定。これからのレーン情報:

- 自分: {my_champ}
- 敵ADC: {enemy_champ}
- 自サポ: {my_sup}
- 敵サポ: {enemy_sup}

このレーン構成における、開始3分間で意識すべき3点を箇条書きで出してください。
1点ごとに「アクション」と「理由」を1文ずつ。
"""
    return COACH_SYSTEM_PROMPT, user
