"""Tests for the devops capability.

Everything is mocked: the az token lookup is a fake subprocess runner and the HTTP layer is an
httpx.MockTransport. No test mints a real token or opens a socket to Azure DevOps, and no test
performs a write.
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
    spec = importlib.util.spec_from_file_location("guppy_capability_devops", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


devops = _load()

PROJECT = "scv2"
TEAM_ITERATIONS = "/scv2/SCV2%20Team/_apis/work/teamsettings/iterations"
WIQL = "/scv2/_apis/wit/wiql"
BATCH = "/_apis/wit/workitemsbatch"

SPRINT = {
    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "name": "Sprint 42",
    "path": "scv2\\Sprint 42",
    "attributes": {"startDate": "2026-09-14T00:00:00Z", "finishDate": "2026-09-27T00:00:00Z",
                   "timeFrame": "current"},
}

OTHER_SPRINT = {
    "id": "11111111-2222-3333-4444-555555555555",
    "name": "Sprint 41",
    "path": "scv2\\Sprint 41",
    "attributes": {"startDate": "2026-08-31T00:00:00Z", "finishDate": "2026-09-13T00:00:00Z",
                   "timeFrame": "past"},
}


def item(wid, kind="Task", title="A thing", state="Active", priority=2, tags="alpha; beta",
         assigned="Scott Smith"):
    return {"id": wid, "fields": {
        "System.WorkItemType": kind, "System.Title": title, "System.State": state,
        "Microsoft.VSTS.Common.Priority": priority, "System.Tags": tags,
        "System.AssignedTo": {"displayName": assigned, "uniqueName": "scott@matw.com"},
    }}


# --- fakes -------------------------------------------------------------------------------------

def fake_az(token: str = "eyJ.fake.token", returncode: int = 0, stderr: str = "",
            raises: Exception | None = None):
    """Stand in for `az account get-access-token`."""
    def runner(*args, **kwargs):
        if raises:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=token, stderr=stderr)
    return runner


def mock_api(routes: dict, seen: list | None = None):
    """A drop-in for server.api: looks up (METHOD, path) and records every call."""
    def call(method, path, *, params=None, body=None, api_version=None, patch=False, **_):
        if seen is not None:
            seen.append({"method": method, "path": path, "params": params, "body": body,
                         "api_version": api_version, "patch": patch})
        try:
            route = routes[(method, path)]
        except KeyError:  # pragma: no cover - a test asked for a route it did not define
            raise AssertionError(f"unexpected call {method} {path}")
        return route(body) if callable(route) else route
    return call


def client_factory_for(handler):
    """An httpx.Client whose transport is a mock, so header/URL building is really exercised."""
    def factory(**kwargs):
        kwargs.pop("timeout", None)
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    return factory


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Belt and braces: a test that forgets to mock the transport fails instead of calling Azure."""
    def refuse(**kwargs):
        raise AssertionError("a test tried to open a real connection to Azure DevOps")

    monkeypatch.setattr(devops, "_open_client", refuse)


