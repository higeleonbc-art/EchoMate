# EchoMate Modelfile ガイド

キャラクターの system_prompt を Ollama に焼き込んだカスタムモデルを作成できます。
毎回 system_prompt にトークンを使わなくて済むため、レスポンス速度が改善されます。

## ビルド方法

```bash
ollama create echomate-echo    -f presets/echomate_echo.Modelfile
ollama create echomate-michiko -f presets/echomate_michiko.Modelfile
ollama create echomate-kid     -f presets/echomate_kid.Modelfile
ollama create echomate-rei     -f presets/echomate_rei.Modelfile
```

## 使い方

`.env` の `LLM_MODEL` を変更するだけで適用されます。

```env
LLM_MODEL=echomate-echo
```
