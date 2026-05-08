# LoL ADC Coach

League of Legends ADC（ボットレーンキャリー）専用のコーチングツール。

> [!NOTE]
> 本プロジェクトは旧 EchoMate（ゲーム相棒AI）から **2026-05-07 に全面ピボット** した。
> 旧コンセプトのコードは `echomate-legacy` ブランチに保全されている。

---

## 目標

**ADC初心者をマスター帯（上位0.5%）以上に引き上げるためのコーチング。**

最初は ADC のみ対応。将来的に他ロール（サポート → ミッド → ジャングル → トップ）へ拡張する。

---

## アーキテクチャ概要

```
                                    ┌────────────────────────┐
                                    │  Riot Web API          │  ←── マッチ履歴・タイムライン取得
                                    │  (試合後レビュー本命)  │
                                    └──────────┬─────────────┘
                                               │
                                               ▼
   ┌──────────────────┐            ┌──────────────────────────┐
   │  LCU API         │ ──────────▶│  match_review.py         │
   │  (チャンプセレ)  │            │  + adc_knowledge.py      │
   └──────────────────┘            │  → ImprovementPoint[]    │
                                   └──────────┬───────────────┘
   ┌──────────────────┐                       │
   │ Live Client API  │                       ▼
   │ (試合中軽量)     │            ┌──────────────────────────┐
   │ → tkinter        │            │  coach_prompts.py        │
   │   半透明オーバレイ│           │  + Ollama (coach_ai.py)  │
   └──────────────────┘            │  → 自然文コーチコメント  │
                                   │  → Notion風HTMLボード    │
                                   └──────────────────────────┘
```

### 二つのモード

| モード | 比重 | 内容 |
|---|---|---|
| **試合後レビュー（本質）** | 主 | マッチ履歴・タイムライン解析 → 改善ポイント抽出・LLM解説 |
| リアルタイム（軽め） | 従 | Live Client Data APIでHP/CS/Goldをポーリング、画面右下に半透明tkinterオーバーレイで邪魔にならないテキスト表示 |

**音声合成（VOICEVOX）は採用しない。** プレイの邪魔にならない非侵襲的UIを優先する。

---

## クイックスタート

### 1. 依存インストール

```powershell
pip install -r requirements_coach.txt
```

### 2. Riot APIキー取得

1. <https://developer.riotgames.com/> にRiotアカウントでログイン
2. Personal API Key を発行（**24時間で失効**するので毎日再発行）
3. 恒久運用には Production Key 申請を推奨。
   申請テンプレは [docs/riot_production_key_application.md](docs/riot_production_key_application.md) 参照

### 3. 設定ファイル

`.env.example` を `.env` にコピーして編集:

```
RIOT_API_KEY=RGAPI-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
RIOT_PLATFORM=jp1
COACH_MODEL=qwen3:8b
```

### 4. Ollama起動

```powershell
ollama pull qwen3:8b
ollama serve   # 通常は自動起動
```

### 5. 試合後レビュー実行

```powershell
# 最新ランクソロ1試合をレビュー
python coach_main.py --riot-id "あなたのID#TAG" --rank GOLD

# 直近3試合 / プラチナ基準
python coach_main.py --riot-id "あなたのID#TAG" --count 3 --rank PLATINUM

# LLM呼び出しをスキップ（ルールベース出力のみ）
python coach_main.py --riot-id "あなたのID#TAG" --no-llm
```

---

## ファイル構成

| ファイル | 役割 |
|---|---|
| `coach_main.py` | エントリーポイント |
| `riot_api.py` | Riot Web APIクライアント |
| `lcu_client.py` | LCU APIクライアント（チャンプセレ等） |
| `live_client.py` | Live Client Data APIクライアント |
| `match_review.py` | 試合後レビューエンジン（CS/KDA/デス分析） |
| `adc_knowledge.py` | ADC知識ベースアクセサ |
| `coach_prompts.py` | コーチング用プロンプトテンプレート |
| `coach_ai.py` | コーチ専用Ollamaクライアント |
| `coach_overlay.py` | tkinter半透明オーバーレイ（インゲーム用） |
| `coach_live.py` | Live Clientポーリング + オーバーレイ更新 |
| `coach_review_view.py` | Notion風HTMLレビューボード生成 |
| `data/adc/champions.json` | ADCチャンプ知識 |
| `data/adc/matchups.json` | マッチアップ表 |
| `data/adc/benchmarks.json` | 各ランクのCS/min等の目標値 |
| `作業.md` | 開発ロードマップ |

### 旧EchoMate由来（再利用候補）

`state_manager.py` / `event.py` / `ai_memory.py` / `user_profile.py` / `patron_db.py` は legacy として残し、コーチ用に段階的に転用する。

`voice.py` (VOICEVOX) / `ai.py` (キャラ会話) / `gui.py` / `bubble.py` (旧バブルUI) は **不採用**（コーチには邪魔）。詳細は `作業.md`。

---

## ロードマップ

`作業.md` 参照。要旨:

- **Phase R-1**: 基盤整備（完了）
- **Phase R-2**: APIクライアント雛形（完了）
- **Phase R-3**: 試合後レビュー最小機能（完了 ※実APIキーで動作確認待ち）
- **Phase R-4**: ADC知識ベース構築（一部完了 — 5チャンプのみ）
- **Phase R-5**: 成長追跡（user_profile転生）
- **Phase R-6**: リアルタイム軽量警告
- **Phase R-7**: GUI
- **Phase R-8**: 他ロール拡張

---

## ライセンス

MIT
