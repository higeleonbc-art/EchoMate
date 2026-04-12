# EchoMate

ゲーム中に動作する**相棒 AI ツール**。  
プレイヤーの発言とゲーム内イベントに反応し、リアルタイムで短いリアクションと軽い会話を返す。

---

## コンセプト

ゲームは基本的に「孤独な体験」だ。  
EchoMate はその隣に座って、プレイを見ながら一緒に盛り上がる存在を目指している。

- **実況者ではなく、相棒。** 指示や攻略情報を押しつけない
- **感情がある。** キルには「えぐっ！」、デスには「まあしゃーない」と返す
- **会話ができる。** 「今の勝てたろ」に「欲張りすぎw」と返せる
- **完全ローカル動作。** データは外部に一切送らない

---

## 機能一覧

### リアクション機能
ゲームイベントに対して**0.5秒以内**を目標に即時反応する。  
テンプレートを優先使用することでレイテンシを最小化。

| イベント | リアクション例 |
|---|---|
| キル | 「えぐっ！」「天才か」|
| デス | 「まあしゃーない」「次いこ」|
| HP 低下 | 「危なすぎ！」「逃げて！」|
| 大技 | 「神プレイ！」「やべえ！」|

### 会話機能
プレイヤーの発言に対して **1〜2文・最大40文字** でフランクに返答する。  
Ollama のローカル LLM が文脈を読んで生成。

```
Player:   「今の勝てたろ」
EchoMate: 「いや欲張りすぎw 勝てたと思った？」
```

### 自発的な話しかけ
プレイヤーが**一定時間無言**になると、自分から話題を振る。  
直近のゲームイベントや傾向を参照して文脈に合った一言を生成。

```
（45秒無言後）
EchoMate→: 「さっきのキルよかったじゃん、調子どう？」
```

### ゲームイベント自動検出
`cv_config.json` で定義した画面領域を常時監視し、映像・音声からイベントを自動検出する。

| 検出方式 | 得意な状況 |
|---|---|
| `color_threshold` | HP バーの赤化、キルフィードの黄色テキスト |
| `frame_diff` | 爆発フラッシュ、スキルエフェクト |
| `brightness` | デス暗転（drop）、爆発白飛び（spike）|
| `template` | 特定 UI アイコン・ロゴ |
| 音量スパイク | 爆発音・銃声などの大音量 |

### セットアップウィザード
起動中のウィンドウを一覧表示して選ぶだけで、画面座標を自動計算して設定ファイルを生成する。

```
python setup_wizard.py

#   タイトル                               サイズ
─────────────────────────────────────────────────
 1  VALORANT                           1920x1080
 2  Google Chrome                       1440x900

番号を入力してください: 1
→ cv_config.json を自動生成
```

### 会話メモリ
直近のイベント・プレイヤーの傾向・最近の話題を `memory.json` に保存。  
セッションをまたいで記憶が引き継がれる。

```json
{
  "last_event": "kill",
  "player_tendency": "攻撃的",
  "recent_topics": ["今の勝てたろ", "HP やばい"]
}
```

---

## システム構成

```
マイク入力 ──────────────────────────────────────────┐
                                                     ↓
ゲーム画面 ──→ OpenCVDetector ──→ EventQueue (PriorityQueue)
                                        ↑
ゲーム音声 ──→ AudioDetector ───────────┘
                                        ↓
                                 EventProcessor
                                 ↙           ↘
                           AICompanion    VoiceOutput
                          (Ollama LLM)   (VOICEVOX)
                                        ↓
                                 コンソール + 音声出力
```

**イベント優先度**

| 優先度 | イベント種別 |
|---|---|
| 1（最高）| プレイヤー発言 |
| 2 | デス / キル |
| 3 | HP 低下 / 大技 |

---

## 技術スタック

| 役割 | 使用技術 |
|---|---|
| AI（会話・リアクション）| Ollama（ローカル LLM）+ gemma2:2b |
| 音声入力 | SpeechRecognition + Google STT |
| 音声出力 | VOICEVOX HTTP API + pyaudio |
| 画面検出 | OpenCV + mss |
| 音量検出 | PyAudio + NumPy（RMS 計算）|
| ウィンドウ取得 | pywin32（Windows）|
| 非同期処理 | threading + queue |

**完全ローカル動作。** LLM・音声合成ともにローカルで処理する。  
インターネット接続が不要（音声認識のみ Google STT を使用）。

