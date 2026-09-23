"""Azure DevOps capability: the Admiral's work items in clientsystems/scv2.

No secret is stored anywhere. At call time a short-lived bearer token is minted from the Admiral's
own `az` CLI login (the Azure DevOps resource id 499b84ac-...), pinned to the configured
subscription so the right signed-in identity is used. That token is used for exactly one HTTP call
and is never printed, logged, returned to the Mind, or written to disk.

Non-secret settings (org, project, team, tenant, subscription, api version) live in config.json
beside this file. The subscription picks the identity for the token; the tenant is only used in the
"here is how to sign in" help message.

Plain functions hold the logic (tested in test_devops.py); the MCP tools are thin wrappers.
"""
from __future__ import annotations

import html as html_module
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer

server = MCPServer("devops")

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

# The Azure DevOps resource id. Constant for every tenant, and not a secret.
DEVOPS_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"

TIMEOUT = 45.0
TEXT_LIMIT = 12_000  # characters of one description/field handed to the Mind
BATCH_LIMIT = 200    # ids per workitemsbatch call, per the API
WIQL_TEXT_LIMIT = 200
DONE_STATES = ("Closed", "Done", "Removed")
WORK_ITEM_TYPES = {"bug": "Bug", "task": "Task", "user story": "User Story", "story": "User Story",
                   "userstory": "User Story"}

SUMMARY_FIELDS = ["System.WorkItemType", "System.Title", "System.State",
                  "Microsoft.VSTS.Common.Priority", "System.Tags", "System.AssignedTo"]

_config_cache: dict | None = None


# --- settings ----------------------------------------------------------------------------------

def config() -> dict:
    """The non-secret settings beside this file: org, project, team, tenant, subscription, api_version."""
    global _config_cache
    if _config_cache is None:
        _config_cache = json.loads(CONFIG_PATH.read_text())
    return _config_cache


def org_url() -> str:
    org = config()["org"]
    return org.rstrip("/") if org.startswith("http") else f"https://dev.azure.com/{quote(org)}"


def seg(value: str) -> str:
    """One URL path segment (the team name has a space in it)."""
    return quote(str(value), safe="")


def project_path() -> str:
    return f"/{seg(config()['project'])}"


def team_path() -> str:
    return f"/{seg(config()['project'])}/{seg(config()['team'])}"


def preview_version(minor: str) -> str:
    """The comments API only exists as a preview of the configured api version."""
    return f"{config()['api_version']}-preview.{minor}"


def login_hint() -> str:
    """Exactly what the Admiral has to run to make this capability work."""
    tenant = config()["tenant"]
    return (f'The {tenant} account has to be signed in to the az CLI. '
            f'Run: az login --tenant {tenant} --scope "{DEVOPS_RESOURCE}/.default" --allow-no-subscriptions')


# --- the token ---------------------------------------------------------------------------------

