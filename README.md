# vv-speaker

WSL側で `Gemini -> VOICEVOX` をつなぎ、**WAV保存せず即再生**するプロジェクトです。

## 前提

- Python: `3.12+`
- `uv` インストール済み
- VOICEVOX Engine が起動済み（例: Proxmox上）
- 再生コマンドのどれかが使えること
  - `pw-play` / `paplay` / `aplay`

## セットアップ

```bash
uv sync
cp .env.example .env
```

`.env` の最低限:

```dotenv
VOICEVOX_URL=http://<VOICEVOX_HOST>:50021
SPEAKER_NAME=冥鳴ひまり
```

必要なら再生コマンドを固定:

```dotenv
PLAYER_COMMAND=aplay -q
```

## 使い方

### 1. APIサーバー起動

```bash
uv run python vv-speaker-box-logic/scripts/vv_box.py api --host 127.0.0.1 --port 8080
```

ヘルスチェック:

```bash
curl -s http://127.0.0.1:8080/health
```

### 2. 発話（API）

`mode=direct`: 入力文をそのまま整形して再生

```bash
curl -s -X POST http://127.0.0.1:8080/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"今の作業はここまでで十分よ。次の一手だけ残して休みましょう。","mode":"direct","dry_run":false}'
```

`mode=llm`: Gemini CLIで返答生成して再生

```bash
curl -s -X POST http://127.0.0.1:8080/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"短く進め方を提案して","mode":"llm","dry_run":false}'
```

レスポンス例:

```json
{
  "request_id": "xxxx",
  "reply_text": "....",
  "played": true,
  "latency_ms": { "total_ms": 1200, "llm_ms": 300, "tts_ms": 900 },
  "speaker_id": 14,
  "reply_source": "llm",
  "error": null
}
```

### 3. CLIモード

```bash
uv run python vv-speaker-box-logic/scripts/vv_box.py cli
```

1行入力ごとに即再生します。

## 疑似ストリーム再生サンプル

文ごとに先行合成し、できた順に再生します。

```bash
uv run python vv-speaker-box-logic/scripts/stream_play_sample.py \
  --text "今から文ごとに再生します。先頭の文から聞こえて、次の文は裏で合成します。"
```

## トラブルシュート

- `Connection refused`
  - `VOICEVOX_URL` が違うか、VOICEVOX Engine が停止中です。
- `No audio player found`
  - `pw-play`/`paplay`/`aplay` のいずれかをインストールするか、`.env` に `PLAYER_COMMAND` を設定してください。
- `reply_source: fallback`
  - Gemini CLIの実行に失敗しています。`LLM_COMMAND` と認証状態を確認してください。
