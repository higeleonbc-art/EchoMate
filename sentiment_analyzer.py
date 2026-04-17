"""
sentiment_analyzer.py - テキストからの簡易感情判定モジュール

プレイヤーの発言から感情状態（ポジティブ/ネガティブ/フラストレーション/ニュートラル）を
ルールベースで推定し、AIプロンプトへの注入文字列を返す。

感情ラベル:
  frustrated  : イライラ・怒り（デス後の愚痴、対戦相手への怒り等）
  positive    : 喜び・興奮（キル後の興奮、良いプレイへの反応等）
  negative    : 落ち込み・諦め（負け確信、疲労感等）
  neutral     : 特定の感情が読み取れない通常発言
"""

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 感情パターン定義
# ---------------------------------------------------------------------------

_FRUSTRATED_PATTERNS = [
    r"[くク][そソ]",          # くそ / クソ
    r"ふざけ",
    r"むかつ[くき]",
    r"うざ[いっ]?",
    r"なんで.*死",
    r"なんで.*負",
    r"は[ぁあ]+",             # はぁ〜 等のため息
    r"もう無理",
    r"ガチで無理",
    r"マジか+よ",
    r"意味わからん",
    r"意味不明",
    r"チーターじゃん",
    r"バグ[っ]?てる",
    r"やばいじゃん",          # 否定文脈
    r"殺せないじゃん",
    r"死んだ[わ]",
    r"また死",
    r"[うウ][わワ][ぁあァア]+", # うわぁ
]

_POSITIVE_PATTERNS = [
    r"やった[ぁあー!！]*",
    r"よっしゃ[あー!！]*",
    r"いい[ね感じ]",
    r"うまっ",
    r"えぐ[い]",
    r"神プレイ",
    r"ペンタ",
    r"キル[し取]",
    r"勝[てっ]",
    r"強い[ぞわ]",
    r"楽し[いっ]",
    r"最高",
    r"[すス]ご[いっ]",
    r"完璧",
    r"天才",
    r"[わワ]ーい",
]

_NEGATIVE_PATTERNS = [
    r"[つ辛]い[なぁ]?$",
    r"疲れ[たっ]",
    r"もうだめ",
    r"負け[た確]",
    r"終わ[っり]た",
    r"弱い[なぁ]",
    r"へた[くっ]",
    r"上手くな[いれ]",
    r"無理かも",
    r"諦め",
    r"眠[いっ]",
    r"だる[いっ]",
]

_COMPILED_FRUSTRATED = [re.compile(p) for p in _FRUSTRATED_PATTERNS]
_COMPILED_POSITIVE   = [re.compile(p) for p in _POSITIVE_PATTERNS]
_COMPILED_NEGATIVE   = [re.compile(p) for p in _NEGATIVE_PATTERNS]


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class SentimentResult:
    label: str   # "frustrated" | "positive" | "negative" | "neutral"
    score: float # 0.0 〜 1.0（確信度）

    def to_prompt_string(self) -> str:
        """AIプロンプトへ注入する文字列を返す（空の場合は注入不要）"""
        if self.label == "neutral" or self.score < 0.3:
            return ""
        label_map = {
            "frustrated": "イライラ・怒り気味",
            "positive":   "テンション高め・喜んでいる",
            "negative":   "落ち込み・疲れ気味",
        }
        desc = label_map.get(self.label, "")
        return f"（ユーザーは現在 {desc} な状態です。それに合わせたトーンで応答してください）"


# ---------------------------------------------------------------------------
# 分析関数
# ---------------------------------------------------------------------------

def analyze(text: str) -> SentimentResult:
    """
    テキストから感情を判定して SentimentResult を返す。

    優先順位: frustrated > positive > negative > neutral
    """
    if not text:
        return SentimentResult("neutral", 0.0)

    frustrated_hits = sum(1 for p in _COMPILED_FRUSTRATED if p.search(text))
    positive_hits   = sum(1 for p in _COMPILED_POSITIVE   if p.search(text))
    negative_hits   = sum(1 for p in _COMPILED_NEGATIVE   if p.search(text))

    total = frustrated_hits + positive_hits + negative_hits
    if total == 0:
        return SentimentResult("neutral", 0.0)

    # フラストレーションが優先
    if frustrated_hits > 0 and frustrated_hits >= positive_hits:
        score = min(1.0, 0.4 + frustrated_hits * 0.2)
        return SentimentResult("frustrated", round(score, 2))

    if positive_hits > 0 and positive_hits >= negative_hits:
        score = min(1.0, 0.4 + positive_hits * 0.2)
        return SentimentResult("positive", round(score, 2))

    if negative_hits > 0:
        score = min(1.0, 0.4 + negative_hits * 0.2)
        return SentimentResult("negative", round(score, 2))

    return SentimentResult("neutral", 0.0)