def access_token(runner: Callable[..., Any] = subprocess.run) -> str:
    """A short-lived Azure DevOps bearer token from the Admiral's az CLI login.

    The token is minted against the configured subscription rather than the tenant: the Admiral's az
    CLI holds several signed-in accounts, and only the subscription names the one that can see this
    organisation. Never logged, never returned to the Mind, never written down. Only az's *stderr* is
    ever quoted back, because its stdout is the token itself.
    """
    subscription = config()["subscription"]
    command = ["az", "account", "get-access-token", "--resource", DEVOPS_RESOURCE,
               "--subscription", subscription, "--query", "accessToken", "-o", "tsv"]
    try:
        done = runner(command, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError(f"The az CLI is not installed, so Azure DevOps is unavailable. {login_hint()}") from None
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Could not run the az CLI for an Azure DevOps token: {e}. {login_hint()}") from None
    token = (done.stdout or "").strip()
    if done.returncode != 0 or not token:
        detail = (done.stderr or "").strip().splitlines()
        reason = detail[0][:200] if detail else "no detail from az"
        raise RuntimeError(
            f"Could not get an Azure DevOps token from subscription {subscription} ({reason}). {login_hint()}"
        )
    return token


# --- the HTTP layer ----------------------------------------------------------------------------

def _open_client(**kwargs) -> httpx.Client:
    return httpx.Client(**kwargs)


def api(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: Any = None,
    api_version: str | None = None,
    patch: bool = False,
    token_reader: Callable[[], str] | None = None,
    client_factory: Callable[..., httpx.Client] | None = None,
) -> dict:
    """One authenticated Azure DevOps REST call. Raises RuntimeError with a spoken-language message."""
    token = (token_reader or access_token)()
    client_factory = client_factory or _open_client
    url = f"{org_url()}{path}"
    query = {**(params or {}), "api-version": api_version or config()["api_version"]}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json-patch+json" if patch else "application/json",
    }
    try:
        with client_factory(timeout=TIMEOUT) as client:
            response = client.request(method, url, params=query, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Azure DevOps at {org_url()}: {e}") from None
    if response.status_code in (401, 203):
        # 203 is Azure DevOps handing back its sign-in page instead of data.
        raise RuntimeError(f"Azure DevOps rejected the sign-in for {config()['tenant']}. {login_hint()}")
    if response.status_code >= 400:
        raise RuntimeError(
            f"Azure DevOps refused the request ({response.status_code}): {describe_error(response)}")
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(
            f"Azure DevOps returned something that was not JSON ({response.status_code}). {login_hint()}") from None
    return payload if isinstance(payload, dict) else {"value": payload}


def describe_error(response: httpx.Response) -> str:
    """Azure DevOps' own message, which usually says exactly what is wrong with the request."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or "no detail"
    if not isinstance(payload, dict):
        return str(payload)[:300]
    message = payload.get("message")
    if not message and isinstance(payload.get("value"), dict):
        message = payload["value"].get("Message")
    return str(message or "no detail")[:400]


# --- text --------------------------------------------------------------------------------------

def strip_html(markup: str) -> str:
    """Work item descriptions are HTML. Turn one into something that reads aloud."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li|ul|ol|table)>", "\n", text)
    text = re.sub(r"(?i)<li\b[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" +([,.;:!?%)\]])", r"\1", text)  # tags become spaces; do not orphan the punctuation
    text = re.sub(r"([(\[]) +", r"\1", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def clip(text: str, limit: int = TEXT_LIMIT) -> str:
    """Keep a wall of ticket text from swamping the Mind, and say so when it is cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[... truncated, {len(text) - limit} more characters]"


def person(value: Any) -> str:
    """An identity field as a name."""
    if isinstance(value, dict):
        return value.get("displayName") or value.get("uniqueName") or ""
    return str(value or "")


def split_tags(value: str | None) -> list[str]:
    return [t.strip() for t in re.split(r"[;,]", value or "") if t.strip()]


def wiql_literal(text: str) -> str:
    """A WIQL string literal. Single quotes are doubled, which is how WIQL escapes them."""
    return "'" + str(text).replace("'", "''") + "'"


# --- iterations --------------------------------------------------------------------------------

def iteration_summary(raw: dict) -> dict:
    attributes = raw.get("attributes") or {}
    return {"id": raw.get("id", ""), "name": raw.get("name", ""), "path": raw.get("path", ""),
            "start_date": attributes.get("startDate"), "finish_date": attributes.get("finishDate")}


def resolve_iteration(iteration: str = "current", *, api_call: Callable[..., dict] | None = None) -> dict:
    """The team's current iteration, or the named one. Returns id/name/path/dates."""
    api_call = api_call or api
    wanted = (iteration or "current").strip()
    path = f"{team_path()}/_apis/work/teamsettings/iterations"
    if wanted.lower() in ("current", "@current", "this", "this sprint", ""):
        found = (api_call("GET", path, params={"$timeframe": "current"}) or {}).get("value") or []
        if not found:
            raise RuntimeError(
                f"{config()['team']} has no current iteration in Azure DevOps right now.")
        return iteration_summary(found[0])
    available = (api_call("GET", path) or {}).get("value") or []
    for raw in available:
        name = (raw.get("name") or "").strip().lower()
        tail = (raw.get("path") or "").replace("/", "\\").split("\\")[-1].strip().lower()
        if wanted.lower() in (name, tail):
            return iteration_summary(raw)
    names = ", ".join(r.get("name", "") for r in available) or "none"
    raise ValueError(f"{config()['team']} has no iteration called {wanted!r}. Known iterations: {names}")


# --- queries -----------------------------------------------------------------------------------

def run_wiql(query: str, *, top: int | None = None, api_call: Callable[..., dict] | None = None) -> list[int]:
    """Run a WIQL query and return the matching work item ids, in the query's own order."""
    api_call = api_call or api
    params = {"$top": int(top)} if top else None
    payload = api_call("POST", f"{project_path()}/_apis/wit/wiql", params=params, body={"query": query})
    return [int(w["id"]) for w in (payload.get("workItems") or []) if w.get("id") is not None]


def fetch_fields(ids: list[int], fields: list[str] | None = None,
                 *, api_call: Callable[..., dict] | None = None) -> list[dict]:
    """A few fields for many work items, in batches the API will accept."""
    api_call = api_call or api
    fields = fields or SUMMARY_FIELDS
    out: list[dict] = []
    for start in range(0, len(ids), BATCH_LIMIT):
        chunk = ids[start:start + BATCH_LIMIT]
        if not chunk:
            continue
        payload = api_call("POST", "/_apis/wit/workitemsbatch",
                           body={"ids": chunk, "fields": fields})
        out.extend(payload.get("value") or [])
    return out


def brief(raw: dict) -> dict:
    """The one-line shape of a work item that Guppy reads out."""
    fields = raw.get("fields") or {}
    return {
        "id": raw.get("id"),
        "type": fields.get("System.WorkItemType", ""),
        "title": (fields.get("System.Title") or "").strip(),
        "state": fields.get("System.State", ""),
        "priority": fields.get("Microsoft.VSTS.Common.Priority"),
        "tags": split_tags(fields.get("System.Tags")),
    }


def mine_in_iteration(iteration: str = "current", include_done: bool = False,
                      *, api_call: Callable[..., dict] | None = None) -> dict:
    """The Admiral's own work items in an iteration."""
    api_call = api_call or api
    sprint = resolve_iteration(iteration, api_call=api_call)
    clause = "" if include_done else \
        f" AND [System.State] NOT IN ({', '.join(wiql_literal(s) for s in DONE_STATES)})"
    query = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = {wiql_literal(config()['project'])} "
        "AND [System.AssignedTo] = @Me "
        f"AND [System.IterationPath] UNDER {wiql_literal(sprint['path'])}"
        f"{clause} "
        "ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.Id] ASC"
    )
    ids = run_wiql(query, api_call=api_call)
    items = [brief(raw) for raw in fetch_fields(ids, api_call=api_call)]
    order = {wid: n for n, wid in enumerate(ids)}
    items.sort(key=lambda i: order.get(i["id"], 0))
    return {"iteration": sprint, "include_done": bool(include_done),
            "count": len(items), "work_items": items}


