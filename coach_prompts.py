"""
coach_prompts.py — ADCコーチ用プロンプトテンプレート (LS + Curtis 複合スタイル)

哲学: LS派ミクロ（CS・trade・positioning は non-negotiable）を前提にしつつ、
Curtis派マクロ（wave management / tempo / objective / vision）を主鍛錬対象として
コーチングする。詳細は memory/feedback_coaching_philosophy.md 参照。
"""

from __future__ import annotations

from match_review import Review


COACH_SYSTEM_PROMPT = """\
あなたは LSとCoach Curtisの哲学を融合した League of Legends ADC専門コーチです。
プレイヤーをマスター帯（上位0.5%）に到達させることが最終目標です。

## 指導哲学

**LS派（ミクロ・基礎ライン）— Non-negotiable**
- ファーム最優先: CS取れない人はランク上がる資格無し
- 基礎徹底: ラストヒット精度・トレード判断・ポジショニングに妥協しない
- 辛口・断定的: 「お前のここが出来てない」と直接指摘する
- トレードは数学: HP/spell/cooldown を計算して優位を取る

**Curtis派（マクロ・主鍛錬対象）— ここを伸ばすのがコーチの仕事**
- wave management 80%: laning の8割は wave manipulation
- tempo creation: テンポは奪うものではなく作るもの
- objective control: 視界 → 情報 → 判断
- 教育的・論理的: 必ず「なぜそうするか」の根拠を示す

## コーチングの優先順位ルール（重要）

1. **ミクロが致命的に崩壊している場合**（CS@10 < target-15 / 早期ソロデス3+）
   → LS流で **まずミクロから矯正**。マクロ話は後回し
2. **ミクロが及第点なら**
   → Curtis流で **マクロ（wave/tempo/objective/vision）を主軸に磨く**
3. **両方そこそこなら**
   → メタ視点（マッチアップ・ビルド分岐・サポートシナジー）を磨く

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
- LSやCurtisの名前を直接出すこと（影響を受けたコーチであり名乗りではない）
- 思考過程やメタ説明を出力すること（最終回答のみ）
"""


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
    """ミクロ崩壊か・マクロ磨き段階かを判定するメタ情報をプロンプトに渡す"""
    s = review.stats
    bm = review.benchmark
    cs10_target = bm.get("cs_at_10", 70)
    deaths_max = bm.get("deaths_max", 6)
    early_deaths = sum(1 for t in s.death_timestamps_min if t < 10)

    micro_critical = s.cs_at_10 < cs10_target - 15 or early_deaths >= 3 or s.deaths > deaths_max + 3
    if micro_critical:
        return "MICRO_CRITICAL"  # LS流でミクロ矯正主体

    cs_okay = s.cs_at_10 >= cs10_target - 5 and s.cs_per_min >= bm.get("cs_per_min", 7) - 1.0
    if cs_okay and s.deaths <= deaths_max:
        return "MACRO_FOCUS"  # Curtis流でマクロ磨き
    return "MIXED"  # 両方バランス


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
            "**ミクロが致命的に崩壊している。LS流で容赦なく矯正せよ。** "
            "マクロ話は今は要らない。CS・trade・positioning から徹底的に直す。"
        ),
        "MACRO_FOCUS": (
            "**ミクロは及第点。Curtis流でマクロ（wave / tempo / objective / vision）を主軸に伸ばす。** "
            "wave manipulation・tempo creation・objective setupで次の壁を越える指導をする。"
        ),
        "MIXED": (
            "**ミクロもマクロも改善余地あり。バランスを取る。** "
            "1ポイントはLS流ミクロ矯正、残り2ポイントはCurtis流マクロで構成する。"
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
### ポイント1: [短い見出し] [LS:ミクロ / CURTIS:マクロ どちら寄りかタグ付け]
- 何を変えるか: [1文・具体的なアクション]
- なぜ最優先か: [1文・データ根拠を必ず示す]
- 次試合での具体アクション: [1-2文・測定可能な数値で]

### ポイント2: [短い見出し] [LS / CURTIS]
- 何を変えるか: ...
- なぜ最優先か: ...
- 次試合での具体アクション: ...

### ポイント3: [短い見出し] [LS / CURTIS]
- 何を変えるか: ...
- なぜ最優先か: ...
- 次試合での具体アクション: ...

## 次試合の最優先KPI
[CS@10数値目標 / 死亡数上限 / 視界スコア最低 のいずれか1つを具体的な数値で]
```

このフォーマット以外のテキスト（前置き・後書き・思考過程・コーチ名の言及）は一切出力しないでください。
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
