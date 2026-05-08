# Riot Production API Key 申請ガイド

ADC Coach Hub を恒久運用するための Production Key 申請テンプレート。
Personal Key（24時間失効）のように毎日再発行する手間を回避できる。

---

## 申請ページ

1. https://developer.riotgames.com/ にRiotアカウントでログイン
2. 右上 **`REGISTER PRODUCT`** をクリック
3. **Product Type** で `Personal API Key` の隣の **`Personal Project`** を選択
   （※ Open-source / Educational 用途は Personal Project に該当）

---

## フォーム入力テンプレ

以下の英文を貼り付け→必要に応じて自分の情報に置換すれば通りやすい申請になります。

### Product Name

```
ADC Coach Hub
```

### Product Type

`Personal Project`

### Product Description

```
ADC Coach Hub is a personal League of Legends coaching tool focused on the
ADC (bot lane carry) role. It is designed for solo personal use to help me
analyze my own ranked matches and improve my mechanical and macro play.

The tool runs entirely locally on a Windows desktop machine using a
pywebview-based GUI. It has no server backend and no external data sharing.

Match data fetched from Riot APIs is stored only in local SQLite / JSON
files on my own machine and is never transmitted, published, redistributed,
or made publicly available.

The project source code is open source on GitHub at
https://github.com/{YOUR_GH_USER}/EchoMate (no API key is committed).
Anyone who clones it must use their own personal Riot API key.
```

### URL / Game Data Information URL

```
https://github.com/{YOUR_GH_USER}/EchoMate
```

### Reasons for Requesting

```
Personal Keys expire every 24 hours, which makes day-to-day use of a
self-coaching tool impractical. A Production Key with the standard
personal rate limits would let me reliably run match reviews after every
ranked game without re-issuing keys daily.
```

### How will players access this?

```
This is a personal-use tool. The only player who will use it is myself
(the developer / Riot account holder). The repository is public for
educational reference only; any clone will require the user to
register their own Riot Developer account and key.
```

### Endpoints Used

```
- /riot/account/v1/accounts/by-riot-id/{gameName}/{tagLine}
- /riot/account/v1/accounts/by-puuid/{puuid}
- /lol/match/v5/matches/by-puuid/{puuid}/ids
- /lol/match/v5/matches/{matchId}
- /lol/match/v5/matches/{matchId}/timeline
- /lol/summoner/v4/summoners/by-puuid/{puuid}
- /lol/league/v4/entries/by-puuid/{puuid}
```

### Rate Limits Required

```
Default personal limits are sufficient: 20 requests per second,
100 requests per 2 minutes. The tool does not poll continuously; it
only fetches data when the user opens the GUI or finishes a match.
```

### Data Storage

```
- Local SQLite (.coach_kpi.db) and JSON (.coach_profile.json,
  .coach_personal.json) files on my own machine.
- No data is sent to any external server.
- No PII (Personally Identifiable Information) other than my own Riot ID
  and PUUID is collected.
- All cached match data is only my own historical match history.
```

### Application is intended for

`Personal use only`

---

## 申請後の流れ

| ステップ | 期間目安 |
|---|---|
| 1. フォーム送信後の初期受領メール | 即時 |
| 2. Riot 担当者からの確認メール | 数日〜2週間 |
| 3. 追加質問が来る場合（無いことも多い） | 任意 |
| 4. 承認またはfeedback | 上記後 |

承認されると Production Key が developer dashboard に出るので、`.env` を:

```
RIOT_API_KEY=RGAPI-{new-production-key}
```

に入れ替えれば永続使用可能。Personal Key と違い再発行不要。

---

## 申請が通りやすくする tips

- **真面目に "Personal Project" を選ぶ**（Game-changing Product を選ばない）
- **GitHub リポを公開しておく**（透明性 +）
- **API key を絶対 commit しない**（承認後に取り消されるリスク）
- **データ外部送信ゼロを明言**（Riot は user data の取り扱いに敏感）
- **使用 endpoint を正直に列挙**（後から増やすと再申請になりうる）
- **Rate limit は default 範囲を希望**（高い limit は審査厳しい）

---

## 拒否されやすい NG ワード

- 「Discord bot で他人にも提供」 → User accountabilityが必要になりNG
- 「自動で対戦相手の情報を晒す」 → 利用規約違反
- 「Smurf検出 / アカウント比較」 → サードパーティ的でNG
- 商用 / 有料機能の言及 → Production用の別カテゴリ申請が必要

---

## 参考

- Riot Developer Portal: https://developer.riotgames.com/
- Riot API Policies: https://developer.riotgames.com/policies/general
- Application Process FAQ: dashboard上のFAQ参照