def tally(items: list[dict]) -> dict:
    """Counts by state and by type."""
    by_state: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for item in items:
        by_state[item.get("state") or "(none)"] = by_state.get(item.get("state") or "(none)", 0) + 1
        by_type[item.get("type") or "(none)"] = by_type.get(item.get("type") or "(none)", 0) + 1
    return {"total": len(items),
            "by_state": dict(sorted(by_state.items())),
            "by_type": dict(sorted(by_type.items()))}


def summarize_sprint(iteration: str = "current", *, api_call: Callable[..., dict] | None = None) -> dict:
    """Where the current iteration stands: the Admiral's items, and the team's."""
    api_call = api_call or api
    sprint = resolve_iteration(iteration, api_call=api_call)
    base = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = {wiql_literal(config()['project'])} "
        f"AND [System.IterationPath] UNDER {wiql_literal(sprint['path'])}"
    )
    team_ids = run_wiql(base + " ORDER BY [System.Id] ASC", api_call=api_call)
    my_ids = set(run_wiql(base + " AND [System.AssignedTo] = @Me ORDER BY [System.Id] ASC", api_call=api_call))
    items = [brief(raw) for raw in fetch_fields(team_ids, api_call=api_call)]
    mine = [i for i in items if i["id"] in my_ids]
    return {"iteration": sprint, "team": tally(items), "mine": tally(mine)}


