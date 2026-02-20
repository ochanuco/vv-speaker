---
name: vv-speaker-box-logic
description: Build and run the WSL-side VOICEVOX box logic for Himari-style speech output. Use when implementing or operating the pipeline: input intake (CLI/HTTP), Gemini CLI reply generation, 80-160 char normalization, VOICEVOX HTTP synthesis, WAV output, single-worker execution, and basic telemetry.
---

# VV Speaker Box Logic

## Overview

Implement and operate the WSL orchestration layer that sits between user text and Proxmox-hosted VOICEVOX Engine.

## Workflow

1. Read `references/himari-constitution.md` and keep prompt constraints stable.
2. Configure environment variables for VOICEVOX URL, output path, and limits.
3. Run `scripts/vv_box.py` in `cli` mode for Phase1.
4. Run `scripts/vv_box.py` in `api` mode for Phase2-style `POST /speak`.
5. Prefer `dry_run=true` first to validate LLM/normalization before synthesis.

## Commands

```bash
# Phase1: CLI
python3 vv-speaker-box-logic/scripts/vv_box.py cli

# Phase2: HTTP API
python3 vv-speaker-box-logic/scripts/vv_box.py api --host 127.0.0.1 --port 8080
```

## Environment Variables

- `VOICEVOX_URL` default: `http://127.0.0.1:50021`
- `SPEAKER_NAME` default: `冥鳴ひまり`
- `OUTPUT_DIR` default: `/mnt/c/voicebox/output`
- `LLM_TIMEOUT_SEC` default: `15`
- `MIN_CHARS` default: `80`
- `MAX_CHARS` default: `160`
- `QUEUE_MAX` default: `10`
- `LOCK_PATH` default: `/tmp/vv-speaker.lock`
- `LLM_COMMAND` default: `gemini -p`
- `LOG_LEVEL` default: `INFO`

## Notes

- Keep one active synthesis at a time with file lock.
- If Gemini fails, use the in-script fallback line and continue TTS.
- If VOICEVOX fails, return text result with `wav_path: null` and include the error.