@pytest.fixture(autouse=True)
def no_real_token(monkeypatch):
    """And no test may shell out to a real az CLI."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to run the real az CLI")

    monkeypatch.setattr(devops.subprocess, "run", refuse)


def current_sprint_routes(extra: dict | None = None):
    """The team iterations lookup every sprint-scoped call starts with, plus whatever else a test needs."""
    return {("GET", TEAM_ITERATIONS): {"value": [SPRINT]}, **(extra or {})}


# --- manifest and settings ---------------------------------------------------------------------

def test_manifest_declares_every_exposed_tool_with_an_effect():
    manifest = json.loads((HERE / "capability.json").read_text())
    assert manifest["name"] == "devops"
    assert manifest["enabled"] is True
    exposed = {t.name for t in asyncio.run(devops.server.list_tools())}
    assert exposed == {"my_work_items", "sprint_summary", "get_work_item", "search_work_items",
                       "update_work_item", "add_comment", "create_work_item"}
    assert set(manifest["effects"]) == exposed
    assert manifest["effects"]["my_work_items"] == "read"
    assert manifest["effects"]["sprint_summary"] == "read"
    assert manifest["effects"]["get_work_item"] == "read"
    assert manifest["effects"]["search_work_items"] == "read"
    assert manifest["effects"]["update_work_item"] == "act"
    assert manifest["effects"]["add_comment"] == "act"
    assert manifest["effects"]["create_work_item"] == "act"


def test_manifest_taints_only_the_readers_of_other_peoples_words():
    manifest = json.loads((HERE / "capability.json").read_text())
    assert set(manifest["taints"]) == {"get_work_item", "search_work_items"}


def test_every_tool_has_a_description_for_the_mind():
    for tool in asyncio.run(devops.server.list_tools()):
        assert (tool.description or "").strip()


def test_config_holds_the_admirals_project_and_no_secret():
    raw = json.loads((HERE / "config.json").read_text())
    assert raw == {"org": "clientsystems", "project": "scv2", "team": "SCV2 Team",
                   "tenant": "matw.com", "api_version": "7.1"}
    blob = json.dumps(raw).lower()
    for word in ("token", "secret", "password", "pat", "key"):
        assert word not in blob


def test_settings_are_read_from_config_at_runtime():
    assert devops.org_url() == "https://dev.azure.com/clientsystems"
    assert devops.project_path() == "/scv2"
    assert devops.team_path() == "/scv2/SCV2%20Team"
    assert devops.preview_version("3") == "7.1-preview.3"


def test_an_org_given_as_a_full_url_is_used_as_is(monkeypatch):
    monkeypatch.setattr(devops, "_config_cache",
                        {**devops.config(), "org": "https://dev.azure.com/other/"})
    assert devops.org_url() == "https://dev.azure.com/other"


def test_login_hint_is_the_exact_command_the_admiral_must_run():
    hint = devops.login_hint()
    assert "az login --tenant matw.com" in hint
    assert '--scope "499b84ac-1321-427f-aa17-267ca6975798/.default"' in hint
    assert "--allow-no-subscriptions" in hint


# --- the token ---------------------------------------------------------------------------------

def test_token_comes_from_the_az_cli():
    assert devops.access_token(runner=fake_az("eyJ.abc\n")) == "eyJ.abc"


def test_token_is_asked_for_with_the_right_resource_and_tenant():
    asked = {}

    def runner(args, **kwargs):
        asked["args"] = args
        return SimpleNamespace(returncode=0, stdout="eyJ.x", stderr="")

    devops.access_token(runner=runner)
    assert asked["args"] == ["az", "account", "get-access-token",
                             "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
                             "--tenant", "matw.com", "--query", "accessToken", "-o", "tsv"]


def test_a_failed_login_tells_the_admiral_how_to_fix_it():
    runner = fake_az("", returncode=1, stderr="ERROR: AADSTS50020: User account does not exist in tenant\n")
    with pytest.raises(RuntimeError) as err:
        devops.access_token(runner=runner)
    message = str(err.value)
    assert "AADSTS50020" in message
    assert "az login --tenant matw.com" in message


def test_an_empty_token_is_a_failure_not_a_blank_bearer():
    with pytest.raises(RuntimeError, match="az login"):
        devops.access_token(runner=fake_az("   "))


def test_a_missing_az_cli_is_a_plain_message():
    with pytest.raises(RuntimeError, match="az CLI is not installed"):
        devops.access_token(runner=fake_az(raises=FileNotFoundError("az")))


def test_az_blowing_up_is_reported_not_swallowed():
    with pytest.raises(RuntimeError, match="Could not run the az CLI"):
        devops.access_token(runner=fake_az(raises=OSError("boom")))


def test_the_token_never_appears_in_a_failure_message():
    """az writes the token to stdout; only stderr may ever be quoted back."""
    runner = fake_az("eyJ.super.secret", returncode=1, stderr="ERROR: interactive login required")
    with pytest.raises(RuntimeError) as err:
        devops.access_token(runner=runner)
    assert "eyJ.super.secret" not in str(err.value)


def test_every_tool_reports_a_missing_login_instead_of_raising(monkeypatch):
    monkeypatch.setattr(devops, "access_token",
                        lambda **_: (_ for _ in ()).throw(RuntimeError("not logged in. " + devops.login_hint())))
    for result in (
        devops.my_work_items(),
        devops.sprint_summary(),
        devops.get_work_item(123),
        devops.search_work_items("refit"),
        devops.update_work_item(123, state="Active"),
        devops.add_comment(123, "hello"),
        devops.create_work_item("Task", "A new task"),
    ):
        assert "az login --tenant matw.com" in result["error"]


# --- the HTTP layer ----------------------------------------------------------------------------

def test_api_sends_the_bearer_token_and_never_returns_it():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"value": []})

    result = devops.api("GET", "/scv2/_apis/wit/wiql", params={"$top": 5},
                        token_reader=lambda: "eyJ.secret",
                        client_factory=client_factory_for(handler))
    assert captured["auth"] == "Bearer eyJ.secret"
    assert captured["url"] == "https://dev.azure.com/clientsystems/scv2/_apis/wit/wiql?%24top=5&api-version=7.1"
    assert "eyJ.secret" not in json.dumps(result)


def test_api_stamps_the_configured_api_version_and_honours_an_override():
    seen = {}

    def handler(request):
        seen.setdefault("urls", []).append(str(request.url))
        return httpx.Response(200, json={})

    factory = client_factory_for(handler)
    devops.api("GET", "/x", token_reader=lambda: "t", client_factory=factory)
    devops.api("GET", "/x", api_version="7.1-preview.4", token_reader=lambda: "t", client_factory=factory)
    assert seen["urls"][0].endswith("api-version=7.1")
    assert seen["urls"][1].endswith("api-version=7.1-preview.4")


def test_a_patch_call_uses_the_json_patch_content_type():
    seen = {}

    def handler(request):
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7})

    devops.api("PATCH", "/scv2/_apis/wit/workitems/7", body=[{"op": "add"}], patch=True,
               token_reader=lambda: "t", client_factory=client_factory_for(handler))
    assert seen["content_type"] == "application/json-patch+json"
    assert seen["body"] == [{"op": "add"}]


def test_a_plain_call_uses_plain_json():
    seen = {}

    def handler(request):
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={})

    devops.api("POST", "/scv2/_apis/wit/wiql", body={"query": "x"},
               token_reader=lambda: "t", client_factory=client_factory_for(handler))
    assert seen["content_type"] == "application/json"


def test_a_401_is_read_as_a_stale_login():
    def handler(request):
        return httpx.Response(401, json={"message": "unauthorized"})

    with pytest.raises(RuntimeError, match="az login --tenant matw.com"):
        devops.api("GET", "/x", token_reader=lambda: "t", client_factory=client_factory_for(handler))


def test_a_203_sign_in_page_is_read_as_a_stale_login():
    def handler(request):
        return httpx.Response(203, text="<html>sign in</html>")

    with pytest.raises(RuntimeError, match="az login --tenant matw.com"):
        devops.api("GET", "/x", token_reader=lambda: "t", client_factory=client_factory_for(handler))


def test_api_turns_an_error_response_into_a_spoken_message():
    def handler(request):
        return httpx.Response(400, json={"message": "TF401232: Work item 9 does not exist"})

    with pytest.raises(RuntimeError) as err:
        devops.api("GET", "/x", token_reader=lambda: "t", client_factory=client_factory_for(handler))
    assert "400" in str(err.value)
    assert "does not exist" in str(err.value)


def test_api_handles_an_error_body_that_is_not_json():
    def handler(request):
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(RuntimeError, match="bad gateway"):
        devops.api("GET", "/x", token_reader=lambda: "t", client_factory=client_factory_for(handler))


def test_api_reports_a_network_failure_plainly():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RuntimeError, match="Could not reach Azure DevOps"):
        devops.api("GET", "/x", token_reader=lambda: "t", client_factory=client_factory_for(handler))


def test_api_accepts_an_empty_body_and_a_bare_list():
    def empty(request):
        return httpx.Response(204)

    def listed(request):
        return httpx.Response(200, json=[1, 2])

    assert devops.api("GET", "/x", token_reader=lambda: "t",
                      client_factory=client_factory_for(empty)) == {}
    assert devops.api("GET", "/x", token_reader=lambda: "t",
                      client_factory=client_factory_for(listed)) == {"value": [1, 2]}


def test_describe_error_falls_back_sensibly():
    assert devops.describe_error(httpx.Response(400, json={"value": {"Message": "nope"}})) == "nope"
    assert devops.describe_error(httpx.Response(400, json={})) == "no detail"


# --- text ------------------------------------------------------------------------------------

def test_strip_html_makes_a_description_readable():
    markup = ("<div>Steps:</div><ul><li>Open the page</li><li>Click <b>Save</b></li></ul>"
              "<style>p{color:red}</style><p>It&nbsp;crashes &amp; burns.</p>")
    # the list ends, so a blank line separates it from the paragraph that follows
    assert devops.strip_html(markup) == "Steps:\n- Open the page\n- Click Save\n\nIt crashes & burns."
    assert devops.strip_html("") == ""
    assert devops.strip_html(None) == ""


def test_clip_truncates_and_says_so():
    assert devops.clip("abc", limit=10) == "abc"
    clipped = devops.clip("x" * 100, limit=10)
    assert clipped.startswith("x" * 10)
    assert "90 more characters" in clipped


def test_person_reads_an_identity_field():
    assert devops.person({"displayName": "Scott Smith", "uniqueName": "s@matw.com"}) == "Scott Smith"
    assert devops.person({"uniqueName": "s@matw.com"}) == "s@matw.com"
    assert devops.person(None) == ""


def test_wiql_literals_escape_quotes():
    assert devops.wiql_literal("it's") == "'it''s'"
    assert devops.wiql_literal("plain") == "'plain'"


def test_tag_strings_split_on_semicolons_and_commas():
    assert devops.split_tags("alpha; beta , gamma") == ["alpha", "beta", "gamma"]
    assert devops.split_tags(None) == []


# --- iterations --------------------------------------------------------------------------------

def test_the_current_iteration_is_asked_for_by_timeframe():
    seen: list = []
    result = devops.resolve_iteration("current", api_call=mock_api(current_sprint_routes(), seen))
    assert seen[0]["path"] == TEAM_ITERATIONS
    assert seen[0]["params"] == {"$timeframe": "current"}
    assert result == {"id": SPRINT["id"], "name": "Sprint 42", "path": "scv2\\Sprint 42",
                      "start_date": "2026-09-14T00:00:00Z", "finish_date": "2026-09-27T00:00:00Z"}


def test_a_blank_iteration_still_means_the_current_one():
    for asked in ("", "  ", "this sprint", "@current"):
        seen: list = []
        devops.resolve_iteration(asked, api_call=mock_api(current_sprint_routes(), seen))
        assert seen[0]["params"] == {"$timeframe": "current"}


def test_no_current_iteration_is_a_plain_message():
    with pytest.raises(RuntimeError, match="no current iteration"):
        devops.resolve_iteration("current", api_call=mock_api({("GET", TEAM_ITERATIONS): {"value": []}}))


def test_a_named_iteration_is_matched_by_name_or_path_tail():
    routes = {("GET", TEAM_ITERATIONS): {"value": [OTHER_SPRINT, SPRINT]}}
    for asked in ("Sprint 41", "sprint 41"):
        assert devops.resolve_iteration(asked, api_call=mock_api(routes))["id"] == OTHER_SPRINT["id"]


def test_an_unknown_iteration_lists_what_there_is():
    routes = {("GET", TEAM_ITERATIONS): {"value": [OTHER_SPRINT, SPRINT]}}
    with pytest.raises(ValueError) as err:
        devops.resolve_iteration("Sprint 99", api_call=mock_api(routes))
    assert "Sprint 41" in str(err.value) and "Sprint 42" in str(err.value)


# --- my_work_items -----------------------------------------------------------------------------

def test_my_work_items_queries_me_in_the_current_sprint_and_hides_done_work():
    seen: list = []
    routes = current_sprint_routes({
        ("POST", WIQL): {"workItems": [{"id": 101}, {"id": 102}]},
        ("POST", BATCH): {"value": [item(102, "Bug", "Second", "Active", 1, "regression"),
                                    item(101, "Task", "First", "New", 2, "alpha; beta")]},
    })
    result = devops.mine_in_iteration(api_call=mock_api(routes, seen))

    query = seen[1]["body"]["query"]
    assert "[System.AssignedTo] = @Me" in query
    assert "[System.TeamProject] = 'scv2'" in query
    assert "[System.IterationPath] UNDER 'scv2\\Sprint 42'" in query
    assert "[System.State] NOT IN ('Closed', 'Done', 'Removed')" in query

    assert result["iteration"]["name"] == "Sprint 42"
    assert result["count"] == 2
    # the WIQL ordering survives the batch fetch, which comes back in its own order
    assert [i["id"] for i in result["work_items"]] == [101, 102]
    assert result["work_items"][0] == {"id": 101, "type": "Task", "title": "First", "state": "New",
                                       "priority": 2, "tags": ["alpha", "beta"]}


def test_my_work_items_can_include_finished_work():
    seen: list = []
    routes = current_sprint_routes({("POST", WIQL): {"workItems": []}})
    result = devops.mine_in_iteration(include_done=True, api_call=mock_api(routes, seen))
    assert "NOT IN" not in seen[1]["body"]["query"]
    assert result["include_done"] is True
    assert result["count"] == 0 and result["work_items"] == []


def test_my_work_items_can_be_asked_for_a_named_sprint():
    seen: list = []
    routes = {("GET", TEAM_ITERATIONS): {"value": [OTHER_SPRINT, SPRINT]},
              ("POST", WIQL): {"workItems": []}}
    result = devops.mine_in_iteration("Sprint 41", api_call=mock_api(routes, seen))
    assert "'scv2\\Sprint 41'" in seen[1]["body"]["query"]
    assert result["iteration"]["name"] == "Sprint 41"


def test_no_matching_ids_means_no_batch_call():
    seen: list = []
    routes = current_sprint_routes({("POST", WIQL): {"workItems": []}})
    devops.mine_in_iteration(api_call=mock_api(routes, seen))
    assert [c["path"] for c in seen] == [TEAM_ITERATIONS, WIQL]


def test_the_batch_fetch_asks_only_for_the_fields_guppy_reads_out():
    seen: list = []
    routes = current_sprint_routes({("POST", WIQL): {"workItems": [{"id": 5}]},
                                      ("POST", BATCH): {"value": [item(5)]}})
    devops.mine_in_iteration(api_call=mock_api(routes, seen))
    assert seen[2]["body"]["ids"] == [5]
    assert "System.Title" in seen[2]["body"]["fields"]
    assert "System.State" in seen[2]["body"]["fields"]


def test_a_long_list_of_ids_is_split_into_batches_the_api_accepts():
    seen: list = []
    ids = list(range(1, 251))
    routes = {("POST", BATCH): lambda body: {"value": [item(i) for i in body["ids"]]}}
    got = devops.fetch_fields(ids, api_call=mock_api(routes, seen))
    assert [len(c["body"]["ids"]) for c in seen] == [200, 50]
    assert len(got) == 250


def test_my_work_items_tool_returns_the_list(monkeypatch):
    routes = current_sprint_routes({("POST", WIQL): {"workItems": [{"id": 9}]},
                                      ("POST", BATCH): {"value": [item(9)]}})
    monkeypatch.setattr(devops, "api", mock_api(routes))
    assert devops.my_work_items()["count"] == 1


# --- sprint_summary ----------------------------------------------------------------------------

def test_sprint_summary_counts_the_team_and_the_admiral_separately():
    seen: list = []
    queries: list = []

    def wiql(body):
        queries.append(body["query"])
        if "@Me" in body["query"]:
            return {"workItems": [{"id": 1}]}
        return {"workItems": [{"id": 1}, {"id": 2}, {"id": 3}]}

    routes = current_sprint_routes({
        ("POST", WIQL): wiql,
        ("POST", BATCH): {"value": [item(1, "Bug", state="Active"),
                                    item(2, "Task", state="New"),
                                    item(3, "Task", state="Active")]},
    })
    result = devops.summarize_sprint(api_call=mock_api(routes, seen))

    assert result["iteration"]["name"] == "Sprint 42"
    assert result["team"] == {"total": 3, "by_state": {"Active": 2, "New": 1},
                              "by_type": {"Bug": 1, "Task": 2}}
    assert result["mine"] == {"total": 1, "by_state": {"Active": 1}, "by_type": {"Bug": 1}}
    assert any("@Me" in q for q in queries) and any("@Me" not in q for q in queries)
    # one iteration lookup, two WIQL queries, one batch fetch
    assert [c["path"] for c in seen] == [TEAM_ITERATIONS, WIQL, WIQL, BATCH]


def test_sprint_summary_on_an_empty_sprint():
    routes = current_sprint_routes({("POST", WIQL): {"workItems": []}})
    result = devops.summarize_sprint(api_call=mock_api(routes))
    assert result["team"]["total"] == 0 and result["mine"]["total"] == 0


def test_tally_names_a_stateless_item():
    assert devops.tally([{"state": "", "type": ""}])["by_state"] == {"(none)": 1}


def test_sprint_summary_tool_reports_a_refusal(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("Azure DevOps refused the request (403): no access to scv2")

    monkeypatch.setattr(devops, "api", refuse)
    assert "no access to scv2" in devops.sprint_summary()["error"]


# --- search_work_items -------------------------------------------------------------------------

def test_search_asks_wiql_for_a_contains_on_title_and_description():
    seen: list = []
    routes = {("POST", WIQL): {"workItems": [{"id": 77}]},
              ("POST", BATCH): {"value": [item(77, "Bug", "Login is broken")]}}
    result = devops.find_work_items("login", api_call=mock_api(routes, seen))
    query = seen[0]["body"]["query"]
    assert "[System.Title] CONTAINS 'login'" in query
    assert "[System.Description] CONTAINS 'login'" in query
    assert "[System.TeamProject] = 'scv2'" in query
    assert seen[0]["params"] == {"$top": 20}
    assert result == {"query": "login", "count": 1,
                      "work_items": [{"id": 77, "type": "Bug", "title": "Login is broken",
                                      "state": "Active", "priority": 2, "tags": ["alpha", "beta"]}]}


def test_search_escapes_a_quote_instead_of_letting_it_break_the_query():
    seen: list = []
    routes = {("POST", WIQL): {"workItems": []}}
    devops.find_work_items("it's broken' OR 1=1 --", api_call=mock_api(routes, seen))
    assert "'it''s broken'' OR 1=1 --'" in seen[0]["body"]["query"]


def test_search_clamps_the_result_count():
    for asked, expected in [(0, 1), (-3, 1), (500, 100), (20, 20)]:
        seen: list = []
        devops.find_work_items("x", asked, api_call=mock_api({("POST", WIQL): {"workItems": []}}, seen))
        assert seen[0]["params"]["$top"] == expected


def test_search_refuses_an_empty_needle():
    with pytest.raises(ValueError, match="Nothing to search for"):
        devops.find_work_items("   ")


def test_search_tool_reports_an_empty_needle_without_calling_out(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("search must validate before it touches the network")

    monkeypatch.setattr(devops, "api", explode)
    assert "Nothing to search for" in devops.search_work_items("")["error"]


# --- get_work_item -----------------------------------------------------------------------------

FULL_ITEM = {
    "id": 1234,
    "fields": {
        "System.WorkItemType": "Bug",
        "System.Title": "  Save button does nothing  ",
        "System.State": "Active",
        "System.Reason": "New",
        "Microsoft.VSTS.Common.Priority": 1,
        "System.Tags": "regression; ui",
        "System.AssignedTo": {"displayName": "Scott Smith", "uniqueName": "scott@matw.com"},
        "System.CreatedBy": {"displayName": "Dana Tester"},
        "System.CreatedDate": "2026-09-15T12:00:00Z",
        "System.ChangedDate": "2026-09-21T09:30:00Z",
        "System.IterationPath": "scv2\\Sprint 42",
        "System.AreaPath": "scv2\\Web",
        "System.Description": "<div>The save button <b>does nothing</b>.</div>",
        "Microsoft.VSTS.Common.AcceptanceCriteria": "<ul><li>Saving persists</li></ul>",
        "Microsoft.VSTS.TCM.ReproSteps": "<p>1. Open the form<br>2. Click Save</p>",
    },
    "relations": [
        {"rel": "System.LinkTypes.Hierarchy-Reverse",
         "url": "https://dev.azure.com/clientsystems/_apis/wit/workItems/1000"},
        {"rel": "AttachedFile", "url": "https://example.com/attachments/abc"},
    ],
}

COMMENTS_PATH = "/scv2/_apis/wit/workItems/1234/comments"


def detail_routes(comments=None):
    return {
        ("GET", "/scv2/_apis/wit/workitems/1234"): FULL_ITEM,
        ("GET", COMMENTS_PATH): {"comments": comments if comments is not None else [
            {"id": 3, "text": "<p>Still broken after the fix.</p>",
             "createdBy": {"displayName": "Dana Tester"}, "createdDate": "2026-09-21T09:00:00Z"},
            {"id": 2, "text": "Deployed to test.",
             "createdBy": {"displayName": "Scott Smith"}, "createdDate": "2026-09-20T17:00:00Z"},
        ]},
    }


def test_get_work_item_returns_the_whole_ticket_as_plain_text():
    seen: list = []
    result = devops.work_item_detail(1234, api_call=mock_api(detail_routes(), seen))

    assert seen[0]["params"] == {"$expand": "all"}
    assert result["id"] == 1234
    assert result["type"] == "Bug"
    assert result["title"] == "Save button does nothing"
    assert result["state"] == "Active"
    assert result["priority"] == 1
    assert result["tags"] == ["regression", "ui"]
    assert result["assigned_to"] == "Scott Smith"
    assert result["created_by"] == "Dana Tester"
    assert result["iteration_path"] == "scv2\\Sprint 42"
    assert result["area_path"] == "scv2\\Web"
    assert result["parent_id"] == 1000
    assert result["description"] == "The save button does nothing."
    assert result["acceptance_criteria"] == "- Saving persists"
    assert result["repro_steps"] == "1. Open the form\n2. Click Save"
    assert result["url"] == "https://dev.azure.com/clientsystems/scv2/_workitems/edit/1234"
    assert "<" not in result["description"] + result["acceptance_criteria"] + result["repro_steps"]


def test_get_work_item_reads_the_latest_comments_oldest_first():
    seen: list = []
    result = devops.work_item_detail(1234, api_call=mock_api(detail_routes(), seen))
    assert seen[1]["params"] == {"$top": 10, "order": "desc"}
    assert seen[1]["api_version"] == "7.1-preview.4"
    assert [c["id"] for c in result["comments"]] == [2, 3]
    assert result["comments"][1] == {"id": 3, "by": "Dana Tester", "at": "2026-09-21T09:00:00Z",
                                     "text": "Still broken after the fix."}


def test_a_ticket_with_no_comments_is_still_readable():
    result = devops.work_item_detail(1234, api_call=mock_api(detail_routes(comments=[])))
    assert result["comments"] == []
    assert "comments_error" not in result


def test_a_comments_failure_does_not_lose_the_ticket():
    def call(method, path, **kwargs):
        if path == COMMENTS_PATH:
            raise RuntimeError("Azure DevOps refused the request (404): comments are off")
        return FULL_ITEM

    result = devops.work_item_detail(1234, api_call=call)
    assert result["title"] == "Save button does nothing"
    assert result["comments"] == []
    assert "comments are off" in result["comments_error"]


def test_a_ticket_with_no_parent_says_so():
    raw = {**FULL_ITEM, "relations": [{"rel": "AttachedFile", "url": "https://x/1"}]}
    routes = {("GET", "/scv2/_apis/wit/workitems/1234"): raw, ("GET", COMMENTS_PATH): {"comments": []}}
    assert devops.work_item_detail(1234, api_call=mock_api(routes))["parent_id"] is None
    assert devops.parent_of({}) is None


def test_an_id_that_is_not_an_id_is_refused_before_any_call():
    for bad in ("banana", "", None, 0, -4, "12x"):
        with pytest.raises(ValueError, match="not a work item id"):
            devops.coerce_id(bad)
    assert devops.coerce_id("#1234") == 1234
    assert devops.coerce_id(" 77 ") == 77


def test_get_work_item_tool_reports_a_bad_id_without_calling_out(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("get_work_item must validate before it touches the network")

    monkeypatch.setattr(devops, "api", explode)
    assert "not a work item id" in devops.get_work_item("banana")["error"]


# --- update_work_item --------------------------------------------------------------------------

UPDATE_PATH = "/scv2/_apis/wit/workitems/1234"
TAGS_ROUTE = {"id": 1234, "fields": {"System.Tags": "regression; ui"}}


def test_update_patches_only_what_was_asked_for():
    seen: list = []
    routes = {("PATCH", UPDATE_PATH): item(1234, "Bug", "Save button does nothing", "Resolved", 1, "ui")}
    result = devops.apply_update(1234, state="Resolved", api_call=mock_api(routes, seen))
    assert seen[0]["method"] == "PATCH" and seen[0]["patch"] is True
    assert seen[0]["body"] == [{"op": "add", "path": "/fields/System.State", "value": "Resolved"}]
    assert result["state"] == "Resolved"
    assert result["changed"] == ["System.State"]
    assert result["url"] == "https://dev.azure.com/clientsystems/scv2/_workitems/edit/1234"


def test_update_can_change_several_fields_at_once():
    seen: list = []
    routes = {("PATCH", UPDATE_PATH): item(1234)}
    devops.apply_update(1234, state="Active", assigned_to="scott@matw.com", title="Better title",
                        priority=1, api_call=mock_api(routes, seen))
    ops = {op["path"]: op["value"] for op in seen[0]["body"]}
    assert ops == {"/fields/System.Title": "Better title", "/fields/System.State": "Active",
                   "/fields/System.AssignedTo": "scott@matw.com",
                   "/fields/Microsoft.VSTS.Common.Priority": 1}


def test_adding_a_tag_keeps_the_ones_already_there():
    seen: list = []
    routes = {("GET", UPDATE_PATH): TAGS_ROUTE, ("PATCH", UPDATE_PATH): item(1234)}
    devops.apply_update(1234, add_tags="kickback", api_call=mock_api(routes, seen))
    assert seen[0]["params"] == {"fields": "System.Tags"}
    assert seen[1]["body"] == [{"op": "add", "path": "/fields/System.Tags",
                               "value": "regression; ui; kickback"}]


def test_removing_a_tag_leaves_the_rest():
    seen: list = []
    routes = {("GET", UPDATE_PATH): TAGS_ROUTE, ("PATCH", UPDATE_PATH): item(1234)}
    devops.apply_update(1234, remove_tags="ui", api_call=mock_api(routes, seen))
    assert seen[1]["body"][0]["value"] == "regression"


def test_tag_merging_is_case_insensitive_and_keeps_no_duplicates():
    assert devops.merge_tags(["Regression", "ui"], ["REGRESSION", "new"], None) == ["Regression", "ui", "new"]
    assert devops.merge_tags(["Regression", "ui"], None, ["regression"]) == ["ui"]
    assert devops.merge_tags([], ["a", "a", " "], None) == ["a"]


def test_tags_may_arrive_as_a_string_or_a_list():
    assert devops.as_list("a; b") == ["a", "b"]
    assert devops.as_list(["a", " b "]) == ["a", "b"]
    assert devops.as_list(None) == []


def test_unassigning_removes_the_field():
    seen: list = []
    routes = {("PATCH", UPDATE_PATH): item(1234)}
    devops.apply_update(1234, assigned_to="unassigned", api_call=mock_api(routes, seen))
    assert seen[0]["body"] == [{"op": "remove", "path": "/fields/System.AssignedTo"}]


def test_update_refuses_nonsense_before_it_changes_anything():
    with pytest.raises(ValueError, match="Nothing to change"):
        devops.apply_update(1234)
    with pytest.raises(ValueError, match="empty title"):
        devops.build_patch(title="   ")
    with pytest.raises(ValueError, match="between 1 and 4"):
        devops.build_patch(priority=9)
    with pytest.raises(ValueError, match="must be a number"):
        devops.build_patch(priority="urgent")


def test_update_tool_leaves_blank_arguments_alone(monkeypatch):
    seen: list = []
    routes = {("PATCH", UPDATE_PATH): item(1234)}
    monkeypatch.setattr(devops, "api", mock_api(routes, seen))
    devops.update_work_item(1234, state="Active")
    assert seen[0]["body"] == [{"op": "add", "path": "/fields/System.State", "value": "Active"}]


def test_update_tool_reports_an_empty_change_without_calling_out(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("update must validate before it touches the network")

    monkeypatch.setattr(devops, "api", explode)
    assert "Nothing to change" in devops.update_work_item(1234)["error"]


# --- add_comment -------------------------------------------------------------------------------

def test_a_comment_is_posted_to_the_discussion():
    seen: list = []
    routes = {("POST", COMMENTS_PATH): {"id": 9, "createdBy": {"displayName": "Scott Smith"},
                                        "createdDate": "2026-09-22T10:00:00Z"}}
    result = devops.post_comment(1234, "  Fixed in build 88.  ", api_call=mock_api(routes, seen))
    assert seen[0]["body"] == {"text": "Fixed in build 88."}
    assert seen[0]["api_version"] == "7.1-preview.3"
    assert result == {"work_item_id": 1234, "comment_id": 9, "by": "Scott Smith",
                      "at": "2026-09-22T10:00:00Z", "text": "Fixed in build 88.",
                      "url": "https://dev.azure.com/clientsystems/scv2/_workitems/edit/1234"}


def test_an_empty_comment_is_refused_before_any_call(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("add_comment must validate before it touches the network")

    monkeypatch.setattr(devops, "api", explode)
    assert "empty comment" in devops.add_comment(1234, "   ")["error"]
    assert "not a work item id" in devops.add_comment("nope", "hello")["error"]


# --- create_work_item --------------------------------------------------------------------------

CREATE_BUG = "/scv2/_apis/wit/workitems/$Bug"
CREATE_TASK = "/scv2/_apis/wit/workitems/$Task"
CREATE_STORY = "/scv2/_apis/wit/workitems/$User%20Story"


def test_creating_a_task_puts_it_in_the_current_sprint():
    seen: list = []
    routes = current_sprint_routes({
        ("POST", CREATE_TASK): {"id": 4321, "fields": {
            "System.WorkItemType": "Task", "System.Title": "Wire up the export",
            "System.State": "New", "System.IterationPath": "scv2\\Sprint 42"}},
    })
    result = devops.create_item("task", "Wire up the export", "Do the thing.",
                                api_call=mock_api(routes, seen))
    body = {op["path"]: op["value"] for op in seen[1]["body"]}
    assert seen[1]["patch"] is True
    assert body["/fields/System.Title"] == "Wire up the export"
    assert body["/fields/System.Description"] == "Do the thing."
    assert body["/fields/System.IterationPath"] == "scv2\\Sprint 42"
    assert result["id"] == 4321
    assert result["url"] == "https://dev.azure.com/clientsystems/scv2/_workitems/edit/4321"


def test_a_bugs_story_goes_into_its_repro_steps():
    patch = devops.build_create_patch("Bug", "Save is broken", "1. Click save 2. Nothing happens")
    fields = {op["path"]: op["value"] for op in patch}
    assert fields["/fields/Microsoft.VSTS.TCM.ReproSteps"] == "1. Click save 2. Nothing happens"
    assert "/fields/System.Description" not in fields


def test_a_parent_becomes_a_hierarchy_link():
    patch = devops.build_create_patch("Task", "Child work", parent_id=1000)
    link = [op for op in patch if op["path"] == "/relations/-"][0]["value"]
    assert link["rel"] == "System.LinkTypes.Hierarchy-Reverse"
    assert link["url"] == "https://dev.azure.com/clientsystems/_apis/wit/workItems/1000"


def test_tags_are_joined_into_the_one_field_azure_devops_has():
    patch = devops.build_create_patch("Task", "T", tags="alpha, beta")
    assert [op for op in patch if op["path"] == "/fields/System.Tags"][0]["value"] == "alpha; beta"


def test_a_user_story_goes_to_the_url_encoded_type():
    seen: list = []
    routes = {("POST", CREATE_STORY): {"id": 5, "fields": {"System.WorkItemType": "User Story"}}}
    devops.create_item("user story", "As a user...", iteration="", api_call=mock_api(routes, seen))
    assert seen[0]["path"] == CREATE_STORY


def test_creating_without_an_iteration_never_looks_one_up():
    seen: list = []
    routes = {("POST", CREATE_TASK): {"id": 6, "fields": {}}}
    for asked in ("", "none", "backlog"):
        devops.create_item("Task", "T", iteration=asked, api_call=mock_api(routes, seen))
    assert all(c["path"] == CREATE_TASK for c in seen)
    assert all("/fields/System.IterationPath" not in {op["path"] for op in c["body"]} for c in seen)


def test_only_the_three_types_are_allowed():
    assert devops.normalize_type("BUG") == "Bug"
    assert devops.normalize_type(" user  story ") == "User Story"
    assert devops.normalize_type("story") == "User Story"
    with pytest.raises(ValueError, match="Bug, Task, or User Story"):
        devops.normalize_type("Epic")
    with pytest.raises(ValueError, match="Bug, Task, or User Story"):
        devops.normalize_type("")


def test_creating_needs_a_title():
    with pytest.raises(ValueError, match="needs a title"):
        devops.build_create_patch("Task", "   ")


def test_create_tool_reports_a_bad_type_without_calling_out(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("create must validate before it touches the network")

    monkeypatch.setattr(devops, "api", explode)
    assert "Bug, Task, or User Story" in devops.create_work_item("Epic", "Something big")["error"]