def find_work_items(text: str, max_results: int = 20,
                    *, api_call: Callable[..., dict] | None = None) -> dict:
    """Work items in the project whose title or description contains some text."""
    api_call = api_call or api
    needle = (text or "").strip()
    if not needle:
        raise ValueError("Nothing to search for.")
    if len(needle) > WIQL_TEXT_LIMIT:
        needle = needle[:WIQL_TEXT_LIMIT]
    limit = max(1, min(int(max_results), 100))
    literal = wiql_literal(needle)
    query = (
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = {wiql_literal(config()['project'])} "
        f"AND ([System.Title] CONTAINS {literal} OR [System.Description] CONTAINS {literal}) "
        "ORDER BY [System.ChangedDate] DESC"
    )
    ids = run_wiql(query, top=limit, api_call=api_call)
    items = [brief(raw) for raw in fetch_fields(ids, api_call=api_call)]
    order = {wid: n for n, wid in enumerate(ids)}
    items.sort(key=lambda i: order.get(i["id"], 0))
    return {"query": needle, "count": len(items), "work_items": items}


# --- one work item -----------------------------------------------------------------------------

def parent_of(raw: dict) -> int | None:
    for relation in raw.get("relations") or []:
        if relation.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
            tail = (relation.get("url") or "").rstrip("/").split("/")[-1]
            if tail.isdigit():
                return int(tail)
    return None


def fetch_comments(work_item_id: int, top: int = 10,
                   *, api_call: Callable[..., dict] | None = None) -> list[dict]:
    """The latest discussion comments, oldest of that batch first so they read in order."""
    api_call = api_call or api
    payload = api_call("GET", f"{project_path()}/_apis/wit/workItems/{int(work_item_id)}/comments",
                       params={"$top": int(top), "order": "desc"},
                       api_version=preview_version("4"))
    comments = [
        {"id": c.get("id"), "by": person(c.get("createdBy")), "at": c.get("createdDate", ""),
         "text": clip(strip_html(c.get("text") or ""), 2_000)}
        for c in (payload.get("comments") or [])
    ]
    return list(reversed(comments))


def work_item_detail(work_item_id: int, *, api_call: Callable[..., dict] | None = None) -> dict:
    """One work item in full: the fields that matter, plus the latest comments, as plain text."""
    api_call = api_call or api
    wid = coerce_id(work_item_id)
    raw = api_call("GET", f"{project_path()}/_apis/wit/workitems/{wid}", params={"$expand": "all"})
    fields = raw.get("fields") or {}
    detail = {
        **brief({"id": raw.get("id", wid), "fields": fields}),
        "assigned_to": person(fields.get("System.AssignedTo")),
        "created_by": person(fields.get("System.CreatedBy")),
        "created_date": fields.get("System.CreatedDate", ""),
        "changed_date": fields.get("System.ChangedDate", ""),
        "iteration_path": fields.get("System.IterationPath", ""),
        "area_path": fields.get("System.AreaPath", ""),
        "reason": fields.get("System.Reason", ""),
        "parent_id": parent_of(raw),
        "description": clip(strip_html(fields.get("System.Description") or "")),
        "acceptance_criteria": clip(strip_html(fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or "")),
        "repro_steps": clip(strip_html(fields.get("Microsoft.VSTS.TCM.ReproSteps") or "")),
        "url": f"{org_url()}/{config()['project']}/_workitems/edit/{wid}",
    }
    try:
        detail["comments"] = fetch_comments(wid, api_call=api_call)
    except (RuntimeError, ValueError) as e:  # a ticket is still worth reading without its discussion
        detail["comments"] = []
        detail["comments_error"] = str(e)
    return detail


def coerce_id(value: Any) -> int:
    try:
        wid = int(str(value).strip().lstrip("#"))
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a work item id.") from None
    if wid <= 0:
        raise ValueError(f"{value!r} is not a work item id.")
    return wid


# --- changes -----------------------------------------------------------------------------------

def current_tags(work_item_id: int, *, api_call: Callable[..., dict] | None = None) -> list[str]:
    api_call = api_call or api
    raw = api_call("GET", f"{project_path()}/_apis/wit/workitems/{int(work_item_id)}",
                   params={"fields": "System.Tags"})
    return split_tags((raw.get("fields") or {}).get("System.Tags"))


