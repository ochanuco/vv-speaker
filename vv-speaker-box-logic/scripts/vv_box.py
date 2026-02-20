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
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


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
    output_dir: Path
    llm_timeout_sec: int
    min_chars: int
    max_chars: int
    queue_max: int
    lock_path: Path
    llm_command: str

    @staticmethod
    def from_env() -> "Config":
        return Config(
            voicevox_url=os.getenv("VOICEVOX_URL", "http://127.0.0.1:50021"),
            speaker_name=os.getenv("SPEAKER_NAME", "冥鳴ひまり"),
            output_dir=Path(os.getenv("OUTPUT_DIR", "/mnt/c/voicebox/output")),
            llm_timeout_sec=int(os.getenv("LLM_TIMEOUT_SEC", "15")),
            min_chars=int(os.getenv("MIN_CHARS", "80")),
            max_chars=int(os.getenv("MAX_CHARS", "160")),
            queue_max=int(os.getenv("QUEUE_MAX", "10")),
            lock_path=Path(os.getenv("LOCK_PATH", "/tmp/vv-speaker.lock")),
            llm_command=os.getenv("LLM_COMMAND", "gemini -p"),
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


def save_wav_bytes(output_dir: Path, text: str, wav_data: bytes) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    path = output_dir / f"{stamp}_{digest}.wav"
    path.write_bytes(wav_data)
    return str(path)


class BoxLogic:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = VoicevoxClient(cfg.voicevox_url)
        self.speaker_id = self.client.resolve_speaker_id(cfg.speaker_name)
        self.logger = logging.getLogger("vv_box")

    def _make_reply(self, text: str, mode: str) -> Tuple[str, Dict[str, int], str]:
        llm_ms = 0
        source = "direct"
        if mode == "direct":
            normalized = normalize_text(text, self.cfg.min_chars, self.cfg.max_chars)
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

    def process(self, text: str, mode: str = "llm", dry_run: bool = False) -> Dict[str, Any]:
        request_id = hashlib.md5(f"{time.time()}:{text}".encode("utf-8")).hexdigest()[:10]
        t0 = time.perf_counter()
        with ProcessLock(self.cfg.lock_path):
            reply_text, latency, reply_source = self._make_reply(text, mode)
            wav_path = None
            tts_ms = 0
            error = None
            if not dry_run:
                try:
                    t1 = time.perf_counter()
                    wav = self.client.synthesize(reply_text, self.speaker_id)
                    tts_ms = int((time.perf_counter() - t1) * 1000)
                    wav_path = save_wav_bytes(self.cfg.output_dir, reply_text, wav)
                except Exception as exc:
                    error = str(exc)

            total_ms = int((time.perf_counter() - t0) * 1000)
            result = {
                "request_id": request_id,
                "reply_text": reply_text,
                "wav_path": wav_path,
                "latency_ms": {
                    "total_ms": total_ms,
                    "llm_ms": latency.get("llm_ms", 0),
                    "tts_ms": tts_ms,
                },
                "speaker_id": self.speaker_id,
                "reply_source": reply_source,
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
                self._send(200, {"status": "ok"})
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
