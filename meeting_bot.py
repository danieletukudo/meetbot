#!/usr/bin/env python3
"""Join a Google Meet, Zoom, or Microsoft Teams call with Recall.ai and transcribe it."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_REGION = "us-east-1"
POLL_SECONDS = 3
TRANSCRIPT_RETRY_SECONDS = 5
TRANSCRIPT_RETRIES = 12
TERMINAL_STATUSES = {"done", "fatal"}
STATUS_HINTS = {
    "joining_call": "Joining the meeting...",
    "in_waiting_room": "In the waiting room — admit the bot if the host is prompted.",
    "in_call_not_recording": "In the call, waiting to start recording...",
    "recording_permission_allowed": "Recording permission granted.",
    "recording_permission_denied": "Recording permission denied. The host must allow recording.",
    "in_call_recording": "In the meeting and transcribing.",
    "call_ended": "Meeting ended. Waiting for the transcript to be ready...",
    "done": "Bot finished. Fetching transcript...",
    "fatal": "Bot failed.",
}

MEETING_PATTERNS = (
    ("Google Meet", re.compile(r"meet\.google\.com/", re.I)),
    ("Zoom", re.compile(r"zoom\.us/|zoom\.com/", re.I)),
    ("Microsoft Teams", re.compile(r"teams\.microsoft\.com|teams\.live\.com", re.I)),
)


class RecallError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def detect_platform(meeting_url: str) -> str | None:
    for name, pattern in MEETING_PATTERNS:
        if pattern.search(meeting_url):
            return name
    return None


def auth_header(api_key: str) -> str:
    key = api_key.strip()
    if key.lower().startswith("token "):
        return key
    return f"Token {key}"


def api_request(
    method: str,
    url: str,
    api_key: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    data = None
    headers = {
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = auth_header(api_key)
    if extra_headers:
        headers.update(extra_headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RecallError(
            explain_http_error(exc.code, body),
            status=exc.code,
            body=body,
        ) from exc
    except urllib.error.URLError as exc:
        raise RecallError(f"Could not reach Recall.ai: {exc.reason}") from exc


def explain_http_error(status: int, body: str) -> str:
    if status == 401:
        return "Recall.ai rejected the API key (401). Check RECALL_API_KEY and RECALL_REGION."
    if status == 507:
        return (
            "Recall.ai has no spare bots right now (507). Wait a minute and retry, "
            "or schedule the bot ahead of time."
        )
    snippet = body.strip().replace("\n", " ")
    if len(snippet) > 400:
        snippet = snippet[:400] + "..."
    return f"Recall.ai request failed ({status}): {snippet or 'no response body'}"


def download_json(url: str, timeout: int = 60) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_status(bot: dict[str, Any]) -> dict[str, Any] | None:
    changes = bot.get("status_changes") or []
    if not changes:
        return None
    return changes[-1]


def status_code(bot: dict[str, Any]) -> str | None:
    status = latest_status(bot)
    if not status:
        return None
    return status.get("code")


def format_clock(seconds: float | None) -> str:
    if seconds is None:
        return "??:??:??"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def utterance_text(words: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for word in words:
        text = str(word.get("text") or "")
        if not text:
            continue
        if parts and not text.startswith(("'", ",", ".", "?", "!", ":", ";")):
            parts.append(" ")
        parts.append(text)
    return "".join(parts).strip()


def transcript_entries(segments: list[dict[str, Any]]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for segment in segments:
        participant = segment.get("participant") or {}
        speaker = participant.get("name") or "Unknown speaker"
        words = segment.get("words") or []
        text = utterance_text(words)
        if not text:
            continue
        start = None
        if words:
            start = (words[0].get("start_timestamp") or {}).get("relative")
        entries.append(
            {
                "time": format_clock(start),
                "speaker": speaker,
                "text": text,
            }
        )
    return entries


def format_transcript(segments: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{entry['time']}] {entry['speaker']}: {entry['text']}"
        for entry in transcript_entries(segments)
    ).strip()


def transcript_url_from_bot(bot: dict[str, Any]) -> str | None:
    for recording in bot.get("recordings") or []:
        shortcuts = recording.get("media_shortcuts") or {}
        transcript = shortcuts.get("transcript") or {}
        data = transcript.get("data") or {}
        url = data.get("download_url")
        if url:
            return url
    return None


class MeetingBot:
    def __init__(self, api_key: str, region: str, output_dir: Path):
        self.api_key = api_key
        self.base_url = f"https://{region}.recall.ai/api/v1"
        self.output_dir = output_dir
        self.bot_id: str | None = None
        self._leave_requested = False

    def create(
        self,
        meeting_url: str,
        bot_name: str,
        waiting_room_timeout: int,
        everyone_left_timeout: int,
    ) -> dict[str, Any]:
        payload = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "recording_config": {
                "transcript": {
                    "provider": {
                        "recallai_streaming": {
                            "mode": "prioritize_accuracy",
                            "language_code": "auto",
                        }
                    },
                    "diarization": {
                        "use_separate_streams_when_available": True,
                    },
                }
            },
            "automatic_leave": {
                "waiting_room_timeout": waiting_room_timeout,
                "noone_joined_timeout": waiting_room_timeout,
                "everyone_left_timeout": {
                    "timeout": everyone_left_timeout,
                    "activate_after": 1,
                },
            },
        }
        bot = api_request("POST", f"{self.base_url}/bot/", self.api_key, payload)
        self.bot_id = bot["id"]
        return bot

    def retrieve(self) -> dict[str, Any]:
        if not self.bot_id:
            raise RecallError("No bot has been created yet.")
        return api_request("GET", f"{self.base_url}/bot/{self.bot_id}/", self.api_key)

    def leave(self) -> None:
        if not self.bot_id or self._leave_requested:
            return
        self._leave_requested = True
        try:
            api_request("POST", f"{self.base_url}/bot/{self.bot_id}/leave_call/", self.api_key)
            log("Asked the bot to leave the meeting.")
        except RecallError as exc:
            self._leave_requested = False
            log(f"Could not send leave request: {exc}")
            raise

    def wait_until_done(self) -> dict[str, Any]:
        last_code: str | None = None
        while True:
            bot = self.retrieve()
            code = status_code(bot)
            status = latest_status(bot) or {}
            if code and code != last_code:
                hint = STATUS_HINTS.get(code, f"Status: {code}")
                sub = status.get("sub_code")
                message = status.get("message")
                extra = []
                if sub:
                    extra.append(str(sub))
                if message:
                    extra.append(str(message))
                suffix = f" ({'; '.join(extra)})" if extra else ""
                log(f"{hint}{suffix}")
                last_code = code
            if code in TERMINAL_STATUSES:
                return bot
            time.sleep(POLL_SECONDS)

    def fetch_transcript(self, bot: dict[str, Any], retries: int = TRANSCRIPT_RETRIES) -> list[dict[str, Any]]:
        for attempt in range(1, retries + 1):
            url = transcript_url_from_bot(bot)
            if url:
                data = download_json(url)
                if isinstance(data, list):
                    return data
                raise RecallError("Transcript download was not a JSON list.")
            if attempt < retries:
                log("Transcript file is not ready yet, retrying...")
                time.sleep(TRANSCRIPT_RETRY_SECONDS)
                bot = self.retrieve()
        raise RecallError(
            "The meeting finished but no transcript was produced. "
            "This usually means nobody spoke, recording was denied, or the bot never joined."
        )

    def save_transcript(
        self,
        segments: list[dict[str, Any]],
        meeting_url: str,
        platform: str | None,
    ) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = f"{stamp}_{self.bot_id}"
        json_path = self.output_dir / f"{stem}.json"
        text_path = self.output_dir / f"{stem}.txt"
        readable = format_transcript(segments)
        json_path.write_text(json.dumps(segments, indent=2), encoding="utf-8")
        header = [
            f"Meeting: {meeting_url}",
            f"Platform: {platform or 'unknown'}",
            f"Bot ID: {self.bot_id}",
            f"Saved at: {stamp}",
            "",
        ]
        text_path.write_text("\n".join(header) + (readable or "[no speech detected]") + "\n", encoding="utf-8")
        return text_path, json_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a Recall.ai bot into a Google Meet, Zoom, or Teams meeting and save the transcript.",
    )
    parser.add_argument("meeting_url", nargs="?", help="Meeting link. If omitted, you will be prompted.")
    parser.add_argument("--name", default="Transcription Bot", help="Display name shown in the meeting.")
    parser.add_argument(
        "--region",
        default=os.environ.get("RECALL_REGION", DEFAULT_REGION),
        help="Recall.ai region, for example us-east-1, us-west-2, eu-central-1.",
    )
    parser.add_argument(
        "--output-dir",
        default="transcripts",
        help="Directory where transcript .txt and .json files are written.",
    )
    parser.add_argument(
        "--waiting-room-timeout",
        type=int,
        default=600,
        help="Seconds to wait in the waiting room before giving up (default: 600).",
    )
    parser.add_argument(
        "--everyone-left-timeout",
        type=int,
        default=5,
        help="Seconds to stay after everyone else leaves (default: 5).",
    )
    return parser.parse_args()


def require_api_key() -> str:
    api_key = os.environ.get("RECALL_API_KEY") or os.environ.get("RECALLAI_API_KEY")
    if api_key:
        return api_key
    raise RecallError(
        "Missing RECALL_API_KEY. Copy .env.example to .env and paste your key from "
        "https://www.recall.ai (Developers → API keys)."
    )


def prompt_meeting_url() -> str:
    try:
        url = input("Paste a Google Meet, Zoom, or Teams link: ").strip()
    except EOFError:
        url = ""
    if not url:
        raise RecallError("No meeting URL provided.")
    return url


def main() -> int:
    load_dotenv(Path(".env"))
    args = parse_args()
    meeting_url = args.meeting_url or prompt_meeting_url()
    platform = detect_platform(meeting_url)
    if platform is None:
        log("This does not look like a Google Meet, Zoom, or Teams URL. Sending it anyway.")
    else:
        log(f"Detected {platform}.")

    client = MeetingBot(
        api_key=require_api_key(),
        region=args.region,
        output_dir=Path(args.output_dir),
    )

    def handle_interrupt(signum: int, frame: Any) -> None:
        log("Ctrl+C received. Leaving the meeting and waiting for the transcript...")
        try:
            client.leave()
        except RecallError as exc:
            log(f"Could not send leave request: {exc}")

    signal.signal(signal.SIGINT, handle_interrupt)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_interrupt)

    log(f"Sending '{args.name}' into the meeting...")
    bot = client.create(
        meeting_url=meeting_url,
        bot_name=args.name,
        waiting_room_timeout=args.waiting_room_timeout,
        everyone_left_timeout=args.everyone_left_timeout,
    )
    log(f"Bot ID: {bot['id']}")
    log("Stay in this terminal. Admit the bot if the meeting has a waiting room.")

    bot = client.wait_until_done()
    code = status_code(bot)
    if code == "fatal":
        status = latest_status(bot) or {}
        reason = status.get("sub_code") or status.get("message") or "unknown error"
        raise RecallError(f"The bot shut down: {reason}")

    log("Downloading transcript...")
    segments = client.fetch_transcript(bot)
    readable = format_transcript(segments)
    print()
    print("========== TRANSCRIPT ==========")
    print(readable or "[no speech detected]")
    print("================================")
    print()
    text_path, json_path = client.save_transcript(segments, meeting_url, platform)
    log(f"Saved readable transcript: {text_path}")
    log(f"Saved raw transcript JSON: {json_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RecallError as exc:
        log(f"Error: {exc}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        log("Stopped.")
        raise SystemExit(130)
