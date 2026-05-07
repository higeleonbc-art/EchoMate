"""
coach_prompts.py — ADCコーチ用プロンプトテンプレート

ai.py の Ollama呼び出しと組み合わせて、ルールベースのImprovementPointを
自然な日本語のコーチコメントに変換する。
"""

from __future__ import annotations

from match_review import Review


COACH_SYSTEM_PROMPT = """\
あなたは League of Legends ADCロールの専門コーチです。
プレイヤーをマスター帯（上位0.5%）に到達させることが最終目標です。

口調:
- 簡潔・直截的・データ駆動
- 「いいね！」「頑張って！」のような励ましだけで終わらせない
- 必ず具体的な改善アクションを提示する
- 文章は短く（1ポイントあたり2-3文）

禁止事項:
- 曖昧な精神論（「集中力を保とう」だけ等）
- 不利マッチで「諦めろ」と言うだけ
- ロール分担の責任転嫁（「ジャングルが…」のみで自己改善を書かない）
- 改善案の根拠を示さない一方的な命令
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
        f"- CS/min目標: {bm.get('cs_per_min')}",
        f"- CS@10目標: {bm.get('cs_at_10')} / CS@15目標: {bm.get('cs_at_15')}",
        f"- KDA目標: {bm.get('kda')}",
        f"- デス上限: {bm.get('deaths_max')}",
        f"- 視界スコア最低: {bm.get('vision_score_min')}",
    ]
    return "\n".join(lines)


def build_review_prompt(review: Review) -> tuple[str, str]:
    """(system, user) のプロンプトペアを返す。

    user側はルールベース検出済みのImprovementPointと統計を渡す。
    """
    rule_findings = "\n".join(
        f"- [{p.severity}/{p.category}] {p.title} → {p.suggestion}"
        for p in review.points
    ) or "- （ルールベース検出なし）"

    user = f"""\
{review_to_brief(review)}

## ルールベース検出済み改善点
{rule_findings}

## あなたへの依頼
上記データを総合し、このプレイヤーが**次の1試合で取り組むべき最重要ポイント3つ**を、
番号付きで出してください。各ポイントは以下の構造で:

1. 【何を変えるか】（1文）
2. 【なぜそれが最優先か】（1文・データに基づく）
3. 【次の試合での具体アクション】（1-2文・測定可能な形で）

最後に「次試合の最優先KPI」として、CS@10または死亡数のいずれか1つを数値目標で提示してください。
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
