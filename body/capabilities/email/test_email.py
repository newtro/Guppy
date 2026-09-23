"""Tests for the email capability.

Everything is mocked: the Keychain lookup is a fake subprocess runner and the HTTP layer is an
httpx.MockTransport. No test sends real email or opens a socket.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

HERE = Path(__file__).resolve().parent


def _load():
    """Import this capability's server.py under a unique name (every capability has a server.py)."""
    spec = importlib.util.spec_from_file_location("guppy_capability_email", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


email_server = _load()


# --- fakes -------------------------------------------------------------------------------------

def fake_keychain(value: str = "rk_test_key", returncode: int = 0, raises: Exception | None = None):
    """Stand in for `security find-generic-password`."""
    def runner(*args, **kwargs):
        if raises:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=value, stderr="")
    return runner


def mock_api(routes: dict, seen: list | None = None):
    """A drop-in for server.api: looks up (METHOD, path) and records every call."""
    def call(method, path, *, params=None, body=None, **_):
        if seen is not None:
            seen.append({"method": method, "path": path, "params": params, "body": body})
        try:
            return routes[(method, path)]
        except KeyError:  # pragma: no cover - a test asked for a route it did not define
            raise AssertionError(f"unexpected call {method} {path}")
    return call


def client_factory_for(handler):
    """An httpx.Client whose transport is a mock, so header/URL building is really exercised."""
    def factory(**kwargs):
        kwargs.pop("timeout", None)
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    return factory


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Belt and braces: a test that forgets to mock the transport fails instead of emailing anyone."""
    def refuse(**kwargs):
        raise AssertionError("a test tried to open a real connection to Remail")

    monkeypatch.setattr(email_server, "_open_client", refuse)


INBOUND_SUMMARY = {
    "id": "11111111-2222-3333-4444-555555555555",
    "from": "Ada <ada@example.com>",
    "to": ["guppy@johnnycode.ai"],
    "subject": "  Status of the refit  ",
    "received_at": "2026-09-22T16:13:18.541Z",
    "is_junk": False,
}

INBOUND_FULL = {
    **INBOUND_SUMMARY,
    "cc": ["scott@johnnycode.ai"],
    "message_id": "<abc@example.com>",
    "text": "The refit is done.\n",
    "html": "<p>ignored when text exists</p>",
    "spam_score": 0.1,
    "auth_results": {"spf": "pass", "dkim": "pass", "dmarc": "pass"},
    "headers": {"references": "<older@example.com>", "reply-to": "ada+replies@example.com"},
    "attachments": [{"id": "att1", "filename": "refit.pdf", "content_type": "application/pdf",
                     "size_bytes": 4096, "content_id": None}],
}


# --- manifest ----------------------------------------------------------------------------------

def test_manifest_declares_every_exposed_tool_with_an_effect():
    manifest = json.loads((HERE / "capability.json").read_text())
    assert manifest["name"] == "email"
    assert manifest["enabled"] is True
    exposed = {t.name for t in asyncio.run(email_server.server.list_tools())}
    assert exposed == {"list_inbox", "read_email", "send_email"}
    assert set(manifest["effects"]) == exposed
    assert manifest["effects"]["list_inbox"] == "read"
    assert manifest["effects"]["read_email"] == "read"
    assert manifest["effects"]["send_email"] == "act"


def test_manifest_taints_both_readers_of_other_peoples_words():
    manifest = json.loads((HERE / "capability.json").read_text())
    assert set(manifest["taints"]) == {"list_inbox", "read_email"}


def test_every_tool_has_a_description_for_the_mind():
    for tool in asyncio.run(email_server.server.list_tools()):
        assert (tool.description or "").strip()


def test_identity_is_guppy():
    assert email_server.MAILBOX == "guppy@johnnycode.ai"
    assert email_server.SENDER == "Guppy <guppy@johnnycode.ai>"


# --- the key -----------------------------------------------------------------------------------

def test_api_key_comes_from_the_keychain():
    assert email_server.api_key(runner=fake_keychain("rk_live_abc\n")) == "rk_live_abc"


def test_api_key_asks_the_keychain_for_the_right_item():
    asked = {}

    def runner(args, **kwargs):
        asked["args"] = args
        return SimpleNamespace(returncode=0, stdout="rk_x", stderr="")

    email_server.api_key(runner=runner)
    assert asked["args"] == ["security", "find-generic-password", "-s", "guppy-remail", "-a", "guppy", "-w"]


def test_missing_key_is_a_plain_message_not_a_crash():
    with pytest.raises(RuntimeError, match="not in the Keychain"):
        email_server.api_key(runner=fake_keychain("", returncode=44))
    with pytest.raises(RuntimeError, match="not in the Keychain"):
        email_server.api_key(runner=fake_keychain("   "))


def test_keychain_failure_is_reported_not_swallowed():
    with pytest.raises(RuntimeError, match="Could not reach the macOS Keychain"):
        email_server.api_key(runner=fake_keychain(raises=OSError("no security binary")))


def test_tools_report_a_missing_key_instead_of_raising(monkeypatch):
    monkeypatch.setattr(email_server, "api_key", lambda **_: (_ for _ in ()).throw(RuntimeError("no key here")))
    assert "no key here" in email_server.list_inbox()["error"]
    assert "no key here" in email_server.read_email(INBOUND_SUMMARY["id"])["error"]
    assert "no key here" in email_server.send_email("a@b.com", "hi", "body")["error"]


# --- the HTTP layer ----------------------------------------------------------------------------

def test_api_sends_the_bearer_token_and_never_returns_it():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": []})

    result = email_server.api("GET", "/v1/inbound", params={"limit": 3},
                              key_reader=lambda: "rk_secret",
                              client_factory=client_factory_for(handler))
    assert captured["auth"] == "Bearer rk_secret"
    assert captured["url"] == "https://remail.foo/v1/inbound?limit=3"
    assert "rk_secret" not in json.dumps(result)


def test_api_honours_an_overridden_base_url(monkeypatch):
    monkeypatch.setenv("REMAIL_BASE_URL", "https://staging.remail.foo/")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    email_server.api("GET", "/v1/inbound", key_reader=lambda: "rk", client_factory=client_factory_for(handler))
    assert seen["url"] == "https://staging.remail.foo/v1/inbound"


def test_api_posts_the_body_as_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "e1", "status": "queued"})

    email_server.api("POST", "/v1/emails", body={"from": "x", "to": ["y@z.com"]},
                     key_reader=lambda: "rk", client_factory=client_factory_for(handler))
    assert seen["body"]["to"] == ["y@z.com"]


def test_api_turns_an_error_response_into_a_spoken_message():
    def handler(request):
        return httpx.Response(422, json={"error": {"message": "domain not verified"},
                                         "next_actions": [{"description": "verify johnnycode.ai"}]})

    with pytest.raises(RuntimeError) as err:
        email_server.api("POST", "/v1/emails", key_reader=lambda: "rk", client_factory=client_factory_for(handler))
    assert "422" in str(err.value)
    assert "domain not verified" in str(err.value)
    assert "verify johnnycode.ai" in str(err.value)


def test_api_handles_an_error_body_that_is_not_json():
    def handler(request):
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(RuntimeError, match="bad gateway"):
        email_server.api("GET", "/v1/inbound", key_reader=lambda: "rk", client_factory=client_factory_for(handler))


def test_api_reports_a_network_failure_plainly():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RuntimeError, match="Could not reach Remail"):
        email_server.api("GET", "/v1/inbound", key_reader=lambda: "rk", client_factory=client_factory_for(handler))


def test_api_accepts_an_empty_body():
    def handler(request):
        return httpx.Response(204)

    assert email_server.api("GET", "/v1/inbound", key_reader=lambda: "rk",
                            client_factory=client_factory_for(handler)) == {}


def test_describe_error_falls_back_to_a_bare_message():
    response = httpx.Response(400, json={"message": "nope"})
    assert email_server.describe_error(response) == "nope"
    assert email_server.describe_error(httpx.Response(400, json={})) == "no detail"


# --- list_inbox --------------------------------------------------------------------------------

def test_fetch_inbox_asks_only_for_guppys_own_mail():
    seen: list = []
    call = mock_api({("GET", "/v1/inbound"): {"data": [INBOUND_SUMMARY]}}, seen)
    result = email_server.fetch_inbox(10, api_call=call)
    assert seen[0]["params"] == {"limit": 10, "to": "guppy@johnnycode.ai"}
    assert result["mailbox"] == "guppy@johnnycode.ai"
    assert result["count"] == 1
    assert result["messages"][0]["subject"] == "Status of the refit"
    assert result["messages"][0]["id"] == INBOUND_SUMMARY["id"]
    assert result["messages"][0]["from"] == "Ada <ada@example.com>"
    assert result["messages"][0]["received_at"] == INBOUND_SUMMARY["received_at"]


def test_fetch_inbox_clamps_a_silly_limit():
    for asked, expected in [(0, 1), (-5, 1), (500, 50), (10, 10)]:
        seen: list = []
        email_server.fetch_inbox(asked, api_call=mock_api({("GET", "/v1/inbound"): {"data": []}}, seen))
        assert seen[0]["params"]["limit"] == expected


def test_fetch_inbox_on_an_empty_mailbox():
    result = email_server.fetch_inbox(api_call=mock_api({("GET", "/v1/inbound"): {"data": []}}))
    assert result["count"] == 0 and result["messages"] == []


def test_fetch_inbox_survives_a_response_with_no_data_key():
    assert email_server.fetch_inbox(api_call=mock_api({("GET", "/v1/inbound"): {}}))["count"] == 0


def test_summarize_names_a_subjectless_message():
    assert email_server.summarize({"id": "x"})["subject"] == "(no subject)"


def test_list_inbox_tool_returns_the_messages(monkeypatch):
    monkeypatch.setattr(email_server, "api", mock_api({("GET", "/v1/inbound"): {"data": [INBOUND_SUMMARY]}}))
    assert email_server.list_inbox(5)["count"] == 1


# --- read_email --------------------------------------------------------------------------------

def test_fetch_email_returns_the_whole_message():
    path = f"/v1/inbound/{INBOUND_SUMMARY['id']}"
    result = email_server.fetch_email(INBOUND_SUMMARY["id"], api_call=mock_api({("GET", path): INBOUND_FULL}))
    assert result["text"] == "The refit is done."
    assert result["from"] == "Ada <ada@example.com>"
    assert result["cc"] == ["scott@johnnycode.ai"]
    assert result["message_id"] == "<abc@example.com>"
    assert result["reply_to"] == "ada+replies@example.com"
    assert result["attachments"] == [{"filename": "refit.pdf", "content_type": "application/pdf",
                                      "size_bytes": 4096}]
    assert result["auth_results"]["dmarc"] == "pass"


def test_fetch_email_falls_back_to_the_html_part():
    message = {**INBOUND_FULL, "text": "", "html": "<p>Hello<br>Admiral &amp; friends</p><script>x=1</script>"}
    path = f"/v1/inbound/{INBOUND_SUMMARY['id']}"
    result = email_server.fetch_email(INBOUND_SUMMARY["id"], api_call=mock_api({("GET", path): message}))
    assert result["text"] == "Hello\nAdmiral & friends"


def test_fetch_email_says_so_when_there_is_nothing_to_read():
    message = {**INBOUND_FULL, "text": "", "html": ""}
    path = f"/v1/inbound/{INBOUND_SUMMARY['id']}"
    result = email_server.fetch_email(INBOUND_SUMMARY["id"], api_call=mock_api({("GET", path): message}))
    assert result["text"] == "(this message has no readable body)"


def test_fetch_email_rejects_something_that_is_not_an_id():
    with pytest.raises(ValueError, match="not a Remail message id"):
        email_server.fetch_email("../../etc/passwd")
    with pytest.raises(ValueError, match="not a Remail message id"):
        email_server.fetch_email("")


def test_read_email_tool_reports_a_bad_id_instead_of_raising():
    assert "not a Remail message id" in email_server.read_email("nope")["error"]


def test_clip_truncates_and_says_so():
    assert email_server.clip("abc", limit=10) == "abc"
    clipped = email_server.clip("x" * 100, limit=10)
    assert clipped.startswith("x" * 10)
    assert "90 more characters" in clipped


def test_strip_html_drops_styles_and_collapses_space():
    assert email_server.strip_html("<style>p{color:red}</style><div>a</div><div>b</div>") == "a\nb"
    assert email_server.strip_html("") == ""


# --- send_email --------------------------------------------------------------------------------

def test_build_send_payload_sends_as_guppy():
    payload = email_server.build_send_payload("ada@example.com", "Refit", "All done.")
    assert payload["from"] == "Guppy <guppy@johnnycode.ai>"
    assert payload["to"] == ["ada@example.com"]
    assert payload["subject"] == "Refit"
    assert payload["text"] == "All done."
    assert "in_reply_to" not in payload


def test_build_send_payload_splits_several_recipients():
    payload = email_server.build_send_payload("a@x.com, b@y.com; c@z.com", "s", "t")
    assert payload["to"] == ["a@x.com", "b@y.com", "c@z.com"]


def test_build_send_payload_accepts_a_display_name():
    assert email_server.build_send_payload("Ada <ada@example.com>", "s", "t")["to"] == ["Ada <ada@example.com>"]


def test_build_send_payload_refuses_nonsense():
    with pytest.raises(ValueError, match="No recipient"):
        email_server.build_send_payload("", "s", "t")
    with pytest.raises(ValueError, match="Not a usable email address"):
        email_server.build_send_payload("not-an-address", "s", "t")
    with pytest.raises(ValueError, match="empty email"):
        email_server.build_send_payload("a@b.com", "s", "   ")


def test_reply_threads_off_the_original_message_id():
    path = f"/v1/inbound/{INBOUND_SUMMARY['id']}"
    payload = email_server.build_send_payload(
        "ada@example.com", "Re: Refit", "Thanks.", INBOUND_SUMMARY["id"],
        api_call=mock_api({("GET", path): INBOUND_FULL}),
    )
    assert payload["in_reply_to"] == "<abc@example.com>"
    assert payload["references"] == ["<older@example.com>", "<abc@example.com>"]


def test_reply_accepts_a_raw_message_id_without_a_lookup():
    assert email_server.thread_headers("<raw@example.com>") == {"in_reply_to": "<raw@example.com>"}


def test_reply_to_a_message_with_no_message_id_still_sends():
    path = f"/v1/inbound/{INBOUND_SUMMARY['id']}"
    stripped = {k: v for k, v in INBOUND_FULL.items() if k != "message_id"}
    stripped["headers"] = {}
    assert email_server.thread_headers(INBOUND_SUMMARY["id"],
                                       api_call=mock_api({("GET", path): stripped})) == {}


def test_thread_headers_ignores_an_empty_value():
    assert email_server.thread_headers(None) == {}
    assert email_server.thread_headers("  ") == {}


def test_deliver_posts_once_and_reports_the_outcome():
    seen: list = []
    call = mock_api({("POST", "/v1/emails"): {"id": "e-1", "status": "queued",
                                              "from": "Guppy <guppy@johnnycode.ai>",
                                              "to": ["ada@example.com"], "subject": "Refit"}}, seen)
    result = email_server.deliver("ada@example.com", "Refit", "All done.", api_call=call)
    assert len(seen) == 1 and seen[0]["method"] == "POST" and seen[0]["path"] == "/v1/emails"
    assert seen[0]["body"]["from"] == "Guppy <guppy@johnnycode.ai>"
    assert result == {"id": "e-1", "status": "queued", "from": "Guppy <guppy@johnnycode.ai>",
                      "to": ["ada@example.com"], "subject": "Refit", "threaded": False}


def test_deliver_marks_a_threaded_reply():
    path = f"/v1/inbound/{INBOUND_SUMMARY['id']}"
    call = mock_api({("GET", path): INBOUND_FULL,
                     ("POST", "/v1/emails"): {"id": "e-2", "status": "queued"}})
    assert email_server.deliver("ada@example.com", "Re: Refit", "Thanks.",
                                INBOUND_SUMMARY["id"], api_call=call)["threaded"] is True


def test_send_email_tool_never_calls_out_when_the_input_is_bad(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("send_email must validate before it touches the network")

    monkeypatch.setattr(email_server, "api", explode)
    assert "No recipient" in email_server.send_email("", "s", "t")["error"]
    assert "empty email" in email_server.send_email("a@b.com", "s", "")["error"]


def test_send_email_tool_reports_a_remail_refusal(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("Remail refused the request (429): daily cap reached")

    monkeypatch.setattr(email_server, "api", refuse)
    assert "daily cap reached" in email_server.send_email("a@b.com", "s", "t")["error"]