def merge_tags(existing: list[str], add: list[str] | None, remove: list[str] | None) -> list[str]:
    """Tags are one semicolon-joined field, so a change means rewriting the whole thing."""
    dropped = {t.strip().lower() for t in (remove or []) if t.strip()}
    merged = [t for t in existing if t.lower() not in dropped]
    seen = {t.lower() for t in merged}
    for tag in add or []:
        tag = tag.strip()
        if tag and tag.lower() not in seen:
            merged.append(tag)
            seen.add(tag.lower())
    return merged


def as_list(value: Any) -> list[str]:
    """Tags arrive as a list, or as the string a voice assistant more likely produces."""
    if value is None:
        return []
    if isinstance(value, str):
        return split_tags(value)
    return [str(v).strip() for v in value if str(v).strip()]


def build_patch(state: str | None = None, assigned_to: str | None = None,
                title: str | None = None, priority: int | None = None,
                tags: list[str] | None = None) -> list[dict]:
    """The JSON-patch document for an update. Tags are passed already merged, or not at all."""
    patch: list[dict] = []
    if title is not None:
        if not str(title).strip():
            raise ValueError("A work item cannot have an empty title.")
        patch.append({"op": "add", "path": "/fields/System.Title", "value": str(title).strip()})
    if state is not None:
        if not str(state).strip():
            raise ValueError("No state given.")
        patch.append({"op": "add", "path": "/fields/System.State", "value": str(state).strip()})
    if assigned_to is not None:
        who = str(assigned_to).strip()
        if who.lower() in ("", "none", "nobody", "unassigned"):
            patch.append({"op": "remove", "path": "/fields/System.AssignedTo"})
        else:
            patch.append({"op": "add", "path": "/fields/System.AssignedTo", "value": who})
    if priority is not None:
        try:
            value = int(priority)
        except (TypeError, ValueError):
            raise ValueError(f"Priority must be a number from 1 to 4, not {priority!r}.") from None
        if not 1 <= value <= 4:
            raise ValueError(f"Priority must be between 1 and 4, not {value}.")
        patch.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": value})
    if tags is not None:
        patch.append({"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)})
    return patch


def apply_update(work_item_id: int, state: str | None = None, assigned_to: str | None = None,
                 add_tags: Any = None, remove_tags: Any = None, title: str | None = None,
                 priority: int | None = None, *, api_call: Callable[..., dict] | None = None) -> dict:
    """Change a work item. Only the arguments that were given are touched."""
    api_call = api_call or api
    wid = coerce_id(work_item_id)
    add, drop = as_list(add_tags), as_list(remove_tags)
    tags = None
    if add or drop:
        tags = merge_tags(current_tags(wid, api_call=api_call), add, drop)
    patch = build_patch(state=state, assigned_to=assigned_to, title=title, priority=priority, tags=tags)
    if not patch:
        raise ValueError("Nothing to change: give a state, assignee, title, priority, or tags.")
    raw = api_call("PATCH", f"{project_path()}/_apis/wit/workitems/{wid}", body=patch, patch=True)
    changed = [op["path"].split("/")[-1] for op in patch]
    return {**brief({"id": raw.get("id", wid), "fields": raw.get("fields") or {}}),
            "changed": changed,
            "url": f"{org_url()}/{config()['project']}/_workitems/edit/{wid}"}


def post_comment(work_item_id: int, text: str,
                 *, api_call: Callable[..., dict] | None = None) -> dict:
    """Add one comment to a work item's discussion."""
    api_call = api_call or api
    wid = coerce_id(work_item_id)
    if not (text or "").strip():
        raise ValueError("Refusing to post an empty comment.")
    raw = api_call("POST", f"{project_path()}/_apis/wit/workItems/{wid}/comments",
                   body={"text": text.strip()}, api_version=preview_version("3"))
    return {"work_item_id": wid, "comment_id": raw.get("id"),
            "by": person(raw.get("createdBy")), "at": raw.get("createdDate", ""),
            "text": text.strip(),
            "url": f"{org_url()}/{config()['project']}/_workitems/edit/{wid}"}


def normalize_type(work_item_type: str) -> str:
    key = re.sub(r"\s+", " ", str(work_item_type or "").strip().lower())
    if key not in WORK_ITEM_TYPES:
        raise ValueError(f"{work_item_type!r} is not a type this makes. Use Bug, Task, or User Story.")
    return WORK_ITEM_TYPES[key]


def build_create_patch(work_item_type: str, title: str, description: str = "",
                       parent_id: int | None = None, iteration_path: str | None = None,
                       tags: Any = None) -> list[dict]:
    """The JSON-patch document that creates a work item."""
    if not (title or "").strip():
        raise ValueError("A new work item needs a title.")
    patch: list[dict] = [{"op": "add", "path": "/fields/System.Title", "value": title.strip()}]
    body = (description or "").strip()
    if body:
        # A Bug's story is told in its repro steps; everything else uses the description field.
        field = "Microsoft.VSTS.TCM.ReproSteps" if work_item_type == "Bug" else "System.Description"
        patch.append({"op": "add", "path": f"/fields/{field}", "value": body})
    if iteration_path:
        patch.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})
    tag_list = as_list(tags)
    if tag_list:
        patch.append({"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tag_list)})
    if parent_id is not None:
        patch.append({"op": "add", "path": "/relations/-", "value": {
            "rel": "System.LinkTypes.Hierarchy-Reverse",
            "url": f"{org_url()}/_apis/wit/workItems/{coerce_id(parent_id)}"}})
    return patch


def create_item(work_item_type: str, title: str, description: str = "", parent_id: Any = None,
                iteration: str = "current", tags: Any = None,
                *, api_call: Callable[..., dict] | None = None) -> dict:
    """Create a Bug, Task, or User Story in the project."""
    api_call = api_call or api
    kind = normalize_type(work_item_type)
    wanted = (iteration or "").strip()
    iteration_path = None
    if wanted and wanted.lower() not in ("none", "backlog", "default"):
        iteration_path = resolve_iteration(wanted, api_call=api_call)["path"]
    patch = build_create_patch(kind, title, description,
                               None if parent_id in (None, "", 0) else parent_id,
                               iteration_path, tags)
    raw = api_call("POST", f"{project_path()}/_apis/wit/workitems/${seg(kind)}", body=patch, patch=True)
    wid = raw.get("id")
    return {**brief({"id": wid, "fields": raw.get("fields") or {}}),
            "iteration_path": (raw.get("fields") or {}).get("System.IterationPath", iteration_path or ""),
            "parent_id": parent_of(raw),
            "url": f"{org_url()}/{config()['project']}/_workitems/edit/{wid}" if wid else ""}


# --- tools -------------------------------------------------------------------------------------

@server.tool()
def my_work_items(iteration: str = "current", include_done: bool = False) -> dict:
    """The Admiral's own Azure DevOps work items in the team's current sprint, or a named one."""
    try:
        return mine_in_iteration(iteration, include_done)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def sprint_summary() -> dict:
    """How the current sprint stands: counts by state and type, the Admiral's items and the team's."""
    try:
        return summarize_sprint()
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def get_work_item(id: int) -> dict:
    """One work item in full: description, acceptance criteria, repro steps and the latest comments."""
    try:
        return work_item_detail(id)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def search_work_items(text: str, max_results: int = 20) -> dict:
    """Work items in the project whose title or description contains some text."""
    try:
        return find_work_items(text, max_results)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def update_work_item(id: int, state: str = "", assigned_to: str = "", add_tags: str = "",
                     remove_tags: str = "", title: str = "", priority: int = 0) -> dict:
    """Change a work item's state, assignee, tags, title, or priority. Blank arguments are left alone."""
    try:
        return apply_update(
            id,
            state=state or None,
            assigned_to=assigned_to or None,
            add_tags=add_tags or None,
            remove_tags=remove_tags or None,
            title=title or None,
            priority=priority or None,
        )
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def add_comment(id: int, text: str) -> dict:
    """Add a comment to a work item's discussion, as the Admiral."""
    try:
        return post_comment(id, text)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def create_work_item(type: str, title: str, description: str = "", parent_id: int = 0,
                     iteration: str = "current", tags: str = "") -> dict:
    """Create a Bug, Task, or User Story, by default in the team's current sprint."""
    try:
        return create_item(type, title, description, parent_id or None, iteration, tags or None)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


if __name__ == "__main__":
    print(f"devops capability ready for {org_url()}/{config()['project']}", file=sys.stderr)
    server.run()
