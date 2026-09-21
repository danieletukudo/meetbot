#!/usr/bin/env python3
"""Tiny local server for the meeting-bot HTML UI + calendar sync."""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import calendar_sync
from faq import extract_faqs
from meeting_bot import (
    DEFAULT_REGION,
    STATUS_HINTS,
    MeetingBot,
    RecallError,
    detect_platform,
    latest_status,
    load_dotenv,
    require_api_key,
    status_code,
    transcript_entries,
)

ROOT = Path(__file__).resolve().parent
SESSIONS: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()


def new_client() -> MeetingBot:
    load_dotenv(ROOT / ".env")
    region = os.environ.get("RECALL_REGION", DEFAULT_REGION)
    return MeetingBot(require_api_key(), region, ROOT / "transcripts")


def public_base(handler: SimpleHTTPRequestHandler) -> str:
    host = handler.headers.get("Host") or "127.0.0.1:8765"
    return f"http://{host}"


def session_payload(bot_id: str, bot: dict[str, Any] | None = None) -> dict[str, Any]:
    with LOCK:
        session = dict(SESSIONS.get(bot_id) or {})
    status = latest_status(bot or {}) or {}
    code = status.get("code") or session.get("status")
    payload = {
        "id": bot_id,
        "meeting_url": session.get("meeting_url"),
        "platform": session.get("platform"),
        "bot_name": session.get("bot_name"),
        "status": code,
        "hint": STATUS_HINTS.get(code or "", f"Status: {code}" if code else "Waiting for status..."),
        "sub_code": status.get("sub_code"),
        "message": status.get("message"),
        "transcript": session.get("transcript"),
        "faqs": session.get("faqs"),
        "error": session.get("error"),
    }
    return payload


