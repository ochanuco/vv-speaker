#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


def _load_vv_box_module():
    module_path = Path(__file__).with_name("vv_box.py")
    spec = importlib.util.spec_from_file_location("vv_box_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VV_BOX = _load_vv_box_module()
MCP = FastMCP("vv-speaker")
_LOGIC = None


def get_logic():
    global _LOGIC
    if _LOGIC is None:
        VV_BOX.load_dotenv()
        cfg = VV_BOX.Config.from_env()
        _LOGIC = VV_BOX.BoxLogic(cfg)
    return _LOGIC


@MCP.tool(
    description=(
        "Default voice output tool. Use this first for normal conversational replies unless "
        "the user explicitly asks for text-only output. Plays audio immediately without saving WAV."
    )
)
def speak(text: str, mode: str = "direct", dry_run: bool = False) -> dict[str, Any]:
    logic = get_logic()
    return logic.process(text=text, mode=mode, dry_run=dry_run)


@MCP.tool(
    description=(
        "Alias of speak for clearer intent. Prefer this for natural chat responses that should be spoken."
    )
)
def say_aloud(text: str, mode: str = "direct", dry_run: bool = False) -> dict[str, Any]:
    logic = get_logic()
    return logic.process(text=text, mode=mode, dry_run=dry_run)


@MCP.tool(description="Get basic runtime status and configured VOICEVOX URL.")
def status() -> dict[str, Any]:
    VV_BOX.load_dotenv()
    cfg = VV_BOX.Config.from_env()
    return {
        "service": "vv-speaker",
        "voicevox_url": cfg.voicevox_url,
        "speaker_name": cfg.speaker_name,
        "stream_playback": cfg.stream_playback,
    }


if __name__ == "__main__":
    MCP.run()
