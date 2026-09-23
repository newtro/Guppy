"""Tests for the blog capability.

Everything is mocked: the Keychain lookup is a fake, the codex and grok CLIs and sips are fake
subprocess runners, and the HTTP layer is an httpx.MockTransport. No test touches johnnycode.ai,
really runs a generator, or opens a socket — an autouse fixture makes a forgotten mock fail instead.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

HERE = Path(__file__).resolve().parent


def _load():
    """Import this capability's server.py under a unique name (every capability has a server.py)."""
    spec = importlib.util.spec_from_file_location("guppy_capability_blog", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


blog = _load()


# --- fakes -------------------------------------------------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake png body"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake jpeg body"
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"fake webp body"


def fake_keychain(value: str = "blog_token", returncode: int = 0, raises: Exception | None = None):
    """Stand in for `security find-generic-password`."""
    def runner(*args, **kwargs):
        if raises:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=value, stderr="")
    return runner


def mock_api(routes: dict, seen: list | None = None):
    """A drop-in for server.api: looks up (METHOD, path) and records every call."""
    def call(method, path, *, body=None, content=None, content_type=None, **_):
        if seen is not None:
            seen.append({"method": method, "path": path, "body": body,
                         "content": content, "content_type": content_type})
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


# What a generator does this run: "error" exits non-zero, "missing" is a CLI that is not installed,
# "timeout" never finishes, "nothing" writes no file, "junk" writes a file that is not an image.
FAILURES = ("error", "missing", "timeout", "nothing", "junk")


def workdir_of(command: list[str], kwargs: dict) -> Path:
    """Where a generator was pointed: codex takes -C, grok is simply run in the directory."""
    if command[0] == "codex":
        return Path(command[command.index("-C") + 1])
    return Path(kwargs["cwd"])


