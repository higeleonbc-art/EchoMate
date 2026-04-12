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

### ミニ会話システム（v2）
イベント発生後、時間差で 3 ステップの発話を行う。

| ステップ | タイミング | 内容 |
|---|---|---|
| step1 | 即座 | テンプレートによる即時リアクション |
| step2 | +5 秒後 | プレイへの短評（LLM 生成）|
| step3 | +10 秒後 | 締めの一言（LLM 生成）|

### プレイヤー状態管理（v2）
連続キル・HP・テンションを追跡し、AI の発言に反映する。

| 状態 | 内容 |
|---|---|
| HP 状態 | SAFE / LOW / CRITICAL（検出ゾーンから自動遷移）|
| 戦闘状態 | IDLE / IN_COMBAT（イベント受信で切替）|
| モメンタム | 連続キル数（デスでリセット）|
| テンション | 0.0〜1.0、キル/大技で上昇・時間で自然減衰 |

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

誤検知対策として **時系列フィルタ**（`min_hits` / `window` パラメータ）を搭載。  
直近 N フレームのうち M 回以上検出された場合のみイベントを発火する。

### セットアップウィザード
3 つの方式で `cv_config.json` を生成できる。

```powershell
# ① ウィンドウ選択（比率ベースで座標を自動計算）
python setup_wizard.py

# ② ROI セレクター（スクリーンショット上でマウスドラッグ）
python setup_wizard.py --roi

# ③ ゲームプリセット（VALORANT / Apex / Fortnite）
python setup_wizard.py --preset valorant
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

## キャラクター

`--character` オプションで起動時にキャラクターを選択できる。  
各キャラクターは口調・性格・VOICEVOX 話者が異なる。

```powershell
python main.py --character michiko
python main.py --character rei --no-cv
```

| キー | 名前 | 性格・口調 | VOICEVOX 話者 |
|---|---|---|---|
| `kid` | キッド（デフォルト）| 悪友・軽口・ハイテンション | ずんだもん（ID:7）|
| `michiko` | ミチコ | ツンデレ・最初に否定・たまにデレ | 春日部つむぎ（ID:8）|
| `rei` | レイ | 冷静分析・感情排除・敬語 | ナースロボ＿タイプT（ID:47）|
| `ryu` | リュウ | 兄貴肌・厳しめ・必ず改善提案 | 青山龍星（ID:13）|
| `akane` | アカネ | 姉御・肯定→安心→助言の順で話す | 波音リツ（ID:9）|
| `echo` | エコー | 観察者・推測表現のみ・AI 的 | 玄野武宏（ID:11）|

キャラクター定義は `characters.json` で管理。`system_prompt` / `rules` / `constraints` を自由に編集できる。

---

## システム構成

```
マイク入力 ──→ VoiceInput (faster-whisper) ──────────────────┐
                                                              ↓
ゲーム画面 ──→ OpenCVDetector ──→ EventQueue (PriorityQueue) ←┤
                                        ↑                    │
ゲーム音声 ──→ AudioDetector ───────────┘                    │
                                        ↓
                              EventProcessorThread
                              ↙                  ↘
                       StateManager           AICompanion
                       (HP/テンション)        (Ollama LLM)
                                                  ↓
                                    MiniConversationManager
                                    (step2: +5s / step3: +10s)
                                                  ↓
                                    VoiceOutput (VOICEVOX)
                                    コンソール出力
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
| 音声入力 | faster-whisper（オフライン STT）+ PyAudio |
| 音声出力 | VOICEVOX HTTP API + pyaudio |
| 画面検出 | OpenCV + mss |
| 音量検出 | PyAudio + NumPy（RMS 計算）|
| ウィンドウ取得 | pywin32（Windows）|
| 非同期処理 | threading + queue |

**完全ローカル動作。** LLM・音声合成・音声認識すべてローカルで処理する。  
インターネット接続不要。

---

## ファイル構成