def cache_transcript(client: MeetingBot, bot: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if not session.get("transcript"):
            segments = client.fetch_transcript(bot, retries=4)
            entries = transcript_entries(segments)
            text_path, json_path = client.save_transcript(
                segments,
                session["meeting_url"],
                session.get("platform"),
            )
            transcript_text = (
                "\n".join(f"[{row['time']}] {row['speaker']}: {row['text']}" for row in entries)
                or "[no speech detected]"
            )
            session["transcript"] = {
                "entries": entries,
                "text": transcript_text,
                "text_path": str(text_path),
                "json_path": str(json_path),
            }

        transcript = session["transcript"]
        needs_faqs = not session.get("faqs") or (
            not (session["faqs"].get("items") or []) and session["faqs"].get("error")
        )
        if needs_faqs:
            load_dotenv(ROOT / ".env")
            faqs = extract_faqs(transcript.get("entries") or [], transcript.get("text") or "")
            text_path = transcript.get("text_path")
            if text_path:
                faq_path = Path(text_path).with_name(Path(text_path).stem + "_faqs.json")
                faq_path.write_text(json.dumps(faqs, indent=2), encoding="utf-8")
                faqs["path"] = str(faq_path)
            session["faqs"] = faqs
        return transcript
    except RecallError as exc:
        session["error"] = str(exc)
        return None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        if self.path.startswith("/api/"):
            super().log_message(format, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self.serve_index()
            return
        if path == "/calendar/connected":
            self.serve_calendar_result(ok=True)
            return
        if path == "/calendar/error":
            self.serve_calendar_result(ok=False)
            return
        if path == "/api/calendar/status":
            self.handle_calendar_status()
            return
        if path == "/api/calendar/connect/google":
            self.handle_calendar_connect("google")
            return
        if path == "/api/calendar/connect/outlook":
            self.handle_calendar_connect("outlook")
            return
        if path == "/api/calendar/meetings":
            self.handle_calendar_meetings()
            return
        if path.startswith("/api/bots/"):
            bot_id = path.removeprefix("/api/bots/").strip("/")
            if not bot_id:
                self.send_json({"error": "Missing bot id."}, 400)
                return
            self.handle_status(bot_id)
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/join":
            self.handle_join()
            return
        if path == "/api/calendar/enable-all":
            self.handle_calendar_enable_all()
            return
        if path == "/api/calendar/refresh":
            self.handle_calendar_refresh()
            return
        if path == "/api/calendar/disconnect":
            self.handle_calendar_disconnect()
            return
        if path.startswith("/api/calendar/meetings/") and path.endswith("/record"):
            meeting_id = path.removeprefix("/api/calendar/meetings/").removesuffix("/record").strip("/")
            self.handle_meeting_record(meeting_id)
            return
        if path.endswith("/leave") and path.startswith("/api/bots/"):
            bot_id = path.removeprefix("/api/bots/").removesuffix("/leave").strip("/")
            self.handle_leave(bot_id)
            return
        if path.endswith("/faqs") and path.startswith("/api/bots/"):
            bot_id = path.removeprefix("/api/bots/").removesuffix("/faqs").strip("/")
            self.handle_regenerate_faqs(bot_id)
            return
        self.send_error(404, "Not found")

    def serve_index(self) -> None:
        html = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html)

    def serve_calendar_result(self, ok: bool) -> None:
        title = "Calendar connected" if ok else "Calendar connection failed"
        message = (
            "Your calendar is linked. Closing this tab…"
            if ok
            else "Something went wrong connecting the calendar. You can close this tab and try again."
        )
        flag = "true" if ok else "false"
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font:16px/1.4 system-ui;padding:40px;background:#f3eee4;color:#1b1814">
  <h1>{title}</h1>
  <p>{message}</p>
  <script>
    try {{ localStorage.setItem("calendar_connect_result", {flag}); }} catch (e) {{}}
    if (window.opener) {{
      window.opener.postMessage({{ type: "calendar-connected", ok: {flag} }}, "*");
    }}
    setTimeout(function () {{ window.close(); }}, 1200);
  </script>
</body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if ok:
            try:
                token = calendar_sync.ensure_token(ROOT)
                calendar_sync.set_record_all(token)
                calendar_sync.refresh_meetings(token)
            except RecallError:
                pass

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise RecallError("Request body must be JSON.") from exc
        if not isinstance(data, dict):
            raise RecallError("Request body must be a JSON object.")
        return data

    def handle_join(self) -> None:
        try:
            body = self.read_json()
            meeting_url = str(body.get("meeting_url") or "").strip()
            bot_name = str(body.get("bot_name") or "Transcription Bot").strip() or "Transcription Bot"
            if not meeting_url:
                raise RecallError("Paste a meeting link first.")
            platform = detect_platform(meeting_url)
            if platform is None:
                raise RecallError("Use a Google Meet, Zoom, or Microsoft Teams link.")
            client = new_client()
            bot = client.create(
                meeting_url=meeting_url,
                bot_name=bot_name,
                waiting_room_timeout=600,
                everyone_left_timeout=5,
            )
            bot_id = bot["id"]
            with LOCK:
                SESSIONS[bot_id] = {
                    "meeting_url": meeting_url,
                    "platform": platform,
                    "bot_name": bot_name,
                    "status": status_code(bot),
                    "transcript": None,
                    "faqs": None,
                    "error": None,
                }
            payload = session_payload(bot_id, bot)
            payload["hint"] = f"Sending {bot_name} into {platform}..."
            self.send_json(payload, 201)
        except RecallError as exc:
            self.send_json({"error": str(exc)}, exc.status if exc.status and exc.status >= 400 else 400)

    def handle_status(self, bot_id: str) -> None:
        with LOCK:
            session = SESSIONS.get(bot_id)
        if not session:
            self.send_json({"error": "Unknown bot. Join a meeting from this page first."}, 404)
            return
        try:
            client = new_client()
            client.bot_id = bot_id
            bot = client.retrieve()
            code = status_code(bot)
            with LOCK:
                session = SESSIONS[bot_id]
                session["status"] = code
                should_cache = code == "done" and not session.get("error")
                if code == "fatal" and not session.get("error"):
                    status = latest_status(bot) or {}
                    session["error"] = (
                        status.get("sub_code") or status.get("message") or "The bot shut down."
                    )
            if should_cache:
                with LOCK:
                    session = SESSIONS[bot_id]
                cache_transcript(client, bot, session)
                with LOCK:
                    SESSIONS[bot_id] = session
            self.send_json(session_payload(bot_id, bot))
        except RecallError as exc:
            self.send_json({"error": str(exc)}, exc.status if exc.status and exc.status >= 400 else 502)

    def handle_leave(self, bot_id: str) -> None:
        with LOCK:
            if bot_id not in SESSIONS:
                self.send_json({"error": "Unknown bot."}, 404)
                return
        try:
            client = new_client()
            client.bot_id = bot_id
            client.leave()
            self.send_json({"ok": True, "id": bot_id, "hint": "Asked the bot to leave. Waiting for the transcript..."})
        except RecallError as exc:
            self.send_json({"error": str(exc)}, exc.status if exc.status and exc.status >= 400 else 502)

    def handle_regenerate_faqs(self, bot_id: str) -> None:
        load_dotenv(ROOT / ".env")
        with LOCK:
            session = SESSIONS.get(bot_id)
            if not session:
                self.send_json({"error": "Unknown bot."}, 404)
                return
            transcript = session.get("transcript")
            if not transcript:
                self.send_json({"error": "No transcript yet. Finish a meeting first."}, 400)
                return
            entries = transcript.get("entries") or []
            text = transcript.get("text") or ""
            faqs = extract_faqs(entries, text)
            if transcript.get("text_path"):
                faq_path = Path(transcript["text_path"]).with_name(
                    Path(transcript["text_path"]).stem + "_faqs.json"
                )
                faq_path.write_text(json.dumps(faqs, indent=2), encoding="utf-8")
                faqs["path"] = str(faq_path)
            session["faqs"] = faqs
            SESSIONS[bot_id] = session
        self.send_json(session_payload(bot_id))

    def handle_calendar_status(self) -> None:
        try:
            load_dotenv(ROOT / ".env")
            self.send_json(calendar_sync.calendar_status(ROOT))
        except RecallError as exc:
            self.send_json({"error": str(exc)}, exc.status if exc.status and exc.status >= 400 else 502)

    def handle_calendar_connect(self, provider: str) -> None:
        try:
            load_dotenv(ROOT / ".env")
            token = calendar_sync.ensure_token(ROOT)
            base = public_base(self)
            success = f"{base}/calendar/connected"
            error = f"{base}/calendar/error"
            if provider == "google":
                url = calendar_sync.build_google_oauth_url(token, success, error)
            else:
                url = calendar_sync.build_outlook_oauth_url(token, success, error)
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()
        except RecallError as exc:
            self.send_json({"error": str(exc)}, 400)

    def handle_calendar_enable_all(self) -> None:
        try:
            load_dotenv(ROOT / ".env")
            body = {}
            try:
                body = self.read_json()
            except RecallError:
                body = {}
            bot_name = str(body.get("bot_name") or "Transcription Bot").strip() or "Transcription Bot"
            token = calendar_sync.ensure_token(ROOT)
            user = calendar_sync.set_record_all(token, bot_name=bot_name)
            try:
                calendar_sync.refresh_meetings(token)
            except RecallError:
                pass
            meetings = [
                calendar_sync.summarize_meeting(m)
                for m in calendar_sync.list_meetings(token)
            ]
            meetings.sort(key=lambda m: m.get("start_time") or "")
            self.send_json(
                {
                    "ok": True,
                    "connections": calendar_sync.connected_platforms(user),
                    "preferences": user.get("preferences"),
                    "meetings": meetings,
                    "hint": "Auto-join is on for all upcoming meetings with a Meet/Zoom/Teams link.",
                }
            )
        except RecallError as exc:
            self.send_json({"error": str(exc)}, exc.status if exc.status and exc.status >= 400 else 502)

    def handle_calendar_refresh(self) -> None:
        try:
            load_dotenv(ROOT / ".env")
            token = calendar_sync.ensure_token(ROOT)
            try:
                calendar_sync.refresh_meetings(token)
            except RecallError:
                pass
            meetings = [
                calendar_sync.summarize_meeting(m)
                for m in calendar_sync.list_meetings(token)
            ]
            meetings.sort(key=lambda m: m.get("start_time") or "")
            self.send_json({"meetings": meetings})
        except RecallError as exc:
            self.send_json({"error": str(exc)}, exc.status if exc.status and exc.status >= 400 else 502)

    def handle_calendar_meetings(self) -> None:
        try:
            load_dotenv(ROOT / ".env")
            token = calendar_sync.ensure_token(ROOT)
            meetings = [
                calendar_sync.summarize_meeting(m)
                for m in calendar_sync.list_meetings(token)
            ]
            meetings.sort(key=lambda m: m.get("start_time") or "")
            self.send_json({"meetings": meetings})
        except RecallError as exc:
            self.send_json({"error": str(exc)}, exc.status if exc.status and exc.status >= 400 else 502)

    def handle_calendar_disconnect(self) -> None:
        try:
            load_dotenv(ROOT / ".env")
            body = self.read_json()
            platform = str(body.get("platform") or "").strip()
            if not platform:
                raise RecallError("Missing platform.")
            token = calendar_sync.ensure_token(ROOT)
            calendar_sync.disconnect_platform(token, platform)
            self.send_json({"ok": True})
        except RecallError as exc:
            self.send_json({"error": str(exc)}, exc.status if exc.status and exc.status >= 400 else 502)

    def handle_meeting_record(self, meeting_id: str) -> None:
        try:
            load_dotenv(ROOT / ".env")
            body = self.read_json()
            should_record = bool(body.get("should_record", True))
            token = calendar_sync.ensure_token(ROOT)
            meeting = calendar_sync.update_meeting_record(token, meeting_id, should_record)
            self.send_json(calendar_sync.summarize_meeting(meeting))
        except RecallError as exc:
            self.send_json({"error": str(exc)}, exc.status if exc.status and exc.status >= 400 else 502)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Serve the meeting-bot HTML UI.")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Bind address (use 0.0.0.0 in Docker / cloud).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8765")),
        help="HTTP port (default: 8765).",
    )
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
