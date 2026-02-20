#!/usr/bin/env python3
import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_dotenv() -> None:
    for candidate in (Path.cwd() / ".env", Path.cwd() / "vv-speaker-box-logic/.env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            row = line.strip()
            if not row or row.startswith("#") or "=" not in row:
                continue
            key, value = row.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if value.startswith(("'", '"')) and value.endswith(("'", '"')):
                value = value[1:-1]
            os.environ.setdefault(key, value)
        break


class VoicevoxClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _json_request(
        self, method: str, path: str, query: Dict[str, Any], body: Optional[bytes]
    ) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method=method, data=body)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.loads(res.read().decode("utf-8"))

    def _bytes_request(
        self, method: str, path: str, query: Dict[str, Any], body: Optional[bytes]
    ) -> bytes:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method=method, data=body)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.read()

    def resolve_speaker_id(self, speaker_name: str) -> int:
        speakers = self._json_request("GET", "/speakers", {}, None)
        for speaker in speakers:
            if speaker.get("name") != speaker_name:
                continue
            styles = speaker.get("styles", [])
            if styles:
                return int(styles[0]["id"])
        raise RuntimeError(f"Speaker not found: {speaker_name}")

    def synthesize(self, text: str, speaker_id: int) -> bytes:
        query = self._json_request(
            "POST", "/audio_query", {"text": text, "speaker": speaker_id}, None
        )
        return self._bytes_request(
            "POST",
            "/synthesis",
            {"speaker": speaker_id},
            json.dumps(query, ensure_ascii=False).encode("utf-8"),
        )


def split_sentences(text: str) -> List[str]:
    normalized = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if not normalized:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?])", normalized) if p.strip()]
    return parts or [normalized]


def detect_player() -> List[str]:
    if shutil.which("pw-play"):
        return ["pw-play"]
    if shutil.which("paplay"):
        return ["paplay"]
    if shutil.which("aplay"):
        return ["aplay", "-q"]
    raise RuntimeError("No player found. Install one of: pw-play, paplay, aplay")


def play_wav_bytes(player_cmd: List[str], wav_data: bytes) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fp:
        fp.write(wav_data)
        temp_path = fp.name
    try:
        subprocess.run(player_cmd + [temp_path], check=True)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Pseudo-stream playback sample: synthesize sentence-by-sentence and play immediately."
    )
    parser.add_argument("--text", required=True, help="Input text to speak")
    parser.add_argument(
        "--voicevox-url",
        default=os.getenv("VOICEVOX_URL", "http://127.0.0.1:50021"),
    )
    parser.add_argument("--speaker-name", default=os.getenv("SPEAKER_NAME", "冥鳴ひまり"))
    args = parser.parse_args()

    client = VoicevoxClient(args.voicevox_url)
    speaker_id = client.resolve_speaker_id(args.speaker_name)
    segments = split_sentences(args.text)
    if not segments:
        raise RuntimeError("Empty text")

    player_cmd = detect_player()
    q: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=2)

    def synth_worker() -> None:
        try:
            for segment in segments:
                wav = client.synthesize(segment, speaker_id)
                q.put(wav)
        finally:
            q.put(None)

    thread = threading.Thread(target=synth_worker, daemon=True)
    thread.start()

    idx = 1
    while True:
        wav = q.get()
        if wav is None:
            break
        print(f"[play {idx}/{len(segments)}] {segments[idx - 1]}")
        play_wav_bytes(player_cmd, wav)
        idx += 1
    thread.join(timeout=1)


if __name__ == "__main__":
    main()