```
EchoMate/
├── main.py               # エントリーポイント・スレッド管理
├── ai.py                 # LLM 呼び出し・リアクション・会話生成
├── event.py              # イベントクラス・優先度キュー・メモリ管理
├── voice.py              # 音声入力（faster-whisper）・音声出力（VOICEVOX）
├── state_manager.py      # プレイヤー状態管理（HP/テンション/モメンタム）
├── characters.json       # キャラクタープロファイル定義
├── opencv_detector.py    # 画面キャプチャ・映像イベント検出
├── audio_detector.py     # マイク音量監視・音声イベント検出
├── setup_wizard.py       # セットアップウィザード（座標自動生成・ROI・プリセット）
├── presets/
│   ├── valorant.json     # VALORANT 用検出設定（1920x1080）
│   ├── apex.json         # Apex Legends 用検出設定（1920x1080）
│   └── fortnite.json     # Fortnite 用検出設定（1920x1080）
├── cv_config.json        # 画面検出ゾーン・音声ルール設定（自動生成）
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

> **注意**: `faster-whisper` は初回起動時に Whisper モデル（約 244MB）を自動ダウンロードします。

### 初回セットアップ（推奨）

```powershell
# ゲームウィンドウを選択して cv_config.json を自動生成
python setup_wizard.py

# または ROI セレクターで視覚的に設定
python setup_wizard.py --roi

# またはゲームプリセットを使用
python setup_wizard.py --preset valorant
```

### 起動

```powershell
# ダブルクリックで起動（VOICEVOX の自動起動・Ollama チェック付き）
start.bat

# または直接起動（デフォルトキャラクター: キッド）
python main.py

# キャラクターを指定して起動
python main.py --character michiko

# OpenCV 検出を無効にして起動
python main.py --no-cv

