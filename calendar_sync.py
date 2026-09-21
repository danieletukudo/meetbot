"""Recall.ai Calendar V1 helpers — connect Google/Outlook and auto-join meetings."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from meeting_bot import RecallError, api_request, require_api_key

GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly "
    "https://www.googleapis.com/auth/userinfo.email"
)
OUTLOOK_SCOPES = (
    "offline_access openid email https://graph.microsoft.com/Calendars.Read"
)

# Record every meeting that has a valid conference link (internal + external).
RECORD_ALL_PREFERENCES = {
    "record_non_host": False,
    "record_recurring": False,
    "record_external": True,
    "record_internal": True,
    "record_confirmed": False,
    "record_only_host": False,
}


def region() -> str:
    return os.environ.get("RECALL_REGION", "us-east-1").strip()


def base_url() -> str:
    return f"https://{region()}.recall.ai/api/v1"


def google_redirect_uri() -> str:
    return f"https://{region()}.recall.ai/api/v1/calendar/google_oauth_callback/"


def outlook_redirect_uri() -> str:
    return f"https://{region()}.recall.ai/api/v1/calendar/ms_oauth_callback/"


def calendar_user_id() -> str:
    return os.environ.get("CALENDAR_USER_ID", "local-user").strip() or "local-user"


def state_path(root: Path) -> Path:
    return root / ".calendar_state.json"


def load_state(root: Path) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(root: Path, data: dict[str, Any]) -> None:
    path = state_path(root)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def create_auth_token() -> str:
    result = api_request(
        "POST",
        f"{base_url()}/calendar/authenticate/",
        require_api_key(),
        {"user_id": calendar_user_id()},
    )
    token = result.get("token")
    if not token:
        raise RecallError("Recall did not return a calendar auth token.")
    return str(token)


def calendar_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    url = f"{base_url()}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return api_request(
        method,
        url,
        api_key=None,
        payload=payload,
        extra_headers={"x-recallcalendarauthtoken": token},
    )


def ensure_token(root: Path) -> str:
    state = load_state(root)
    token = state.get("token")
    expires_at = state.get("expires_at")
    now = datetime.now(timezone.utc)
    if token and expires_at:
        try:
            exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if exp - now > timedelta(minutes=30):
                return str(token)
        except ValueError:
            pass
    token = create_auth_token()
    state["token"] = token
    state["user_id"] = calendar_user_id()
    state["expires_at"] = (now + timedelta(hours=23)).isoformat()
    save_state(root, state)
    return token


def get_user(token: str) -> dict[str, Any]:
    return calendar_request("GET", "/calendar/user/", token)


def set_record_all(token: str, bot_name: str = "Transcription Bot") -> dict[str, Any]:
    prefs = dict(RECORD_ALL_PREFERENCES)
    prefs["bot_name"] = bot_name
    return calendar_request(
        "PUT",
        "/calendar/user/",
        token,
        {"preferences": prefs},
    )


def refresh_meetings(token: str) -> Any:
    return calendar_request("POST", "/calendar/meetings/refresh/", token)


def list_meetings(token: str, days_ahead: int = 14) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    result = calendar_request(
        "GET",
        "/calendar/meetings/",
        token,
        query={
            "start_time_after": now.isoformat().replace("+00:00", "Z"),
            "start_time_before": end.isoformat().replace("+00:00", "Z"),
        },
    )
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        return result["results"]
    return []


def disconnect_platform(token: str, platform: str) -> Any:
    return calendar_request(
        "POST",
        "/calendar/user/disconnect/",
        token,
        {"platform": platform},
    )


def update_meeting_record(token: str, meeting_id: str, should_record: bool) -> Any:
    return calendar_request(
        "PUT",
        f"/calendar/meetings/{meeting_id}/",
        token,
        {"override_should_record": should_record},
    )


def meeting_url_from_event(meeting: dict[str, Any]) -> str | None:
    platform = (meeting.get("meeting_platform") or "").lower()
    zoom = meeting.get("zoom_invite") or {}
    meet = meeting.get("meet_invite") or {}
    teams = meeting.get("teams_invite") or {}
    if platform in {"zoom", "zoom_meeting"} and zoom.get("meeting_id"):
        pwd = zoom.get("meeting_password")
        url = f"https://zoom.us/j/{zoom['meeting_id']}"
        return f"{url}?pwd={pwd}" if pwd else url
    if platform in {"google_meet", "meet"} and meet.get("meeting_id"):
        return f"https://meet.google.com/{meet['meeting_id']}"
    if platform in {"microsoft_teams", "teams"}:
        # Teams invite fields vary; surface whatever id we have for display.
        for key in ("business_meeting_id", "meeting_id", "thread_id"):
            if teams.get(key):
                return f"teams:{teams[key]}"
    return None


def summarize_meeting(meeting: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": meeting.get("id"),
        "title": meeting.get("title") or "Untitled meeting",
        "start_time": meeting.get("start_time"),
        "end_time": meeting.get("end_time"),
        "meeting_platform": meeting.get("meeting_platform"),
        "calendar_platform": meeting.get("calendar_platform"),
        "will_record": bool(meeting.get("will_record")),
        "will_record_reason": meeting.get("will_record_reason"),
        "bot_id": meeting.get("bot_id"),
        "meeting_url": meeting_url_from_event(meeting),
        "organizer_email": meeting.get("organizer_email"),
        "is_recurring": bool(meeting.get("is_recurring")),
    }


def build_google_oauth_url(token: str, success_url: str, error_url: str) -> str:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    if not client_id:
        raise RecallError(
            "Missing GOOGLE_OAUTH_CLIENT_ID. Create a Google OAuth client, save it in the "
            "Recall dashboard, and add the client id to .env."
        )
    redirect = google_redirect_uri()
    state = json.dumps(
        {
            "recall_calendar_auth_token": token,
            "google_oauth_redirect_url": redirect,
            "success_url": success_url,
            "error_url": error_url,
        }
    )
    params = {
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "response_type": "code",
        "state": state,
        "redirect_uri": redirect,
        "client_id": client_id,
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def build_outlook_oauth_url(token: str, success_url: str, error_url: str) -> str:
    client_id = os.environ.get("MICROSOFT_OAUTH_CLIENT_ID", "").strip()
    if not client_id:
        raise RecallError(
            "Missing MICROSOFT_OAUTH_CLIENT_ID. Create an Azure app, save it in the "
            "Recall dashboard, and add the client id to .env."
        )
    redirect = outlook_redirect_uri()
    state = json.dumps(
        {
            "recall_calendar_auth_token": token,
            "ms_oauth_redirect_url": redirect,
            "success_url": success_url,
            "error_url": error_url,
        }
    )
    params = {
        "scope": OUTLOOK_SCOPES,
        "response_mode": "query",
        "response_type": "code",
        "state": state,
        "redirect_uri": redirect,
        "client_id": client_id,
    }
    return (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"
        + urlencode(params)
    )


def connected_platforms(user: dict[str, Any]) -> list[dict[str, Any]]:
    connections = []
    for item in user.get("connections") or []:
        if item.get("connected"):
            connections.append(
                {
                    "platform": item.get("platform"),
                    "email": item.get("email"),
                    "id": item.get("id"),
                }
            )
    return connections


def calendar_status(root: Path) -> dict[str, Any]:
    token = ensure_token(root)
    user = get_user(token)
    connections = connected_platforms(user)
    prefs = user.get("preferences") or {}
    return {
        "connected": bool(connections),
        "connections": connections,
        "preferences": {
            "record_external": prefs.get("record_external"),
            "record_internal": prefs.get("record_internal"),
            "bot_name": prefs.get("bot_name"),
        },
        "google_configured": bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID")),
        "outlook_configured": bool(os.environ.get("MICROSOFT_OAUTH_CLIENT_ID")),
        "region": region(),
        "google_redirect_uri": google_redirect_uri(),
        "outlook_redirect_uri": outlook_redirect_uri(),
    }
