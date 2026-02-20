---
name: vv-speaker-box-logic
description: Build and run the WSL-side VOICEVOX box logic for Himari-style speech output. Use when implementing or operating the pipeline: input intake (CLI/HTTP), Codex CLI reply generation, 80-160 char normalization, VOICEVOX HTTP synthesis, immediate playback without WAV persistence, single-worker execution, and basic telemetry.
---

# VV Speaker Box Logic

## Overview

Implement and operate the WSL orchestration layer that sits between user text and Proxmox-hosted VOICEVOX Engine.

## Workflow

1. Configure `.env` (`VOICEVOX_URL`, `SPEAKER_NAME`, `PLAYER_COMMAND`, `LLM_COMMAND`).
2. Start API mode and check `GET /health`.
3. Validate with `POST /speak` using `dry_run=true` first.
4. For normal conversation, use `mode=direct` (or `mode=auto`, which is treated as `direct`).
5. Use `mode=llm` only when Codex-generated responses are needed.

## Commands

```bash
# CLI mode
uv run python vv-speaker-box-logic/scripts/vv_box.py cli

# HTTP API mode
uv run python vv-speaker-box-logic/scripts/vv_box.py api --host 127.0.0.1 --port 8080
```

## Environment Variables

- `VOICEVOX_URL` default: `http://127.0.0.1:50021`
- `SPEAKER_NAME` default: `冥鳴ひまり`
- `LLM_TIMEOUT_SEC` default: `15`
- `MIN_CHARS` default: `80`
- `MAX_CHARS` default: `160`
- `QUEUE_MAX` default: `10`
- `LOCK_PATH` default: `/tmp/vv-speaker.lock`
- `LLM_COMMAND` default: `codex -p`
- `DEFAULT_PRESET` default: `himari`
- `PLAYER_COMMAND` default: `paplay` (`.env.example`), auto-detect fallback (`pw-play`/`paplay`/`aplay`/`ffplay`)
- `STREAM_PLAYBACK` default: `true`
- `LOG_LEVEL` default: `INFO`

## Notes

- The project preset is effectively `himari` only; unknown preset names fall back to `himari`.
- Keep one active synthesis at a time with file lock.
- If `PLAYER_COMMAND` is invalid or fails, the runtime retries with auto-detected players.
- If Codex CLI fails in `mode=llm`, the in-script fallback line is used and TTS continues.
- On VOICEVOX or playback failure, return text result with `played: false` and include `error`.