def fake_tools(source_size=(1672, 941), seen: list | None = None, calls: list | None = None,
               image_names: dict | None = None, fails: dict | None = None):
    """One runner standing in for the codex and grok CLIs and for sips, writing plausible files."""
    names = {"codex": "hero.png", "grok": "hero.jpg", **(image_names or {})}
    fails = fails or {}
    for provider, mode in fails.items():
        assert mode in FAILURES, f"unknown failure mode {mode!r}"

    def runner(command, **kwargs):
        if seen is not None:
            seen.append(list(command))
        if calls is not None:
            calls.append({"command": list(command), **kwargs})
        if command[0] in ("codex", "grok"):
            mode = fails.get(command[0])
            if mode == "missing":
                raise FileNotFoundError(command[0])
            if mode == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 240))
            out_dir = workdir_of(command, kwargs)
            out_dir.mkdir(parents=True, exist_ok=True)
            if mode == "error":
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr=f"{command[0]}: usage limit reached")
            if mode == "junk":
                (out_dir / "hero.png").write_bytes(b"this is not an image")
            elif mode != "nothing":
                name = names[command[0]]
                (out_dir / name).write_bytes(PNG_BYTES if name.endswith(".png") else JPEG_BYTES)
            return SimpleNamespace(returncode=0, stdout=f"{out_dir}/{names[command[0]]}\n", stderr="")
        if command[0] == "sips":
            if "-g" in command:
                width, height = source_size
                return SimpleNamespace(
                    returncode=0, stderr="",
                    stdout=f"{command[-1]}\n  pixelWidth: {width}\n  pixelHeight: {height}\n")
            destination = Path(command[command.index("--out") + 1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(JPEG_BYTES if destination.suffix == ".jpg" else PNG_BYTES)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command {command}")  # pragma: no cover
    return runner


@pytest.fixture(autouse=True)
def no_real_world(monkeypatch):
    """Belt and braces: a test that forgets a mock fails instead of publishing or running a CLI."""
    def refuse_network(**kwargs):
        raise AssertionError("a test tried to open a real connection to the blog")

    def refuse_process(*args, **kwargs):
        raise AssertionError(f"a test tried to really run {args[0] if args else '?'}")

    monkeypatch.setattr(blog, "_open_client", refuse_network)
    monkeypatch.setattr(blog.subprocess, "run", refuse_process)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway working directory for hero images, in place of the one in Application Support."""
    settings = {**blog.config()["image"], "working_dir": str(tmp_path / "blog-images")}
    monkeypatch.setattr(blog, "_config_cache", {**blog.config(), "image": settings})
    return tmp_path


def override_config(monkeypatch, **changes):
    """Change a top-level setting (providers, timeouts) on top of whatever is already loaded."""
    monkeypatch.setattr(blog, "_config_cache", {**blog.config(), **changes})


POST_METADATA = {
    "slug": "why-agents-need-guardrails",
    "title": "Why agents need guardrails",
    "summary": "A short argument for effect classes.",
    "tags": ["agents", "safety"],
    "image": "why-agents-need-guardrails-0123456789abcdef.jpg",
    "image_alt": "A blue circuit path bending around an orange gate",
    "image_url": "https://johnnycode.ai/blog/media/why-agents-need-guardrails-0123456789abcdef.jpg",
    "status": "draft",
    "created_at": "2026-09-20T10:00:00+00:00",
    "updated_at": "2026-09-21T11:00:00+00:00",
    "published_at": None,
    "url": "https://johnnycode.ai/blog/why-agents-need-guardrails",
}


@pytest.fixture
def hero(tmp_path):
    """A file on disk that passes for a hero image."""
    path = tmp_path / "hero.jpg"
    path.write_bytes(JPEG_BYTES)
    return str(path)


# --- manifest ----------------------------------------------------------------------------------

def test_manifest_declares_every_exposed_tool_with_an_effect():
    manifest = json.loads((HERE / "capability.json").read_text())
    assert manifest["name"] == "blog"
    assert manifest["enabled"] is True
    exposed = {t.name for t in asyncio.run(blog.server.list_tools())}
    assert exposed == {"list_posts", "get_post", "generate_image", "save_draft",
                       "publish_post", "unpublish_post"}
    assert set(manifest["effects"]) == exposed
    assert manifest["effects"] == {
        "list_posts": "read", "get_post": "read", "generate_image": "draft",
        "save_draft": "draft", "publish_post": "act", "unpublish_post": "act",
    }


def test_manifest_taints_nothing_because_the_blog_is_guppys_own_content():
    assert json.loads((HERE / "capability.json").read_text())["taints"] == []


def test_every_tool_has_a_description_for_the_mind():
    for tool in asyncio.run(blog.server.list_tools()):
        assert (tool.description or "").strip()


def test_config_holds_the_non_secret_settings_and_no_token():
    raw = (HERE / "config.json").read_text()
    settings = json.loads(raw)
    assert settings["base_url"] == "https://johnnycode.ai"
    assert settings["image"]["width"] == 1600 and settings["image"]["height"] == 900
    assert settings["image"]["max_bytes"] <= 8 * 1024 * 1024
    assert settings["image_providers"] == ["codex", "grok"]
    assert settings["image_timeouts"] == {"codex": 240, "grok": 180}
    assert "token" not in raw.lower() and "secret" not in raw.lower()


def test_base_url_has_no_trailing_slash(monkeypatch):
    monkeypatch.setattr(blog, "_config_cache", {**blog.config(), "base_url": "https://johnnycode.ai/"})
    assert blog.base_url() == "https://johnnycode.ai"


# --- the token ---------------------------------------------------------------------------------

def test_admin_token_comes_from_the_keychain():
    assert blog.admin_token(runner=fake_keychain("secret-token\n")) == "secret-token"


def test_admin_token_asks_the_keychain_for_the_right_item():
    asked = {}

    def runner(args, **kwargs):
        asked["args"] = args
        return SimpleNamespace(returncode=0, stdout="t", stderr="")

    blog.admin_token(runner=runner)
    assert asked["args"] == ["security", "find-generic-password", "-s", "guppy-blog", "-a", "guppy", "-w"]


def test_missing_token_is_a_plain_message_not_a_crash():
    with pytest.raises(RuntimeError, match="not in the Keychain"):
        blog.admin_token(runner=fake_keychain("", returncode=44))
    with pytest.raises(RuntimeError, match="not in the Keychain"):
        blog.admin_token(runner=fake_keychain("   "))


def test_keychain_failure_is_reported_not_swallowed():
    with pytest.raises(RuntimeError, match="Could not reach the macOS Keychain"):
        blog.admin_token(runner=fake_keychain(raises=OSError("no security binary")))


def test_tools_report_a_missing_token_instead_of_raising(monkeypatch):
    monkeypatch.setattr(blog, "admin_token", lambda **_: (_ for _ in ()).throw(RuntimeError("no token here")))
    assert "no token here" in blog.list_posts()["error"]
    assert "no token here" in blog.get_post("a-post")["error"]
    assert "no token here" in blog.publish_post("a-post")["error"]
    assert "no token here" in blog.unpublish_post("a-post")["error"]


# --- the HTTP layer ----------------------------------------------------------------------------

def test_api_sends_the_bearer_token_and_never_returns_it():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"posts": []})

    result = blog.api("GET", "/api/blog/posts", token_reader=lambda: "top-secret",
                      client_factory=client_factory_for(handler))
    assert captured["auth"] == "Bearer top-secret"
    assert captured["url"] == "https://johnnycode.ai/api/blog/posts"
    assert "top-secret" not in json.dumps(result)


def test_api_puts_the_post_as_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["method"] = request.method
        return httpx.Response(201, json=POST_METADATA)

    blog.api("PUT", "/api/blog/posts/a-post", body={"title": "A post"},
             token_reader=lambda: "t", client_factory=client_factory_for(handler))
    assert seen["method"] == "PUT" and seen["body"] == {"title": "A post"}


def test_api_uploads_raw_image_bytes_with_their_content_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["type"] = request.headers.get("content-type")
        return httpx.Response(200, json=POST_METADATA)

    blog.api("PUT", "/api/blog/posts/a-post/image", content=JPEG_BYTES, content_type="image/jpeg",
             token_reader=lambda: "t", client_factory=client_factory_for(handler))
    assert seen["body"] == JPEG_BYTES and seen["type"] == "image/jpeg"


def test_api_explains_a_rejected_token_without_echoing_it():
    def handler(request):
        return httpx.Response(401, json={"detail": "A valid blog admin token is required"})

    with pytest.raises(RuntimeError) as err:
        blog.api("GET", "/api/blog/posts", token_reader=lambda: "top-secret",
                 client_factory=client_factory_for(handler))
    assert "rejected the admin token" in str(err.value)
    assert "top-secret" not in str(err.value)


def test_api_explains_an_admin_api_that_is_switched_off():
    def handler(request):
        return httpx.Response(503, json={"detail": "Blog administration is not configured"})

    with pytest.raises(RuntimeError, match="switched off"):
        blog.api("GET", "/api/blog/posts", token_reader=lambda: "t",
                 client_factory=client_factory_for(handler))


def test_api_reports_why_a_publish_was_refused():
    def handler(request):
        return httpx.Response(422, json={"detail": {"message": "Post cannot be published",
                                                    "problems": ["A hero image is required."]}})

    with pytest.raises(RuntimeError) as err:
        blog.api("POST", "/api/blog/posts/a-post/publish", token_reader=lambda: "t",
                 client_factory=client_factory_for(handler))
    assert "422" in str(err.value)
    assert "A hero image is required." in str(err.value)


def test_api_reports_a_network_failure_plainly():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RuntimeError, match="Could not reach the blog"):
        blog.api("GET", "/api/blog/posts", token_reader=lambda: "t",
                 client_factory=client_factory_for(handler))


def test_api_accepts_an_empty_body():
    def handler(request):
        return httpx.Response(204)

    assert blog.api("GET", "/api/blog/posts", token_reader=lambda: "t",
                    client_factory=client_factory_for(handler)) == {}


def test_api_says_so_when_the_answer_is_not_json():
    def handler(request):
        return httpx.Response(200, text="<html>the React app</html>")

    with pytest.raises(RuntimeError, match="not JSON"):
        blog.api("GET", "/api/blog/posts", token_reader=lambda: "t",
                 client_factory=client_factory_for(handler))


def test_describe_error_handles_every_shape_the_site_uses():
    assert blog.describe_error(httpx.Response(404, json={"detail": "Post not found"})) == "Post not found"
    assert blog.describe_error(httpx.Response(413, text="too big")) == "too big"
    assert blog.describe_error(httpx.Response(500, json={})) == "no detail"
    validation = httpx.Response(422, json={"detail": [
        {"loc": ["body", "title"], "msg": "String should have at least 1 character"}]})
    assert "title" in blog.describe_error(validation)


# --- slugs and tags ----------------------------------------------------------------------------

def test_check_slug_accepts_what_the_site_accepts():
    assert blog.check_slug("  why-agents-need-guardrails  ") == "why-agents-need-guardrails"
    assert blog.check_slug("post2026") == "post2026"


@pytest.mark.parametrize("bad", ["", "   ", "Why-Agents", "why--agents", "-leading", "trailing-",
                                 "with space", "with_underscore", "slash/es", "../escape", "a" * 101])
def test_check_slug_refuses_what_the_site_would_refuse(bad):
    with pytest.raises(ValueError, match="not a usable slug"):
        blog.check_slug(bad)


def test_suggest_slug_turns_a_spoken_title_into_one():
    assert blog.suggest_slug("Why Agents Need Guardrails!") == "why-agents-need-guardrails"
    assert blog.suggest_slug("  AI, in 2026  ") == "ai-in-2026"
    assert blog.check_slug(blog.suggest_slug("A " * 80))


def test_suggest_slug_refuses_a_title_with_nothing_in_it():
    with pytest.raises(ValueError, match="nothing in it"):
        blog.suggest_slug("!!!")


def test_as_tags_takes_a_list_or_a_spoken_string():
    assert blog.as_tags(["agents", "safety"]) == ["agents", "safety"]
    assert blog.as_tags("agents, safety; ai") == ["agents", "safety", "ai"]
    assert blog.as_tags(None) == [] and blog.as_tags("") == []
    assert blog.as_tags("agents, Agents") == ["agents"]


def test_as_tags_refuses_more_than_the_site_takes():
    with pytest.raises(ValueError, match="at most 12 tags"):
        blog.as_tags([f"tag{n}" for n in range(13)])
    with pytest.raises(ValueError, match="longer than 40"):
        blog.as_tags(["x" * 41])


# --- what makes a post -------------------------------------------------------------------------

def test_check_post_fields_trims_and_returns_what_the_site_wants():
    fields = blog.check_post_fields("  A title  ", "  A summary ", "# Body\n", " Alt text ")
    assert fields == {"title": "A title", "summary": "A summary", "markdown": "# Body\n",
                      "image_alt": "Alt text"}


@pytest.mark.parametrize("args, message", [
    (("", "s", "body", "alt"), "needs a title"),
    (("t", "s", "   ", "alt"), "needs a Markdown body"),
    (("t", "s", "body", "  "), "needs alt text"),
    (("x" * 201, "s", "body", "alt"), "at most 200"),
    (("t", "x" * 501, "body", "alt"), "at most 500"),
    (("t", "s", "body", "x" * 301), "at most 300"),
])
def test_check_post_fields_refuses_what_the_site_would_refuse(args, message):
    with pytest.raises(ValueError, match=message):
        blog.check_post_fields(*args)


# --- the image on disk -------------------------------------------------------------------------

def test_sniff_image_knows_the_three_types_the_site_takes():
    assert blog.sniff_image(PNG_BYTES) == "png"
    assert blog.sniff_image(JPEG_BYTES) == "jpg"
    assert blog.sniff_image(WEBP_BYTES) == "webp"
    assert blog.sniff_image(b"GIF89a....") is None


def test_read_image_returns_the_bytes_and_content_type(tmp_path):
    path = tmp_path / "hero.png"
    path.write_bytes(PNG_BYTES)
    assert blog.read_image(str(path)) == (PNG_BYTES, "image/png")


def test_read_image_insists_that_every_post_has_one():
    with pytest.raises(ValueError, match="Every post needs a hero image"):
        blog.read_image("")


def test_read_image_refuses_a_missing_empty_wrong_or_oversized_file(tmp_path):
    with pytest.raises(ValueError, match="no image at"):
        blog.read_image(str(tmp_path / "nope.jpg"))
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="is empty"):
        blog.read_image(str(empty))
    text = tmp_path / "notes.txt"
    text.write_bytes(b"just some words")
    with pytest.raises(ValueError, match="not a PNG, JPEG, or WebP"):
        blog.read_image(str(text))
    huge = tmp_path / "huge.jpg"
    huge.write_bytes(b"\xff\xd8\xff" + b"0" * (8 * 1024 * 1024))
    with pytest.raises(ValueError, match="at most 8 MB"):
        blog.read_image(str(huge))


# --- making the image --------------------------------------------------------------------------

def test_compose_prompt_adds_the_house_style():
    composed = blog.compose_prompt("  A lighthouse made of   circuitry  ")
    assert composed.startswith("A lighthouse made of circuitry.")
    assert "#35bdff" in composed and "#ff9a45" in composed
    assert "no text" in composed and "no logos" in composed


def test_compose_prompt_needs_an_idea():
    with pytest.raises(ValueError, match="Describe the picture"):
        blog.compose_prompt("   ")


def test_providers_and_timeouts_come_from_config():
    assert blog.image_providers() == ["codex", "grok"]
    assert blog.image_timeout("codex") == 240
    assert blog.image_timeout("grok") == 180


def test_providers_and_timeouts_fall_back_when_config_is_silent(monkeypatch):
    override_config(monkeypatch, image_providers=[], image_timeouts={})
    assert blog.image_providers() == ["codex", "grok"]
    assert blog.image_timeout("codex") == 240 and blog.image_timeout("grok") == 180


def test_codex_instruction_asks_for_a_wide_hero_saved_as_hero_png():
    instruction = blog.codex_instruction(blog.compose_prompt("A lighthouse made of circuitry"))
    assert "wide 16:9 editorial blog hero image" in instruction
    assert "A lighthouse made of circuitry" in instruction and "#35bdff" in instruction
    assert "No text, no logos." in instruction
    assert "current directory as hero.png" in instruction
    assert instruction.endswith("reply with just the absolute path.")
    assert ".." not in instruction  # the house style already ends in a full stop


def test_grok_instruction_names_the_file_and_leaves_the_extension_open():
    instruction = blog.grok_instruction(blog.compose_prompt("A lighthouse made of circuitry"))
    assert "current directory as hero with the matching file extension" in instruction
    assert "hero.png or hero.jpg" in instruction


def test_run_codex_is_headless_and_closes_stdin(workspace):
    calls: list = []
    out = workspace / "work"
    out.mkdir()
    blog.run_codex("a prompt", out, runner=fake_tools(calls=calls))
    command = calls[0]["command"]
    assert command[:4] == ["codex", "exec", "--skip-git-repo-check", "--json"]
    assert command[command.index("-c") + 1] == "model_reasoning_effort=low"
    assert command[command.index("-C") + 1] == str(out)
    assert command[-1] == blog.codex_instruction("a prompt")
    assert calls[0]["stdin"] == subprocess.DEVNULL  # or codex waits for input forever
    assert calls[0]["timeout"] == 240
    assert "cwd" not in calls[0]


def test_run_grok_runs_inside_the_workdir(workspace):
    calls: list = []
    out = workspace / "work"
    out.mkdir()
    blog.run_grok("a prompt", out, runner=fake_tools(calls=calls))
    command = calls[0]["command"]
    assert command[0] == "grok"
    assert command[command.index("-p") + 1] == blog.grok_instruction("a prompt")
    assert "--always-approve" in command
    assert command[command.index("--output-format") + 1] == "streaming-json"
    assert calls[0]["cwd"] == str(out)
    assert calls[0]["stdin"] == subprocess.DEVNULL
    assert calls[0]["timeout"] == 180


def test_a_generator_reports_a_cli_that_is_not_installed(workspace):
    with pytest.raises(RuntimeError, match="codex is not installed"):
        blog.run_codex("a prompt", workspace, runner=fake_tools(fails={"codex": "missing"}))


def test_a_generator_reports_a_failed_command(workspace):
    with pytest.raises(RuntimeError, match="usage limit reached"):
        blog.run_grok("a prompt", workspace, runner=fake_tools(fails={"grok": "error"}))


def test_a_generator_reports_a_timeout(workspace):
    with pytest.raises(RuntimeError, match="took longer than 240 seconds"):
        blog.run_codex("a prompt", workspace, runner=fake_tools(fails={"codex": "timeout"}))


def test_generated_file_prefers_hero_png(tmp_path):
    (tmp_path / "hero.png").write_bytes(PNG_BYTES)
    (tmp_path / "hero.jpg").write_bytes(JPEG_BYTES)
    assert blog.generated_file(tmp_path).name == "hero.png"


def test_generated_file_takes_whatever_image_was_saved(tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"words")
    (tmp_path / "concept.webp").write_bytes(WEBP_BYTES)
    assert blog.generated_file(tmp_path).name == "concept.webp"


def test_generated_file_refuses_a_file_that_is_not_really_an_image(tmp_path):
    (tmp_path / "hero.png").write_bytes(b"sorry, I could not make that")
    with pytest.raises(RuntimeError, match="no usable image"):
        blog.generated_file(tmp_path)


def test_generated_file_says_so_when_there_is_no_picture(tmp_path):
    with pytest.raises(RuntimeError, match="no usable image"):
        blog.generated_file(tmp_path)


def test_generate_with_refuses_a_generator_it_does_not_know(workspace):
    with pytest.raises(RuntimeError, match="not an image generator"):
        blog.generate_with("dall-e", "a prompt", workspace / "w", runner=fake_tools())


def test_image_size_reads_what_sips_reports(tmp_path):
    path = tmp_path / "x.png"
    assert blog.image_size(path, runner=fake_tools(source_size=(2048, 1024))) == (2048, 1024)


def test_image_size_reports_an_answer_it_cannot_read(tmp_path):
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="nothing useful", stderr="")

    with pytest.raises(RuntimeError, match="Could not work out the size"):
        blog.image_size(tmp_path / "x.png", runner=runner)


@pytest.mark.parametrize("size, expected", [
    ((1672, 941), (1672, 940)),     # what codex hands back: a hair too tall
    ((1280, 720), (1280, 720)),     # what grok hands back: already 16:9
    ((1600, 900), (1600, 900)),     # already the finished size
    ((4000, 1000), (1778, 1000)),   # too wide: keep the height
    ((1000, 4000), (1000, 562)),    # too tall: keep the width
])
def test_crop_box_finds_the_largest_sixteen_by_nine(size, expected):
    assert blog.crop_box(size[0], size[1], 1600, 900) == expected


def test_crop_box_refuses_a_sizeless_image():
    with pytest.raises(RuntimeError, match="no usable size"):
        blog.crop_box(0, 0, 1600, 900)


def test_convert_for_web_crops_then_resizes_and_cleans_up(tmp_path, workspace):
    seen: list = []
    source = tmp_path / "hero.png"
    source.write_bytes(PNG_BYTES)
    destination = tmp_path / "out" / "hero.jpg"
    result = blog.convert_for_web(source, destination,
                                  runner=fake_tools(source_size=(2048, 2048), seen=seen))
    assert result == destination and destination.is_file()
    crop, resize = seen[1], seen[2]
    assert crop[:4] == ["sips", "-c", "1152", "2048"]      # height then width, as sips wants it
    assert resize[:4] == ["sips", "-z", "900", "1600"]
    assert resize[resize.index("-s") + 1:resize.index("-s") + 3] == ["format", "jpeg"]
    assert "82" in resize
    assert not (destination.parent / "hero-crop.png").exists()


def test_convert_for_web_skips_the_crop_when_the_picture_is_already_sixteen_by_nine(tmp_path, workspace):
    seen: list = []
    source = tmp_path / "hero.png"
    source.write_bytes(PNG_BYTES)
    destination = tmp_path / "hero.jpg"
    blog.convert_for_web(source, destination,
                         runner=fake_tools(source_size=(1280, 720), seen=seen))
    assert [c[1] for c in seen] == ["-g", "-z"]            # measured, then resized: no crop
    assert seen[1][seen[1].index("--out") - 1] == str(source)


def test_convert_for_web_clears_the_crop_even_when_sips_fails(tmp_path, workspace):
    source = tmp_path / "in.png"
    source.write_bytes(PNG_BYTES)
    destination = tmp_path / "hero.jpg"
    calls = {"n": 0}

    def runner(command, **kwargs):
        if "-g" in command:
            return SimpleNamespace(returncode=0, stdout="pixelWidth: 2048\npixelHeight: 2048\n", stderr="")
        calls["n"] += 1
        if calls["n"] == 1:
            Path(command[command.index("--out") + 1]).write_bytes(PNG_BYTES)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="sips: cannot write")

    with pytest.raises(RuntimeError, match="cannot write"):
        blog.convert_for_web(source, destination, runner=runner)
    assert not (destination.parent / "hero-crop.png").exists()


def test_image_filename_says_when_it_was_made_and_ends_in_jpg():
    name = blog.image_filename("a prompt", now=lambda: datetime(2026, 9, 22, 14, 30, 15))
    assert name.startswith("hero-20260922-143015-") and name.endswith(".jpg")
    assert name != blog.image_filename("another prompt", now=lambda: datetime(2026, 9, 22, 14, 30, 15))


AT_HALF_TWO = lambda: datetime(2026, 9, 22, 14, 30, 15)  # noqa: E731 - a stand-in clock


def test_make_image_uses_codex_first_and_returns_a_web_ready_path(workspace):
    seen: list = []
    result = blog.make_image("A lighthouse made of circuitry", runner=fake_tools(seen=seen),
                             now=AT_HALF_TWO)
    path = Path(result["image_path"])
    assert path.is_file() and path.suffix == ".jpg"
    assert path.parent == (workspace / "blog-images")
    assert result["provider"] == "codex" and result["fell_back_from"] == []
    assert result["width"] == 1600 and result["height"] == 900
    assert result["bytes"] == len(JPEG_BYTES)
    assert "#35bdff" in result["prompt"]
    assert "codex" in result["note"]
    assert [c[0] for c in seen] == ["codex", "sips", "sips", "sips"]  # one generation, no more
    assert Path(result["original"]).name == "hero.png"


@pytest.mark.parametrize("mode", ["error", "timeout", "missing", "nothing", "junk"])
def test_make_image_falls_back_to_grok_when_codex_does_not_deliver(workspace, mode):
    seen: list = []
    result = blog.make_image("A lighthouse", runner=fake_tools(seen=seen, fails={"codex": mode}),
                             now=AT_HALF_TWO)
    assert result["provider"] == "grok" and result["fell_back_from"] == ["codex"]
    assert "fallback" in result["note"] and "codex" in result["note"]
    assert [c[0] for c in seen][:2] == ["codex", "grok"]
    assert Path(result["image_path"]).is_file()


def test_make_image_gives_each_generation_a_fresh_workdir(workspace):
    calls: list = []
    runner = fake_tools(calls=calls, fails={"codex": "nothing"})
    blog.make_image("A lighthouse", runner=runner, now=AT_HALF_TWO)
    blog.make_image("A lighthouse", runner=runner, now=AT_HALF_TWO)
    generators = [c for c in calls if c["command"][0] in ("codex", "grok")]
    directories = [str(workdir_of(c["command"], c)) for c in generators]
    assert len(set(directories)) == len(directories) == 4
    raw = workspace / "blog-images" / "raw"
    assert all(Path(d).parent == raw for d in directories)
    assert [Path(d).name.split("-")[0] for d in directories] == ["codex", "grok", "codex", "grok"]


def test_make_image_reports_when_no_generator_manages_it(workspace):
    runner = fake_tools(fails={"codex": "timeout", "grok": "error"})
    with pytest.raises(RuntimeError, match="No image generator managed") as failure:
        blog.make_image("A lighthouse", runner=runner, now=AT_HALF_TWO)
    assert "codex: " in str(failure.value) and "grok: " in str(failure.value)


def test_make_image_follows_the_order_in_the_config(workspace, monkeypatch):
    override_config(monkeypatch, image_providers=["grok"])
    seen: list = []
    result = blog.make_image("A lighthouse", runner=fake_tools(seen=seen), now=AT_HALF_TWO)
    assert result["provider"] == "grok"
    assert [c[0] for c in seen] == ["grok", "sips", "sips", "sips"]


def test_make_image_says_so_when_no_generator_is_configured(workspace, monkeypatch):
    override_config(monkeypatch, image_providers=["  "])
    with pytest.raises(RuntimeError, match="image_providers is empty"):
        blog.make_image("A lighthouse", runner=fake_tools())


def test_make_image_never_starts_a_generation_for_an_empty_prompt(workspace):
    def explode(*args, **kwargs):
        raise AssertionError("make_image must validate before it runs a generator")

    with pytest.raises(ValueError, match="Describe the picture"):
        blog.make_image("   ", runner=explode)


def test_generate_image_tool_reports_a_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr(blog, "make_image",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("both generators failed")))
    assert blog.generate_image("a lighthouse")["error"] == "both generators failed"


# --- reading the blog --------------------------------------------------------------------------

def test_fetch_posts_counts_drafts_and_published():
    published = {**POST_METADATA, "slug": "shipped", "status": "published",
                 "published_at": "2026-09-01T00:00:00+00:00"}
    seen: list = []
    call = mock_api({("GET", "/api/blog/posts"): {"posts": [POST_METADATA, published]}}, seen)
    result = blog.fetch_posts(api_call=call)
    assert seen[0]["method"] == "GET"
    assert result["count"] == 2 and result["published"] == 1 and result["drafts"] == 1
    assert result["blog_url"] == "https://johnnycode.ai/blog"
    assert result["posts"][0]["slug"] == "why-agents-need-guardrails"
    assert result["posts"][0]["has_image"] is True
    assert result["posts"][0]["url"] == "https://johnnycode.ai/blog/why-agents-need-guardrails"


def test_fetch_posts_on_an_empty_blog():
    assert blog.fetch_posts(api_call=mock_api({("GET", "/api/blog/posts"): {}}))["count"] == 0


def test_fetch_post_returns_the_markdown():
    path = "/api/blog/posts/why-agents-need-guardrails"
    full = {**POST_METADATA, "markdown": "# Guardrails\n\nEvery call has an effect class.\n"}
    result = blog.fetch_post("why-agents-need-guardrails", api_call=mock_api({("GET", path): full}))
    assert result["markdown"].startswith("# Guardrails")
    assert result["title"] == "Why agents need guardrails"
    assert result["image_url"].endswith(".jpg")


def test_fetch_post_clips_an_enormous_body():
    path = "/api/blog/posts/long-one"
    full = {**POST_METADATA, "slug": "long-one", "markdown": "x" * 50_000}
    result = blog.fetch_post("long-one", api_call=mock_api({("GET", path): full}))
    assert "10000 more characters" in result["markdown"]


def test_fetch_post_rejects_a_bad_slug_before_calling_out():
    def explode(*args, **kwargs):
        raise AssertionError("get_post must validate the slug before it touches the network")

    with pytest.raises(ValueError, match="not a usable slug"):
        blog.fetch_post("../../etc/passwd", api_call=explode)


def test_get_post_tool_reports_a_bad_slug():
    assert "not a usable slug" in blog.get_post("Not A Slug")["error"]


def test_list_posts_tool_returns_the_posts(monkeypatch):
    monkeypatch.setattr(blog, "api", mock_api({("GET", "/api/blog/posts"): {"posts": [POST_METADATA]}}))
    assert blog.list_posts()["count"] == 1


# --- saving a draft ----------------------------------------------------------------------------

def test_write_draft_writes_the_post_then_uploads_the_image(hero):
    seen: list = []
    created = {**POST_METADATA, "created_at": "2026-09-22T09:00:00+00:00",
               "updated_at": "2026-09-22T09:00:00+00:00"}
    call = mock_api({
        ("PUT", "/api/blog/posts/why-agents-need-guardrails"): created,
        ("PUT", "/api/blog/posts/why-agents-need-guardrails/image"): POST_METADATA,
    }, seen)
    result = blog.write_draft("why-agents-need-guardrails", "Why agents need guardrails",
                              "A short argument for effect classes.", "# Guardrails\n", hero,
                              "A blue circuit path bending around an orange gate",
                              tags="agents, safety", api_call=call)
    assert [c["path"] for c in seen] == ["/api/blog/posts/why-agents-need-guardrails",
                                         "/api/blog/posts/why-agents-need-guardrails/image"]
    assert seen[0]["body"]["title"] == "Why agents need guardrails"
    assert seen[0]["body"]["tags"] == ["agents", "safety"]
    assert seen[0]["body"]["image_alt"] == "A blue circuit path bending around an orange gate"
    assert seen[1]["content"] == JPEG_BYTES and seen[1]["content_type"] == "image/jpeg"
    assert result["status"] == "draft"
    assert result["created"] is True
    assert result["image_bytes"] == len(JPEG_BYTES)
    assert "Nothing is public" in result["note"]


def test_write_draft_keeps_a_published_post_from_staying_public(hero):
    seen: list = []
    call = mock_api({("PUT", "/api/blog/posts/a-post"): POST_METADATA,
                     ("PUT", "/api/blog/posts/a-post/image"): POST_METADATA}, seen)
    blog.write_draft("a-post", "T", "", "body", hero, "alt", api_call=call)
    assert seen[0]["body"]["keep_published"] is False


def test_write_draft_knows_an_edit_from_a_creation(hero):
    call = mock_api({("PUT", "/api/blog/posts/a-post"): POST_METADATA,   # created_at != updated_at
                     ("PUT", "/api/blog/posts/a-post/image"): POST_METADATA})
    assert blog.write_draft("a-post", "T", "", "body", hero, "alt", api_call=call)["created"] is False


def test_write_draft_refuses_before_it_writes_anything(hero, tmp_path):
    def explode(*args, **kwargs):
        raise AssertionError("save_draft must validate before it touches the network")

    with pytest.raises(ValueError, match="not a usable slug"):
        blog.write_draft("Bad Slug", "T", "", "body", hero, "alt", api_call=explode)
    with pytest.raises(ValueError, match="needs a title"):
        blog.write_draft("a-post", "", "", "body", hero, "alt", api_call=explode)
    with pytest.raises(ValueError, match="needs a Markdown body"):
        blog.write_draft("a-post", "T", "", "", hero, "alt", api_call=explode)
    with pytest.raises(ValueError, match="needs alt text"):
        blog.write_draft("a-post", "T", "", "body", hero, "", api_call=explode)
    with pytest.raises(ValueError, match="Every post needs a hero image"):
        blog.write_draft("a-post", "T", "", "body", "", "alt", api_call=explode)
    with pytest.raises(ValueError, match="no image at"):
        blog.write_draft("a-post", "T", "", "body", str(tmp_path / "gone.jpg"), "alt", api_call=explode)


def test_save_draft_tool_reports_a_refusal_instead_of_raising():
    assert "Every post needs a hero image" in blog.save_draft("a-post", "T", "", "body", "", "alt")["error"]


# --- publishing --------------------------------------------------------------------------------

def test_publish_returns_the_public_url():
    published = {**POST_METADATA, "status": "published", "published_at": "2026-09-22T12:00:00+00:00"}
    seen: list = []
    call = mock_api({("POST", "/api/blog/posts/why-agents-need-guardrails/publish"): published}, seen)
    result = blog.publish("why-agents-need-guardrails", api_call=call)
    assert seen[0]["method"] == "POST"
    assert result["status"] == "published"
    assert result["public_url"] == "https://johnnycode.ai/blog/why-agents-need-guardrails"


def test_publish_passes_on_the_sites_reason_for_refusing(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("The blog refused the request (422): Post cannot be published "
                           "(Hero image alt text is required.)")

    monkeypatch.setattr(blog, "api", refuse)
    assert "Hero image alt text is required." in blog.publish_post("a-post")["error"]


def test_unpublish_takes_a_post_back_to_draft():
    call = mock_api({("POST", "/api/blog/posts/a-post/unpublish"): {**POST_METADATA, "slug": "a-post"}})
    result = blog.unpublish("a-post", api_call=call)
    assert result["status"] == "draft"
    assert "draft again" in result["note"]


def test_publishing_tools_reject_a_bad_slug_before_calling_out(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("publishing must validate the slug before it touches the network")

    monkeypatch.setattr(blog, "api", explode)
    assert "not a usable slug" in blog.publish_post("Not A Slug")["error"]
    assert "not a usable slug" in blog.unpublish_post("")["error"]