# 音声検出を無効にして起動
python main.py --no-audio
```

---

## 設定

### 無言検知のタイミング調整（`main.py`）

```python
SILENCE_THRESHOLD  = 45.0   # 無言 N 秒で話しかける
PROACTIVE_COOLDOWN = 120.0  # 話題振りの最小間隔（連投防止）
```

### ミニ会話の間隔調整（`main.py`）

```python
# MiniConversationManager
STEP2_DELAY = 5.0   # step2 を発火するまでの秒数
STEP3_DELAY = 10.0  # step3 を発火するまでの秒数
```

### 画面検出のチューニング（`cv_config.json`）

```json
{
  "name": "hp_bar_low",
  "region": { "top": 880, "left": 60, "width": 220, "height": 18 },
  "method": "color_threshold",
  "params": { "color": "red", "threshold": 0.40, "min_hits": 2, "window": 3 },
  "cooldown": 5.0,
  "enabled": true
}
```

| パラメータ | 説明 |
|---|---|
| `threshold` | 検出感度。上げると誤検知減、下げると見逃し減 |
| `min_hits` | 発火に必要な連続検出回数（時系列フィルタ）|
| `window` | 判定する直近フレーム数（時系列フィルタ）|
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

### デバッグログを有効化する

```powershell
python main.py --debug
```

DEBUG モードでは CV 検出の数値・Ollama の応答時間・音量 RMS などが `echomate.log` に記録される。  
通常使用では `--debug` なしの INFO レベルで十分。

### AI モデルの変更（`ai.py`）

```python
OLLAMA_MODEL = "gemma2:2b"  # より速くしたい場合: llama3.2:1b
```

### キャラクターのカスタマイズ（`characters.json`）

```json
{
  "kid": {
    "name": "キッド",
    "voicevox": { "speaker": "ずんだもん", "speaker_id": 7 },
    "system_prompt": "お前はゲームの相棒AIだ。...",
    "rules": ["1文・最大20文字", "語尾に「じゃん」「じゃん？」を多用"],
    "constraints": { "no_exclamation": false }
  }
}
```

---

## プライバシーとデータ管理

EchoMate は**完全ローカル動作**であり、ユーザーデータを外部サーバーに送信しない。  
ただし以下のデータがローカルに保存・処理されることに注意すること。

| データ種別 | 保存先 | 内容 | 送信先 |
|---|---|---|---|
| 音声認識テキスト | `memory.json`（最新5件）・`echomate.log` | プレイヤーの発言 | なし（ローカルのみ）|
| ゲームイベント履歴 | `memory.json` | kill/death 等のイベント種別 | なし |
| プレイヤー傾向 | `memory.json` | 「攻撃的」等のラベル | なし |
| ゲーム画面の映像 | 保存なし | 検出計算のみ（メモリ内処理） | なし |
| マイク音量 | 保存なし | RMS値のみ計算（録音なし）| なし |
| 音声認識の音声バッファ | 保存なし | faster-whisper でローカル処理後に破棄 | なし |

### 注意事項

- `echomate.log` にはプレイヤーの発言が記録される（INFO レベル以上）。  
  バグ報告などでログファイルを共有する場合は内容を事前に確認すること
- `memory.json` を削除するとセッション記憶がリセットされる
- VOICEVOX・Ollama はどちらも `localhost` へのリクエストのみ発行する

---

## 利用規約・ライセンス情報

### 使用ライブラリのライセンス

| ライブラリ | ライセンス | 商用利用 |
|---|---|---|
| faster-whisper | MIT | 可 |
| Whisper モデル（OpenAI） | MIT | 可 |
| requests | Apache 2.0 | 可 |
| numpy | BSD 3-Clause | 可 |
| pyaudio / PortAudio | MIT / MIT | 可 |
| opencv-python | Apache 2.0 | 可 |
| mss | MIT | 可 |
| pywin32 | PSF | 可 |

### Ollama / LLM モデル

- **Ollama 本体**: MIT License
- **gemma2:2b（Google）**: [Gemma Terms of Use](https://ai.google.dev/gemma/terms) に従う  
  個人・研究・商用利用は基本的に許可されているが、禁止用途（武器・違法コンテンツ等）が定められている

### VOICEVOX（重要）

VOICEVOX で生成した音声の利用には **VOICEVOX の利用規約**と**各キャラクターのキャラクター利用規約**の両方が適用される。

| キャラクター | 主な確認先 |
|---|---|
| ずんだもん | [ずんだもんキャラクター利用規約](https://zunko.jp/con_ongen_kiyaku.html) |
| 春日部つむぎ | [埼玉県春日部市公認](https://voicevox.hiroshiba.jp/dormitory/kasukabe-tsumugi/) |
| ナースロボ＿タイプT | 各キャラクターの利用規約を確認 |
| 青山龍星・波音リツ・玄野武宏 | 各キャラクターの利用規約を確認 |

**特に商用利用・配信・動画投稿する場合は各規約を必ず確認すること。**  
VOICEVOX 全体の利用規約: https://voicevox.hiroshiba.jp/

### ゲーム画面キャプチャについて

EchoMate はゲーム画面の特定領域をリアルタイムで解析する。  
画面キャプチャはメモリ内のみで処理し、スクリーンショットはファイルに保存しない。  
ただし、ゲームタイトルによってはスクリーンキャプチャを制限する利用規約を設けている場合がある。  
プレイ前に各ゲームの利用規約を確認すること。

---

## 今後の展望

### 近期（すぐ実装可能）
- **ゲーム API 連携** — Discord Rich Presence、Steam API からリアルイベント取得
- **感情表現の強化** — テンション値に応じてリアクションテンプレートを動的切替
- **GUI キャラクター選択** — ゲーム起動中にキャラクターをホットキーで切り替え

### 中期
- **OCR によるキルフィード読み取り** — テキスト認識で正確なキル/デス検出
- **Web UI** — ブラウザで設定・ログ確認・しゃべらせテストができる管理画面
- **カスタムキャラクター作成 UI** — `characters.json` を GUI で編集できるツール

### 長期
- **YOLO によるオブジェクト検出** — 敵キャラ・体力ゲージ・スキルアイコンをリアルタイム認識
- **マルチプレイヤー対応** — ボイスチャットの音声を分離して複数人の会話に反応
- **プレイスタイル分析** — 長期の行動ログから「最近デス多いね、疲れてる？」などの洞察
- **OBS 連携** — 配信・録画中に字幕・コメントとして EchoMate の発言をオーバーレイ表示

---

## ライセンス

EchoMate 本体のコード: MIT License

依存ライブラリおよび VOICEVOX キャラクターの利用は、それぞれの規約に従うこと。  
詳細は上記「利用規約・ライセンス情報」セクションを参照。
