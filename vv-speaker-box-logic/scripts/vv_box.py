#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import json
import logging
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


FALLBACK_REPLY = (
    "今は情報が少ないから、私の方で要点を先にまとめるわ。"
    "まず優先順位を一つに絞って、短い手順から試すのが確実よ。"
)

SYSTEM_PROMPT = (
    "あなたは冥鳴ひまりとして話す。"
    "落ち着き・知的・少し余裕のある女性の口調で、一人称は私。"
    "返答は80〜160文字、2〜3文、結論→理由→軽い補足の順。"
    "箇条書きとMarkdownを禁止し、返答本文のみを出力。"
    "情報不足でも質問返しせず、仮定して短く答える。"
)


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


@dataclass
class Config:
    voicevox_url: str
    speaker_name: str
    llm_timeout_sec: int
    min_chars: int
    max_chars: int
    queue_max: int
    lock_path: Path
    llm_command: str
    player_command: str
    stream_playback: bool

    @staticmethod
    def from_env() -> "Config":
        def as_bool(value: str, default: bool = False) -> bool:
            if value is None:
                return default
            return value.strip().lower() in {"1", "true", "yes", "on"}

        return Config(
            voicevox_url=os.getenv("VOICEVOX_URL", "http://127.0.0.1:50021"),
            speaker_name=os.getenv("SPEAKER_NAME", "冥鳴ひまり"),
            llm_timeout_sec=int(os.getenv("LLM_TIMEOUT_SEC", "15")),
            min_chars=int(os.getenv("MIN_CHARS", "80")),
            max_chars=int(os.getenv("MAX_CHARS", "160")),
            queue_max=int(os.getenv("QUEUE_MAX", "10")),
            lock_path=Path(os.getenv("LOCK_PATH", "/tmp/vv-speaker.lock")),
            llm_command=os.getenv("LLM_COMMAND", "gemini -p"),
            player_command=os.getenv("PLAYER_COMMAND", ""),
            stream_playback=as_bool(os.getenv("STREAM_PLAYBACK", "true"), default=True),
        )


class ProcessLock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.fp = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = open(self.lock_path, "w", encoding="utf-8")
        fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fp:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            self.fp.close()


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
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode("utf-8"))

    def _bytes_request(
        self, method: str, path: str, query: Dict[str, Any], body: Optional[bytes]
    ) -> bytes:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method=method, data=body)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.read()

    def resolve_speaker_id(self, speaker_name: str) -> int:
        speakers = self._json_request("GET", "/speakers", {}, None)
        for speaker in speakers:
            if speaker.get("name") != speaker_name:
                continue
            styles = speaker.get("styles", [])
            if not styles:
                break
            return int(styles[0]["id"])
        raise RuntimeError(f"Speaker not found: {speaker_name}")

    def get_speakers(self) -> List[Dict[str, Any]]:
        return self._json_request("GET", "/speakers", {}, None)

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


def normalize_text(text: str, min_chars: int, max_chars: int) -> Optional[str]:
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?:^|\s)[\-\*]\s+", " ", text).strip()
    if not text:
        return None
    if text.endswith("?") or text.endswith("？"):
        return None
    if not re.search(r"[。！？]$", text):
        text += "。"

    if len(text) > max_chars:
        head = text[: max_chars + 1]
        idx = max(head.rfind("。"), head.rfind("！"), head.rfind("？"))
        if idx >= min_chars - 1:
            text = head[: idx + 1]
        else:
            text = text[:max_chars].rstrip() + "。"

    if len(text) < min_chars:
        return None
    return text


def normalize_direct_text(text: str) -> Optional[str]:
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?:^|\s)[\-\*]\s+", " ", text).strip()
    if not text:
        return None
    if not re.search(r"[。！？!?]$", text):
        text += "。"
    return text


def run_llm(user_text: str, cfg: Config) -> str:
    prompt = f"{SYSTEM_PROMPT}\n\nユーザー入力: {user_text}\n\n返答:"
    cmd = shlex.split(cfg.llm_command) + [prompt]
    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=cfg.llm_timeout_sec,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "LLM command failed")
    out = completed.stdout.strip()
    if not out:
        raise RuntimeError("LLM returned empty response")
    return out


