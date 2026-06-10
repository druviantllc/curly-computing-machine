#!/usr/bin/env python3
"""Build and deliver a daily morning brief.

The script is designed to run from GitHub Actions on a schedule. It reads from
Google Calendar, Gmail, Google Drive, GitHub, and Airtable when the relevant
secrets are configured, then sends the brief through Gmail.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.message
import json
import os
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GITHUB_API_URL = "https://api.github.com"
AIRTABLE_API_URL = "https://api.airtable.com/v0"


@dataclass(frozen=True)
class Config:
    timezone: str
    recipient_email: str
    sender_email: str
    calendar_ids: list[str]
    gmail_query: str
    max_email_count: int
    drive_query: str
    max_drive_count: int
    github_owner: str
    github_repo: str
    github_username: str
    airtable_base_id: str
    airtable_table_name: str
    airtable_view_name: str
    airtable_due_field: str
    airtable_status_field: str
    airtable_done_statuses: list[str]


class ApiError(RuntimeError):
    """Raised when an upstream API call fails."""


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def load_config() -> Config:
    recipient = env("BRIEF_RECIPIENT_EMAIL") or env("GMAIL_SENDER_EMAIL")
    calendar_ids = [value.strip() for value in env("GOOGLE_CALENDAR_IDS", "primary").split(",") if value.strip()]
    done_statuses = [value.strip() for value in env("AIRTABLE_DONE_STATUSES", "Done,Complete,Completed").split(",") if value.strip()]

    return Config(
        timezone=env("BRIEF_TIMEZONE", "America/New_York"),
        recipient_email=recipient,
        sender_email=env("GMAIL_SENDER_EMAIL"),
        calendar_ids=calendar_ids,
        gmail_query=env("GMAIL_BRIEF_QUERY", "in:inbox is:unread (is:important OR category:primary) newer_than:14d"),
        max_email_count=int(env("GMAIL_BRIEF_MAX", "10")),
        drive_query=env("GOOGLE_DRIVE_BRIEF_QUERY", "starred = true and trashed = false"),
        max_drive_count=int(env("GOOGLE_DRIVE_BRIEF_MAX", "8")),
        github_owner=env("GITHUB_OWNER") or env("GITHUB_REPOSITORY", "/").split("/", 1)[0],
        github_repo=env("GITHUB_REPO") or env("GITHUB_REPOSITORY", "/").split("/", 1)[-1],
        github_username=env("GITHUB_USERNAME"),
        airtable_base_id=env("AIRTABLE_BASE_ID"),
        airtable_table_name=env("AIRTABLE_TABLE_NAME"),
        airtable_view_name=env("AIRTABLE_VIEW_NAME"),
        airtable_due_field=env("AIRTABLE_DUE_FIELD", "Due Date"),
        airtable_status_field=env("AIRTABLE_STATUS_FIELD", "Status"),
        airtable_done_statuses=done_statuses,
    )


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, str | int] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    body = None
    merged_headers = {"Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        merged_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=merged_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ApiError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise ApiError(f"{method} {url} failed: {error.reason}") from error


def google_access_token() -> str:
    client_id = env("GOOGLE_CLIENT_ID")
    client_secret = env("GOOGLE_CLIENT_SECRET")
    refresh_token = env("GOOGLE_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        return ""

    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.loads(response.read().decode("utf-8"))
            return token["access_token"]
    except (KeyError, urllib.error.HTTPError, urllib.error.URLError) as error:
        raise ApiError(f"Could not refresh Google access token: {error}") from error


def today_window(timezone: str) -> tuple[dt.datetime, dt.datetime, str]:
    zone = ZoneInfo(timezone)
    now = dt.datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return start, end, now.strftime("%A, %B %-d, %Y")


def iso_zulu(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def google_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def fetch_calendar_events(config: Config, access_token: str, start: dt.datetime, end: dt.datetime) -> list[str]:
    if not access_token:
        return ["Google Calendar skipped: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, or GOOGLE_REFRESH_TOKEN is not configured."]

    events: list[dict[str, Any]] = []
    for calendar_id in config.calendar_ids:
        encoded_calendar_id = urllib.parse.quote(calendar_id, safe="")
        payload = request_json(
            GOOGLE_CALENDAR_EVENTS_URL.format(calendar_id=encoded_calendar_id),
            headers=google_headers(access_token),
            params={
                "timeMin": iso_zulu(start),
                "timeMax": iso_zulu(end),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 20,
            },
        )
        for event in payload.get("items", []):
            event["_calendar_id"] = calendar_id
            events.append(event)

    if not events:
        return ["No calendar events scheduled today."]

    zone = ZoneInfo(config.timezone)
    lines: list[str] = []
    for event in sorted(events, key=lambda item: item.get("start", {}).get("dateTime", item.get("start", {}).get("date", ""))):
        start_value = event.get("start", {})
        if "dateTime" in start_value:
            when = dt.datetime.fromisoformat(start_value["dateTime"].replace("Z", "+00:00")).astimezone(zone).strftime("%-I:%M %p")
        else:
            when = "All day"
        summary = event.get("summary", "Untitled event")
        location = event.get("location")
        suffix = f" — {location}" if location else ""
        lines.append(f"{when}: {summary}{suffix}")
    return lines


def fetch_gmail_messages(config: Config, access_token: str) -> list[str]:
    if not access_token:
        return ["Gmail skipped: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, or GOOGLE_REFRESH_TOKEN is not configured."]

    search = request_json(
        GMAIL_MESSAGES_URL,
        headers=google_headers(access_token),
        params={"q": config.gmail_query, "maxResults": config.max_email_count},
    )
    messages = search.get("messages", [])
    if not messages:
        return ["No important unread emails found."]

    lines: list[str] = []
    for message in messages[: config.max_email_count]:
        detail = request_json(
            f"{GMAIL_MESSAGES_URL}/{message['id']}",
            headers=google_headers(access_token),
            params={"format": "metadata"},
        )
        headers = {header["name"].lower(): header["value"] for header in detail.get("payload", {}).get("headers", [])}
        sender = headers.get("from", "Unknown sender")
        subject = headers.get("subject", "No subject")
        snippet = detail.get("snippet", "")
        lines.append(f"{subject} — {sender}. {snippet}")
    return lines


def fetch_drive_items(config: Config, access_token: str) -> list[str]:
    if not access_token:
        return ["Google Drive skipped: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, or GOOGLE_REFRESH_TOKEN is not configured."]

    payload = request_json(
        GOOGLE_DRIVE_FILES_URL,
        headers=google_headers(access_token),
        params={
            "q": config.drive_query,
            "pageSize": config.max_drive_count,
            "orderBy": "modifiedTime desc",
            "fields": "files(name,webViewLink,modifiedTime,owners(displayName))",
        },
    )
    files = payload.get("files", [])
    if not files:
        return ["No starred Drive files needing attention."]

    lines: list[str] = []
    for item in files:
        modified = item.get("modifiedTime", "")[:10]
        owner = item.get("owners", [{}])[0].get("displayName", "Unknown owner")
        link = item.get("webViewLink", "")
        lines.append(f"{item.get('name', 'Untitled file')} — modified {modified} by {owner}. {link}")
    return lines


def github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = env("GH_TOKEN") or env("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_github_attention(config: Config) -> list[str]:
    if not env("GH_TOKEN") and not env("GITHUB_TOKEN"):
        return ["GitHub skipped: GH_TOKEN or GITHUB_TOKEN is not configured."]

    queries = []
    if config.github_username:
        queries.append(f"repo:{config.github_owner}/{config.github_repo} is:open assignee:{config.github_username}")
        queries.append(f"repo:{config.github_owner}/{config.github_repo} is:open review-requested:{config.github_username}")
    else:
        queries.append(f"repo:{config.github_owner}/{config.github_repo} is:open involves:@me")

    lines: list[str] = []
    for query in queries:
        payload = request_json(
            f"{GITHUB_API_URL}/search/issues",
            headers=github_headers(),
            params={"q": query, "sort": "updated", "order": "desc", "per_page": 5},
        )
        for item in payload.get("items", []):
            kind = "PR" if "pull_request" in item else "Issue"
            lines.append(f"{kind} #{item['number']}: {item['title']} — {item['html_url']}")

    return lines or ["No open GitHub items found for your attention."]


def airtable_formula(config: Config, today: dt.date) -> str:
    safe_due = config.airtable_due_field.replace("'", "\\'")
    safe_status = config.airtable_status_field.replace("'", "\\'")
    done_checks = ", ".join(f"{{{safe_status}}}='{status.replace(chr(39), chr(92) + chr(39))}'" for status in config.airtable_done_statuses)
    return f"AND(IS_SAME({{{safe_due}}}, '{today.isoformat()}', 'day'), NOT(OR({done_checks})))"


def fetch_airtable_due(config: Config, today: dt.date) -> list[str]:
    token = env("AIRTABLE_TOKEN")
    if not all([token, config.airtable_base_id, config.airtable_table_name]):
        return ["Airtable skipped: AIRTABLE_TOKEN, AIRTABLE_BASE_ID, or AIRTABLE_TABLE_NAME is not configured."]

    params: dict[str, str | int] = {
        "pageSize": 10,
        "filterByFormula": airtable_formula(config, today),
    }
    if config.airtable_view_name:
        params["view"] = config.airtable_view_name

    payload = request_json(
        f"{AIRTABLE_API_URL}/{urllib.parse.quote(config.airtable_base_id)}/{urllib.parse.quote(config.airtable_table_name)}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    records = payload.get("records", [])
    if not records:
        return ["No Airtable records due today."]

    lines: list[str] = []
    for record in records:
        fields = record.get("fields", {})
        name = fields.get("Name") or fields.get("Task") or fields.get("Title") or record.get("id")
        status = fields.get(config.airtable_status_field, "No status")
        lines.append(f"{name} — {status}")
    return lines


def section(title: str, lines: list[str]) -> str:
    formatted = "\n".join(f"- {line}" for line in lines)
    return f"## {title}\n{formatted}"


def safe_section(title: str, fetch: Any) -> str:
    try:
        return section(title, fetch())
    except ApiError as error:
        return section(title, [f"{title} check failed: {error}"])


def build_brief(config: Config) -> tuple[str, str]:
    start, end, friendly_date = today_window(config.timezone)
    access_token = google_access_token()

    subject = f"Morning brief for {friendly_date}"
    body = "\n\n".join(
        [
            f"# {subject}",
            safe_section("Calendar", lambda: fetch_calendar_events(config, access_token, start, end)),
            safe_section("Important unread email", lambda: fetch_gmail_messages(config, access_token)),
            safe_section("Google Drive", lambda: fetch_drive_items(config, access_token)),
            safe_section("GitHub", lambda: fetch_github_attention(config)),
            safe_section("Airtable", lambda: fetch_airtable_due(config, start.date())),
        ]
    )
    return subject, body


def gmail_profile_email(access_token: str) -> str:
    if not access_token:
        return ""
    profile = request_json(GMAIL_PROFILE_URL, headers=google_headers(access_token))
    return profile.get("emailAddress", "")


def send_email(config: Config, subject: str, markdown_body: str) -> None:
    access_token = google_access_token()
    if not access_token:
        print(markdown_body)
        print("\nEmail delivery skipped because Google OAuth secrets are not configured.")
        return

    recipient = config.recipient_email or gmail_profile_email(access_token)
    sender = config.sender_email or recipient
    if not recipient:
        raise ApiError("BRIEF_RECIPIENT_EMAIL or GMAIL_SENDER_EMAIL must be configured to send the brief.")

    message = email.message.EmailMessage()
    message["To"] = recipient
    message["From"] = sender
    message["Subject"] = subject
    message.set_content(markdown_body)

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    request_json(GMAIL_SEND_URL, method="POST", headers=google_headers(access_token), data={"raw": encoded})


def append_github_summary(markdown_body: str) -> None:
    summary_path = env("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(markdown_body)
            summary.write("\n")


def main() -> int:
    config = load_config()
    try:
        subject, body = build_brief(config)
        append_github_summary(body)
        send_email(config, subject, body)
        print(f"Delivered morning brief: {subject}")
        return 0
    except ApiError as error:
        print(textwrap.fill(str(error), width=100), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