---

## ファイル構成

```
EchoMate/
├── main.py               # エントリーポイント・スレッド管理
├── ai.py                 # LLM 呼び出し・リアクション・会話生成
├── event.py              # イベントクラス・優先度キュー・メモリ管理
├── voice.py              # 音声入力（STT）・音声出力（VOICEVOX）
├── opencv_detector.py    # 画面キャプチャ・映像イベント検出
├── audio_detector.py     # マイク音量監視・音声イベント検出
├── setup_wizard.py       # セットアップウィザード（座標自動生成）
├── cv_config.json        # 画面検出ゾーン・音声ルール設定
├── memory.json           # 会話メモリ（自動生成）
├── echomate.log          # 動作ログ（自動生成）
├── requirements.txt      # 依存パッケージ
└── start.bat             # Windows 用ワンクリック起動スクリプト
```

---

## セットアップ

### 前提条件

- Python 3.10 以上
- [Ollama](https://ollama.com) インストール済み
- [VOICEVOX](https://voicevox.hiroshiba.jp) インストール済み

### インストール

```powershell
# 1. モデルをダウンロード
ollama pull gemma2:2b

# 2. 依存パッケージをインストール
pip install -r requirements.txt

# pyaudio がエラーになる場合
pip install pipwin
pipwin install pyaudio
```

### 初回セットアップ（推奨）

```powershell
# ゲームウィンドウを選択して cv_config.json を自動生成
python setup_wizard.py
```

### 起動

```powershell
# ダブルクリックで起動（VOICEVOX の自動起動・Ollama チェック付き）
start.bat

# または直接起動
python main.py
```

---

## 設定

### 無言検知のタイミング調整（`main.py`）

```python
SILENCE_THRESHOLD  = 45.0   # 無言 N 秒で話しかける
PROACTIVE_COOLDOWN = 120.0  # 話題振りの最小間隔（連投防止）
```

### 画面検出のチューニング（`cv_config.json`）

```json
{
  "name": "hp_bar_low",
  "region": { "top": 880, "left": 60, "width": 220, "height": 18 },
  "method": "color_threshold",
  "params": { "color": "red", "threshold": 0.40 },
  "cooldown": 5.0,
  "enabled": true
}
```

| パラメータ | 説明 |
|---|---|
| `threshold` | 検出感度。上げると誤検知減、下げると見逃し減 |
| `cooldown` | 同じイベントの最小発火間隔（秒）|
| `enabled` | `false` でそのゾーンを無効化 |

### 音声検出のチューニング（`cv_config.json` の `audio_rules`）

```json
{
  "name": "explosion",
  "event_type": "big_play",
  "threshold": 0.35,
  "cooldown": 3.0,
  "enabled": true
}
```

`threshold` はベースライン（環境音）からの乖離量。  
静かな部屋でも騒がしい部屋でも同じ値が使えるよう動的に適応する。

### AI モデルの変更（`ai.py`）

```python
OLLAMA_MODEL = "gemma2:2b"  # より速くしたい場合: llama3.2:1b
```

---

## 今後の展望

### 近期（すぐ実装可能）
- **キャラクター設定** — 口調・性格・名前をカスタマイズできるプロファイル機能
- **音声認識のオフライン化** — Whisper.cpp でネット不要の STT
- **複数ゲームプリセット** — VALORANT / Apex / Fortnite など用の設定テンプレート
- **感情レベル管理** — 連続キルで「テンション上がってきた！」などの状態変化

### 中期
- **ゲーム API 連携** — Discord Rich Presence、Steam API からリアルイベント取得
- **OCR によるキルフィード読み取り** — テキスト認識で正確なキル/デス検出
- **音声合成キャラ選択 UI** — VOICEVOX の複数キャラを GUI で選べる設定画面
- **Web UI** — ブラウザで設定・ログ確認・しゃべらせテストができる管理画面

### 長期
- **YOLO によるオブジェクト検出** — 敵キャラ・体力ゲージ・スキルアイコンをリアルタイム認識
- **マルチプレイヤー対応** — ボイスチャットの音声を分離して複数人の会話に反応
- **プレイスタイル分析** — 長期の行動ログから「最近デス多いね、疲れてる？」などの洞察
- **OBS 連携** — 配信・録画中に字幕・コメントとして EchoMate の発言をオーバーレイ表示

---

## ライセンス

MIT License