def split_sentences(text: str) -> List[str]:
    normalized = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if not normalized:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?])", normalized) if p.strip()]
    return parts or [normalized]


def detect_player_command(user_command: str) -> List[str]:
    if user_command.strip():
        return shlex.split(user_command)
    if shutil.which("pw-play"):
        return ["pw-play"]
    if shutil.which("paplay"):
        return ["paplay"]
    if shutil.which("aplay"):
        return ["aplay", "-q"]
    raise RuntimeError("No audio player found. Set PLAYER_COMMAND or install pw-play/paplay/aplay.")


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


class BoxLogic:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = VoicevoxClient(cfg.voicevox_url)
        self.logger = logging.getLogger("vv_box")
        self.player_cmd: Optional[List[str]] = None
        self._speaker_id_cache: Dict[str, int] = {}

    def resolve_speaker_id(self, speaker: Optional[Any]) -> int:
        if speaker is None or str(speaker).strip() == "":
            speaker = self.cfg.speaker_name
        if isinstance(speaker, int):
            return speaker
        speaker_str = str(speaker).strip()
        if speaker_str.isdigit():
            return int(speaker_str)
        cached = self._speaker_id_cache.get(speaker_str)
        if cached is not None:
            return cached
        resolved = self.client.resolve_speaker_id(speaker_str)
        self._speaker_id_cache[speaker_str] = resolved
        return resolved

    def health(self) -> Dict[str, Any]:
        try:
            speakers = self.client.get_speakers()
            speaker_id = self.resolve_speaker_id(self.cfg.speaker_name)
            return {
                "status": "ok",
                "voicevox": {
                    "reachable": True,
                    "speakers_count": len(speakers),
                },
                "default_speaker": {
                    "name": self.cfg.speaker_name,
                    "id": speaker_id,
                },
            }
        except Exception as exc:
            return {
                "status": "degraded",
                "voicevox": {"reachable": False},
                "default_speaker": {"name": self.cfg.speaker_name, "id": None},
                "error": str(exc),
            }

    def _make_reply(self, text: str, mode: str) -> Tuple[str, Dict[str, int], str]:
        llm_ms = 0
        source = "direct"
        if mode == "direct":
            normalized = normalize_direct_text(text)
            return (normalized or FALLBACK_REPLY, {"llm_ms": 0}, source)

        source = "llm"
        for _ in range(2):
            started = time.perf_counter()
            try:
                raw = run_llm(text, self.cfg)
            except Exception:
                raw = FALLBACK_REPLY
            llm_ms += int((time.perf_counter() - started) * 1000)
            normalized = normalize_text(raw, self.cfg.min_chars, self.cfg.max_chars)
            if normalized:
                return normalized, {"llm_ms": llm_ms}, source
        fallback = normalize_text(FALLBACK_REPLY, self.cfg.min_chars, self.cfg.max_chars)
        return (fallback or FALLBACK_REPLY, {"llm_ms": llm_ms}, "fallback")

    def process(
        self,
        text: str,
        mode: str = "llm",
        dry_run: bool = False,
        speaker: Optional[Any] = None,
    ) -> Dict[str, Any]:
        request_id = hashlib.md5(f"{time.time()}:{text}".encode("utf-8")).hexdigest()[:10]
        t0 = time.perf_counter()
        with ProcessLock(self.cfg.lock_path):
            speaker_id = self.resolve_speaker_id(speaker)
            reply_text, latency, reply_source = self._make_reply(text, mode)
            tts_ms = 0
            error = None
            played = False
            if not dry_run:
                try:
                    if self.player_cmd is None:
                        self.player_cmd = detect_player_command(self.cfg.player_command)
                    t1 = time.perf_counter()
                    if self.cfg.stream_playback:
                        segments = split_sentences(reply_text)
                        if not segments:
                            segments = [reply_text]

                        synth_queue: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=2)

                        def synth_worker() -> None:
                            try:
                                for segment in segments:
                                    synth_queue.put(
                                        self.client.synthesize(segment, speaker_id)
                                    )
                            finally:
                                synth_queue.put(None)

                        worker = threading.Thread(target=synth_worker, daemon=True)
                        worker.start()

                        while True:
                            wav = synth_queue.get()
                            if wav is None:
                                break
                            play_wav_bytes(self.player_cmd, wav)
                        worker.join(timeout=1)
                    else:
                        wav = self.client.synthesize(reply_text, speaker_id)
                        play_wav_bytes(self.player_cmd, wav)
                    tts_ms = int((time.perf_counter() - t1) * 1000)
                    played = True
                except Exception as exc:
                    error = str(exc)

            total_ms = int((time.perf_counter() - t0) * 1000)
            result = {
                "request_id": request_id,
                "reply_text": reply_text,
                "played": played,
                "latency_ms": {
                    "total_ms": total_ms,
                    "llm_ms": latency.get("llm_ms", 0),
                    "tts_ms": tts_ms,
                },
                "speaker_id": speaker_id,
                "reply_source": reply_source,
                "input_chars": len(text),
                "output_chars": len(reply_text),
                "mode": mode,
                "error": error,
            }
            self.logger.info(json.dumps(result, ensure_ascii=False))
            return result


