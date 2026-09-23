"""Email capability: Guppy's own mailbox at guppy@johnnycode.ai, via the Admiral's Remail.

The key lives only in the macOS Keychain (service "guppy-remail", account "guppy") and is read at
call time. It is never logged, never returned, and never written to disk.

Plain functions hold the logic (tested in test_email.py); the MCP tools are thin wrappers.
"""
from __future__ import annotations

import html as html_module
import os
import re
import subprocess
import sys
from typing import Any, Callable

import httpx
from mcp.server.mcpserver import MCPServer

server = MCPServer("email")

MAILBOX = "guppy@johnnycode.ai"
SENDER = f"Guppy <{MAILBOX}>"
KEYCHAIN_SERVICE = "guppy-remail"
KEYCHAIN_ACCOUNT = "guppy"
BODY_LIMIT = 20_000  # characters of one email body handed to the Mind
TIMEOUT = 30.0

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ADDRESS_RE = re.compile(r"^[^\s<>,@]+@[^\s<>,@]+\.[^\s<>,@]+$")


def base_url() -> str:
    """Where Remail lives. Overridable for a staging install; defaults to the Admiral's."""
    return os.environ.get("REMAIL_BASE_URL", "https://remail.foo").rstrip("/")


def api_key(runner: Callable[..., Any] = subprocess.run) -> str:
    """Guppy's Remail key, straight out of the Keychain. Never logged or returned to the Mind."""
    try:
        done = runner(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Could not reach the macOS Keychain for Guppy's Remail key: {e}") from None
    if done.returncode != 0 or not (done.stdout or "").strip():
        raise RuntimeError(
            "Guppy's Remail key is not in the Keychain "
            f"(service {KEYCHAIN_SERVICE!r}, account {KEYCHAIN_ACCOUNT!r}). Email is unavailable until it is."
        )
    return done.stdout.strip()


def _open_client(**kwargs) -> httpx.Client:
    return httpx.Client(**kwargs)


def api(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    key_reader: Callable[[], str] | None = None,
    client_factory: Callable[..., httpx.Client] | None = None,
) -> dict:
    """One authenticated Remail REST call. Raises RuntimeError with a spoken-language message."""
    key = (key_reader or api_key)()
    client_factory = client_factory or _open_client
    url = f"{base_url()}{path}"
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        with client_factory(timeout=TIMEOUT) as client:
            response = client.request(method, url, params=params, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Remail at {base_url()}: {e}") from None
    if response.status_code >= 400:
        raise RuntimeError(f"Remail refused the request ({response.status_code}): {describe_error(response)}")
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(f"Remail returned something that was not JSON ({response.status_code}).") from None


def describe_error(response: httpx.Response) -> str:
    """Remail's own message plus its next_actions, which say how to fix it."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or "no detail"
    if not isinstance(payload, dict):
        return str(payload)[:300]
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else (error if isinstance(error, str) else None)
    message = message or payload.get("message") or "no detail"
    actions = payload.get("next_actions") or (error.get("next_actions") if isinstance(error, dict) else None)
    if isinstance(actions, list) and actions:
        rendered = "; ".join(
            a.get("description") or a.get("action") or str(a) if isinstance(a, dict) else str(a) for a in actions[:3]
        )
        return f"{message} (next: {rendered})"
    return str(message)


def strip_html(markup: str) -> str:
    """A readable plain-text approximation of an HTML body, for mail that carries no text part."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def clip(text: str, limit: int = BODY_LIMIT) -> str:
    """Keep a long body from swamping the Mind, and say so when it is cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[... truncated, {len(text) - limit} more characters]"


def summarize(message: dict) -> dict:
    """The one-line shape of an inbound message that Guppy reads out."""
    return {
        "id": message.get("id", ""),
        "from": message.get("from", ""),
        "subject": (message.get("subject") or "(no subject)").strip(),
        "received_at": message.get("received_at", ""),
        "to": message.get("to", []),
        "is_junk": bool(message.get("is_junk", False)),
    }


def fetch_inbox(limit: int = 10, *, api_call: Callable[..., dict] | None = None) -> dict:
    """Newest inbound mail addressed to Guppy's own address, newest first."""
    api_call = api_call or api
    limit = max(1, min(int(limit), 50))
    payload = api_call("GET", "/v1/inbound", params={"limit": limit, "to": MAILBOX.lower()})
    messages = [summarize(m) for m in payload.get("data", []) or []]
    return {"mailbox": MAILBOX, "count": len(messages), "messages": messages}


def fetch_email(email_id: str, *, api_call: Callable[..., dict] | None = None) -> dict:
    """One inbound message in full: who sent it, when, and what it says."""
    api_call = api_call or api
    email_id = (email_id or "").strip()
    if not UUID_RE.match(email_id):
        raise ValueError(f"{email_id!r} is not a Remail message id. Use an id from list_inbox.")
    message = api_call("GET", f"/v1/inbound/{email_id}")
    text = (message.get("text") or "").strip() or strip_html(message.get("html") or "")
    headers = message.get("headers") or {}
    return {
        **summarize(message),
        "cc": message.get("cc", []),
        "message_id": message.get("message_id") or headers.get("message-id") or headers.get("Message-ID"),
        "reply_to": headers.get("reply-to") or headers.get("Reply-To"),
        "spam_score": message.get("spam_score"),
        "auth_results": message.get("auth_results", {}),
        "attachments": [
            {"filename": a.get("filename", ""), "content_type": a.get("content_type", ""),
             "size_bytes": a.get("size_bytes", 0)}
            for a in message.get("attachments", []) or []
        ],
        "text": clip(text) or "(this message has no readable body)",
    }


def thread_headers(in_reply_to_id: str | None, *, api_call: Callable[..., dict] | None = None) -> dict:
    """Turn a list_inbox id (or a raw Message-ID) into the headers that keep a reply in its thread."""
    api_call = api_call or api
    value = (in_reply_to_id or "").strip()
    if not value:
        return {}
    if UUID_RE.match(value):
        original = api_call("GET", f"/v1/inbound/{value}")
        headers = original.get("headers") or {}
        message_id = original.get("message_id") or headers.get("message-id") or headers.get("Message-ID")
        if not message_id:
            return {}
        references = [r for r in (headers.get("references") or headers.get("References") or "").split() if r]
        threaded: dict = {"in_reply_to": message_id}
        chain = references + [message_id]
        if chain:
            threaded["references"] = chain[-100:]
        return threaded
    return {"in_reply_to": value}


def build_send_payload(to: str, subject: str, text: str, in_reply_to_id: str | None = None,
                       *, api_call: Callable[..., dict] | None = None) -> dict:
    """Validate what Guppy is about to send, and shape it for POST /v1/emails."""
    api_call = api_call or api
    recipients = [r.strip() for r in re.split(r"[,;]", to or "") if r.strip()]
    if not recipients:
        raise ValueError("No recipient given.")
    bad = [r for r in recipients if not ADDRESS_RE.match(r.split("<")[-1].rstrip(">").strip())]
    if bad:
        raise ValueError(f"Not a usable email address: {', '.join(bad)}")
    if not (text or "").strip():
        raise ValueError("Refusing to send an empty email.")
    payload: dict = {
        "from": SENDER,
        "to": recipients,
        "subject": (subject or "").strip(),
        "text": text,
    }
    payload.update(thread_headers(in_reply_to_id, api_call=api_call))
    return payload


def deliver(to: str, subject: str, text: str, in_reply_to_id: str | None = None,
            *, api_call: Callable[..., dict] | None = None) -> dict:
    """Send one email as Guppy."""
    api_call = api_call or api
    payload = build_send_payload(to, subject, text, in_reply_to_id, api_call=api_call)
    sent = api_call("POST", "/v1/emails", body=payload)
    return {
        "id": sent.get("id", ""),
        "status": sent.get("status", ""),
        "from": sent.get("from", SENDER),
        "to": sent.get("to", payload["to"]),
        "subject": sent.get("subject", payload["subject"]),
        "threaded": "in_reply_to" in payload,
    }


@server.tool()
def list_inbox(limit: int = 10) -> dict:
    """The newest emails sent to Guppy's own address, guppy@johnnycode.ai."""
    try:
        return fetch_inbox(limit)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def read_email(email_id: str) -> dict:
    """The full text of one inbound email, by the id that list_inbox gives."""
    try:
        return fetch_email(email_id)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def send_email(to: str, subject: str, text: str, in_reply_to_id: str = "") -> dict:
    """Send an email as Guppy <guppy@johnnycode.ai>. Pass a list_inbox id as in_reply_to_id to reply in thread."""
    try:
        return deliver(to, subject, text, in_reply_to_id or None)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


if __name__ == "__main__":
    print(f"email capability ready as {SENDER}", file=sys.stderr)
    server.run()