class APITask:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.done = threading.Event()
        self.result: Dict[str, Any] = {}


class APIService:
    def __init__(self, logic: BoxLogic, queue_max: int):
        self.logic = logic
        self.q: queue.Queue[APITask] = queue.Queue(maxsize=queue_max)
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()

    def _worker(self):
        while True:
            task = self.q.get()
            try:
                payload = task.payload
                task.result = self.logic.process(
                    text=str(payload.get("text", "")),
                    mode=str(payload.get("mode", "llm")),
                    dry_run=bool(payload.get("dry_run", False)),
                    speaker=payload.get("speaker"),
                )
            except Exception as exc:
                task.result = {"error": str(exc)}
            finally:
                task.done.set()
                self.q.task_done()

    def enqueue(self, payload: Dict[str, Any]) -> APITask:
        task = APITask(payload)
        self.q.put_nowait(task)
        return task


def make_handler(service: APIService):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: Dict[str, Any]):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/health":
                health = service.logic.health()
                code = 200 if health.get("status") == "ok" else 503
                self._send(code, health)
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/speak":
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw or "{}")
                if "text" not in payload or not str(payload["text"]).strip():
                    self._send(400, {"error": "text is required"})
                    return
                try:
                    task = service.enqueue(payload)
                except queue.Full:
                    self._send(429, {"error": "queue full"})
                    return
                task.done.wait()
                if "error" in task.result and task.result.get("reply_text") is None:
                    self._send(500, task.result)
                    return
                self._send(200, task.result)
            except Exception as exc:
                self._send(500, {"error": str(exc)})

        def log_message(self, format: str, *args):
            return

    return Handler


def run_cli(logic: BoxLogic):
    print("vv_box cli ready. one line per request. Ctrl-D to stop.")
    for line in os.sys.stdin:
        text = line.strip()
        if not text:
            continue
        result = logic.process(text=text, mode="llm", dry_run=False)
        print(json.dumps(result, ensure_ascii=False))


def run_api(logic: BoxLogic, host: str, port: int):
    service = APIService(logic, queue_max=logic.cfg.queue_max)
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"vv_box api listening on http://{host}:{port}")
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="VV speaker box logic runner")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_cli = sub.add_parser("cli", help="run interactive CLI mode")
    p_cli.set_defaults(fn="cli")

    p_api = sub.add_parser("api", help="run HTTP API mode")
    p_api.add_argument("--host", default="127.0.0.1")
    p_api.add_argument("--port", type=int, default=8080)
    p_api.set_defaults(fn="api")

    args = parser.parse_args()

    load_dotenv()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
    cfg = Config.from_env()
    logic = BoxLogic(cfg)

    if args.fn == "cli":
        run_cli(logic)
        return
    run_api(logic, args.host, args.port)


if __name__ == "__main__":
    main()
