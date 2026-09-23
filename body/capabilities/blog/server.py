"""Blog capability: the Admiral's self-hosted blog at https://johnnycode.ai/blog.

The site is its own publisher — no Substack, no rebuild. Posts are written through the admin API
with a bearer token that lives only in the macOS Keychain (service "guppy-blog", account "guppy")
and is read at call time. It is never logged, never returned to the Mind, never written to disk.

Every post carries a hero image. `generate_image` makes one with the Admiral's Codex CLI, falling
back to the Grok CLI, and converts it for the web with macOS `sips`; `save_draft` refuses to write a
post without one. Both CLIs run headless on the Admiral's own subscriptions: no API key, no billing
here. Which generators are tried, and in what order, is config.json's `image_providers`.

Non-secret settings (base url, image size and house style) live in config.json beside this file.

Plain functions hold the logic (tested in test_blog.py); the MCP tools are thin wrappers.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer

server = MCPServer("blog")

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

KEYCHAIN_SERVICE = "guppy-blog"
KEYCHAIN_ACCOUNT = "guppy"

TIMEOUT = 60.0
SIPS_TIMEOUT = 120
HERO_STEM = "hero"  # what both CLIs are told to name the file they save
DEFAULT_IMAGE_PROVIDERS = ["codex", "grok"]          # used only if config.json says nothing
DEFAULT_IMAGE_TIMEOUTS = {"codex": 240, "grok": 180}  # seconds for one generation

# The same rules the site enforces, checked here so a mistake never costs a round trip.
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_SLUG_LENGTH = 100
MAX_TITLE_LENGTH = 200
MAX_SUMMARY_LENGTH = 500
MAX_MARKDOWN_LENGTH = 200_000
MAX_ALT_LENGTH = 300
MAX_TAGS = 12
MAX_TAG_LENGTH = 40
MAX_IMAGE_BYTES = 8 * 1024 * 1024

MARKDOWN_LIMIT = 40_000  # characters of one post's body handed back to the Mind
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}

_config_cache: dict | None = None


# --- settings ----------------------------------------------------------------------------------

def config() -> dict:
    """The non-secret settings beside this file: base_url and the image settings."""
    global _config_cache
    if _config_cache is None:
        _config_cache = json.loads(CONFIG_PATH.read_text())
    return _config_cache


def base_url() -> str:
    return str(config()["base_url"]).rstrip("/")


def image_settings() -> dict:
    return config()["image"]


def working_dir() -> Path:
    """Where finished hero images are kept, outside the repository."""
    return Path(image_settings()["working_dir"]).expanduser()


def image_providers() -> list[str]:
    """The image generators to try, in order: Codex first, Grok as the fallback."""
    configured = config().get("image_providers") or DEFAULT_IMAGE_PROVIDERS
    return [str(name).strip().lower() for name in configured if str(name).strip()]


def image_timeout(provider: str) -> int:
    """How long one generator gets before Guppy gives up on it and tries the next."""
    timeouts = config().get("image_timeouts") or {}
    return int(timeouts.get(provider, DEFAULT_IMAGE_TIMEOUTS.get(provider, 240)))


# --- the token ---------------------------------------------------------------------------------

def admin_token(runner: Callable[..., Any] | None = None) -> str:
    """The blog admin token, straight out of the Keychain. Never logged or returned to the Mind."""
    runner = runner or subprocess.run
    try:
        done = runner(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Could not reach the macOS Keychain for the blog admin token: {e}") from None
    if done.returncode != 0 or not (done.stdout or "").strip():
        raise RuntimeError(
            "The blog admin token is not in the Keychain "
            f"(service {KEYCHAIN_SERVICE!r}, account {KEYCHAIN_ACCOUNT!r}). The blog is unavailable until it is."
        )
    return done.stdout.strip()


# --- the HTTP layer ----------------------------------------------------------------------------

def _open_client(**kwargs) -> httpx.Client:
    return httpx.Client(**kwargs)


def api(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    content: bytes | None = None,
    content_type: str | None = None,
    token_reader: Callable[[], str] | None = None,
    client_factory: Callable[..., httpx.Client] | None = None,
) -> dict:
    """One authenticated admin API call. Raises RuntimeError with a spoken-language message."""
    token = (token_reader or admin_token)()
    client_factory = client_factory or _open_client
    url = f"{base_url()}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content is not None and content_type:
        headers["Content-Type"] = content_type
    try:
        with client_factory(timeout=TIMEOUT) as client:
            response = client.request(method, url, json=body, content=content, headers=headers)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach the blog at {base_url()}: {e}") from None
    if response.status_code == 401:
        raise RuntimeError(
            "The blog rejected the admin token. Check the Keychain item "
            f"(service {KEYCHAIN_SERVICE!r}, account {KEYCHAIN_ACCOUNT!r}) against the site's BLOG_ADMIN_TOKEN."
        )
    if response.status_code == 503:
        raise RuntimeError("The blog's admin API is switched off on the site (BLOG_ADMIN_TOKEN is unset).")
    if response.status_code >= 400:
        raise RuntimeError(f"The blog refused the request ({response.status_code}): {describe_error(response)}")
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(f"The blog returned something that was not JSON ({response.status_code}).") from None
    return payload if isinstance(payload, dict) else {"value": payload}


def describe_error(response: httpx.Response) -> str:
    """The site's own message. A failed publish carries a `problems` list that says what is missing."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or "no detail"
    if not isinstance(payload, dict):
        return str(payload)[:300]
    detail = payload.get("detail", payload.get("message"))
    if isinstance(detail, dict):
        message = str(detail.get("message") or "no detail")
        problems = detail.get("problems")
        if isinstance(problems, list) and problems:
            return f"{message} ({'; '.join(str(p) for p in problems)})"[:400]
        return message[:400]
    if isinstance(detail, list):  # pydantic's field-by-field complaints
        rendered = []
        for item in detail[:4]:
            if isinstance(item, dict):
                where = ".".join(str(p) for p in (item.get("loc") or [])[1:])
                rendered.append(f"{where}: {item.get('msg', '')}".strip(": "))
            else:
                rendered.append(str(item))
        return "; ".join(r for r in rendered if r)[:400] or "no detail"
    return str(detail or "no detail")[:400]


# --- what the site will accept -----------------------------------------------------------------

def check_slug(slug: str) -> str:
    """Validate a slug exactly the way the site does, before spending a round trip on it."""
    value = (slug or "").strip()
    if not value or len(value) > MAX_SLUG_LENGTH or not SLUG_RE.fullmatch(value):
        raise ValueError(
            f"{slug!r} is not a usable slug. Use lowercase letters, digits, and single hyphens, "
            f"at most {MAX_SLUG_LENGTH} characters, like 'why-agents-need-guardrails'."
        )
    return value


def suggest_slug(title: str) -> str:
    """A title turned into the slug the site would accept. Handy when the Admiral names a post aloud."""
    value = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    value = re.sub(r"-{2,}", "-", value)[:MAX_SLUG_LENGTH].strip("-")
    if not value:
        raise ValueError(f"{title!r} has nothing in it that can become a slug.")
    return value


def as_tags(value: Any) -> list[str]:
    """Tags arrive as a list, or as the comma-separated string a voice assistant more likely produces."""
    if value is None:
        raw: list[str] = []
    elif isinstance(value, str):
        raw = re.split(r"[,;]", value)
    else:
        raw = [str(v) for v in value]
    tags: list[str] = []
    seen: set[str] = set()
    for tag in raw:
        tag = tag.strip()
        if not tag or tag.lower() in seen:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"The tag {tag!r} is longer than {MAX_TAG_LENGTH} characters.")
        tags.append(tag)
        seen.add(tag.lower())
    if len(tags) > MAX_TAGS:
        raise ValueError(f"A post takes at most {MAX_TAGS} tags, not {len(tags)}.")
    return tags


def sniff_image(data: bytes) -> str | None:
    """The image's real type, by its bytes, the way the site checks it."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def read_image(image_path: str) -> tuple[bytes, str]:
    """The hero image's bytes and content type, refusing anything the site would reject."""
    if not str(image_path or "").strip():
        raise ValueError("Every post needs a hero image. Call generate_image first and pass its image_path.")
    path = Path(str(image_path).strip()).expanduser()
    if not path.is_file():
        raise ValueError(f"There is no image at {path}.")
    data = path.read_bytes()
    if not data:
        raise ValueError(f"The image at {path} is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"The image at {path} is {len(data) / 1_048_576:.1f} MB; the blog takes at most 8 MB.")
    kind = sniff_image(data)
    if kind is None:
        raise ValueError(f"The file at {path} is not a PNG, JPEG, or WebP image.")
    return data, CONTENT_TYPES[kind]


def check_post_fields(title: str, summary: str, markdown: str, image_alt: str) -> dict:
    """Everything the site's own validation would check, checked here first."""
    title = (title or "").strip()
    if not title:
        raise ValueError("A post needs a title.")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(f"The title is {len(title)} characters; the blog takes at most {MAX_TITLE_LENGTH}.")
    summary = (summary or "").strip()
    if len(summary) > MAX_SUMMARY_LENGTH:
        raise ValueError(f"The summary is {len(summary)} characters; the blog takes at most {MAX_SUMMARY_LENGTH}.")
    body = (markdown or "").strip()
    if not body:
        raise ValueError("A post needs a Markdown body.")
    if len(body) > MAX_MARKDOWN_LENGTH:
        raise ValueError(f"The body is {len(body)} characters; the blog takes at most {MAX_MARKDOWN_LENGTH}.")
    alt = (image_alt or "").strip()
    if not alt:
        raise ValueError("The hero image needs alt text: the blog will not publish a post without it.")
    if len(alt) > MAX_ALT_LENGTH:
        raise ValueError(f"The image alt text is {len(alt)} characters; the blog takes at most {MAX_ALT_LENGTH}.")
    return {"title": title, "summary": summary, "markdown": markdown, "image_alt": alt}


# --- shapes ------------------------------------------------------------------------------------

def clip(text: str, limit: int = MARKDOWN_LIMIT) -> str:
    """Keep a long post from swamping the Mind, and say so when it is cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[... truncated, {len(text) - limit} more characters]"


def summarize(post: dict) -> dict:
    """The one-line shape of a post that Guppy reads out."""
    return {
        "slug": post.get("slug", ""),
        "title": (post.get("title") or "").strip(),
        "status": post.get("status", "draft"),
        "summary": (post.get("summary") or "").strip(),
        "tags": post.get("tags", []),
        "has_image": bool(post.get("image")),
        "image_alt": post.get("image_alt", ""),
        "created_at": post.get("created_at"),
        "updated_at": post.get("updated_at"),
        "published_at": post.get("published_at"),
        "url": post.get("url", f"{base_url()}/blog/{post.get('slug', '')}"),
    }


# --- reading -----------------------------------------------------------------------------------

def fetch_posts(*, api_call: Callable[..., dict] | None = None) -> dict:
    """Every post, drafts included, newest change first."""
    api_call = api_call or api
    payload = api_call("GET", "/api/blog/posts")
    posts = [summarize(p) for p in payload.get("posts") or []]
    return {
        "blog_url": f"{base_url()}/blog",
        "count": len(posts),
        "published": sum(1 for p in posts if p["status"] == "published"),
        "drafts": sum(1 for p in posts if p["status"] != "published"),
        "posts": posts,
    }


def fetch_post(slug: str, *, api_call: Callable[..., dict] | None = None) -> dict:
    """One post in full, including its Markdown."""
    api_call = api_call or api
    value = check_slug(slug)
    post = api_call("GET", f"/api/blog/posts/{quote(value)}")
    return {**summarize(post), "markdown": clip(post.get("markdown", "")),
            "image_url": post.get("image_url")}


# --- the hero image ----------------------------------------------------------------------------

def compose_prompt(prompt: str) -> str:
    """The Admiral's idea, plus the JohnnyCode.ai house style the site's pages are built around."""
    idea = " ".join((prompt or "").split())
    if not idea:
        raise ValueError("Describe the picture you want before generating one.")
    style = str(image_settings().get("style") or "").strip()
    if not style:
        return idea
    if not idea.endswith((".", "!", "?")):
        idea += "."
    return f"{idea} {style}"


def _run(command: list[str], runner: Callable[..., Any] | None, timeout: int, what: str,
         *, cwd: Path | None = None, stdin: Any = None):
    """Run one local command, turning every failure into a message that can be said out loud."""
    runner = runner or subprocess.run
    extra: dict[str, Any] = {}
    if cwd is not None:
        extra["cwd"] = str(cwd)
    if stdin is not None:
        extra["stdin"] = stdin
    try:
        done = runner(command, capture_output=True, text=True, timeout=timeout, **extra)
    except FileNotFoundError:
        raise RuntimeError(f"{command[0]} is not installed, so {what} is unavailable.") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} took longer than {timeout} seconds and was given up on.") from None
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Could not run {command[0]} for {what}: {e}") from None
    if done.returncode != 0:
        detail = ((done.stderr or "") + (done.stdout or "")).strip().splitlines()
        raise RuntimeError(f"{what} failed: {detail[-1][:300] if detail else 'no detail'}")
    return done


# --- the generators ----------------------------------------------------------------------------

def hero_instruction(prompt: str, *, saved_as: str) -> str:
    """What either CLI is told to do: make the picture, save it beside itself, say where it is."""
    idea = " ".join((prompt or "").split()).rstrip(".")
    return (
        "Use your image generation tool to create a wide 16:9 editorial blog hero image: "
        f"{idea}. No text, no logos. Save the generated image into the current directory as "
        f"{saved_as}, then reply with just the absolute path."
    )


def codex_instruction(prompt: str) -> str:
    return hero_instruction(prompt, saved_as=f"{HERO_STEM}.png")


def grok_instruction(prompt: str) -> str:
    return hero_instruction(
        prompt,
        saved_as=(f"{HERO_STEM} with the matching file extension "
                  f"(for example {HERO_STEM}.png or {HERO_STEM}.jpg)"),
    )


def run_codex(prompt: str, workdir: Path, runner: Callable[..., Any] | None = None) -> None:
    """The first choice: the Admiral's Codex CLI, headless, working inside workdir."""
    _run(
        ["codex", "exec", "--skip-git-repo-check", "--json",
         "-c", "model_reasoning_effort=low", "-C", str(workdir), codex_instruction(prompt)],
        runner, image_timeout("codex"), "generating a hero image with Codex",
        stdin=subprocess.DEVNULL,  # codex sits waiting on stdin unless it is closed
    )


def run_grok(prompt: str, workdir: Path, runner: Callable[..., Any] | None = None) -> None:
    """The fallback: the Grok CLI, which saves into whatever directory it is run from."""
    _run(
        ["grok", "-p", grok_instruction(prompt), "--always-approve",
         "--output-format", "streaming-json"],
        runner, image_timeout("grok"), "generating a hero image with Grok",
        cwd=workdir, stdin=subprocess.DEVNULL,
    )


GENERATORS: dict[str, Callable[..., None]] = {"codex": run_codex, "grok": run_grok}


def _first_bytes(path: Path, count: int = 16) -> bytes:
    with path.open("rb") as handle:
        return handle.read(count)


def generated_file(workdir: Path) -> Path:
    """The picture a CLI just saved: hero.png for choice, any real image it left otherwise."""
    wanted = [workdir / f"{HERO_STEM}{suffix}" for suffix in (".png", ".jpg", ".jpeg", ".webp")]
    strays = sorted(
        path for path in (workdir.iterdir() if workdir.is_dir() else [])
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and path not in wanted
    )
    for candidate in wanted + strays:
        if candidate.is_file() and sniff_image(_first_bytes(candidate)) is not None:
            return candidate
    raise RuntimeError(f"it saved no usable image in {workdir}")


def generate_with(provider: str, prompt: str, workdir: Path,
                  runner: Callable[..., Any] | None = None) -> Path:
    """Run one generator in a workdir of its own and hand back the image it saved."""
    generate = GENERATORS.get(provider)
    if generate is None:
        raise RuntimeError(
            f"{provider!r} is not an image generator Guppy knows; config.json's image_providers "
            f"takes {' and '.join(sorted(GENERATORS))}."
        )
    workdir.mkdir(parents=True, exist_ok=True)
    generate(prompt, workdir, runner)
    return generated_file(workdir)


# --- making it web-ready -----------------------------------------------------------------------

def image_size(path: Path, runner: Callable[..., Any] | None = None) -> tuple[int, int]:
    """The pixel size of an image, as sips reports it."""
    done = _run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                runner, SIPS_TIMEOUT, "measuring the image")
    found = dict(re.findall(r"(pixelWidth|pixelHeight):\s*(\d+)", done.stdout or ""))
    if "pixelWidth" not in found or "pixelHeight" not in found:
        raise RuntimeError(f"Could not work out the size of {path.name}.")
    return int(found["pixelWidth"]), int(found["pixelHeight"])


def crop_box(width: int, height: int, ratio_width: int, ratio_height: int) -> tuple[int, int]:
    """The largest centred rectangle of the wanted ratio that fits inside the picture."""
    if width <= 0 or height <= 0:
        raise RuntimeError(f"The image has no usable size ({width}x{height}).")
    if width * ratio_height > height * ratio_width:   # too wide: keep the full height
        return max(1, round(height * ratio_width / ratio_height)), height
    return width, max(1, round(width * ratio_height / ratio_width))


def convert_for_web(source: Path, destination: Path,
                    runner: Callable[..., Any] | None = None) -> Path:
    """Resize to the configured JPEG, cropping to 16:9 first only when it is not already 16:9."""
    settings = image_settings()
    width, height = int(settings["width"]), int(settings["height"])
    quality = int(settings["quality"])
    source_width, source_height = image_size(source, runner)
    crop_width, crop_height = crop_box(source_width, source_height, width, height)
    destination.parent.mkdir(parents=True, exist_ok=True)
    needs_crop = (crop_width, crop_height) != (source_width, source_height)
    cropped = destination.with_name(destination.stem + "-crop.png") if needs_crop else None
    try:
        resize_from = source
        if cropped is not None:
            # sips takes a crop as height then width, and crops from the centre.
            _run(["sips", "-c", str(crop_height), str(crop_width), str(source), "--out", str(cropped)],
                 runner, SIPS_TIMEOUT, "cropping the image to 16 by 9")
            resize_from = cropped
        _run(["sips", "-z", str(height), str(width), "-s", "format", "jpeg",
              "-s", "formatOptions", str(quality), str(resize_from), "--out", str(destination)],
             runner, SIPS_TIMEOUT, "resizing the image for the web")
    finally:
        if cropped is not None:
            cropped.unlink(missing_ok=True)
    if not destination.is_file():
        raise RuntimeError(f"sips did not produce {destination.name}.")
    return destination


def image_filename(prompt: str, now: Callable[[], datetime] = datetime.now) -> str:
    """A working name that says when the picture was made and which prompt made it."""
    stamp = now().strftime("%Y%m%d-%H%M%S")
    digest = sha256((prompt or "").encode("utf-8")).hexdigest()[:8]
    return f"hero-{stamp}-{digest}.jpg"


def make_image(prompt: str, *, runner: Callable[..., Any] | None = None,
               now: Callable[[], datetime] = datetime.now) -> dict:
    """Generate one hero image with the first generator that manages it, web-ready on disk."""
    full_prompt = compose_prompt(prompt)
    providers = image_providers()
    if not providers:
        raise RuntimeError("No image generators are configured; config.json's image_providers is empty.")
    directory = working_dir()
    raw_root = directory / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    troubles: list[str] = []
    for provider in providers:
        # A fresh workdir per generation, so one CLI never picks up another's leftovers.
        workdir = Path(tempfile.mkdtemp(prefix=f"{provider}-", dir=str(raw_root)))
        try:
            original = generate_with(provider, full_prompt, workdir, runner)
        except (RuntimeError, ValueError) as e:
            troubles.append(f"{provider}: {e}")
            continue
        destination = convert_for_web(original, directory / image_filename(full_prompt, now), runner)
        settings = image_settings()
        note = f"Made with the {provider} CLI. Pass this image_path to save_draft."
        if troubles:
            note += " It was the fallback: " + "; ".join(troubles) + "."
        return {
            "image_path": str(destination),
            "provider": provider,
            "width": int(settings["width"]),
            "height": int(settings["height"]),
            "bytes": destination.stat().st_size,
            "prompt": full_prompt,
            "original": str(original),
            "fell_back_from": [t.split(":", 1)[0] for t in troubles],
            "note": note,
        }
    raise RuntimeError("No image generator managed a hero image — " + "; ".join(troubles))


# --- writing -----------------------------------------------------------------------------------

def write_draft(slug: str, title: str, summary: str, markdown: str, image_path: str,
                image_alt: str, tags: Any = None,
                *, api_call: Callable[..., dict] | None = None) -> dict:
    """Create or update a post as a draft, with its hero image. Nothing here goes public."""
    api_call = api_call or api
    value = check_slug(slug)
    fields = check_post_fields(title, summary, markdown, image_alt)
    tag_list = as_tags(tags)
    data, content_type = read_image(image_path)  # fail before writing anything if the image is bad
    written = api_call("PUT", f"/api/blog/posts/{quote(value)}", body={
        **fields, "tags": tag_list,
        # A draft is a draft: editing a published post through this tool returns it to draft.
        "keep_published": False,
    })
    # The site stamps created_at and updated_at with the same moment when it makes a post.
    created = bool(written.get("created_at")) and written.get("created_at") == written.get("updated_at")
    with_image = api_call("PUT", f"/api/blog/posts/{quote(value)}/image",
                          content=data, content_type=content_type)
    final = with_image or written
    return {**summarize(final), "created": created, "image_url": final.get("image_url"),
            "image_bytes": len(data),
            "note": f"Saved as a draft. Nothing is public until publish_post('{value}')."}


def publish(slug: str, *, api_call: Callable[..., dict] | None = None) -> dict:
    """Publish a draft. The site refuses a post with no image or no alt text."""
    api_call = api_call or api
    value = check_slug(slug)
    post = api_call("POST", f"/api/blog/posts/{quote(value)}/publish")
    return {**summarize(post), "public_url": post.get("url") or f"{base_url()}/blog/{value}"}


def unpublish(slug: str, *, api_call: Callable[..., dict] | None = None) -> dict:
    """Take a post back off the site. It becomes a draft again; nothing is deleted."""
    api_call = api_call or api
    value = check_slug(slug)
    post = api_call("POST", f"/api/blog/posts/{quote(value)}/unpublish")
    return {**summarize(post), "note": "The post is a draft again and returns 404 to the public."}


# --- tools -------------------------------------------------------------------------------------

@server.tool()
def list_posts() -> dict:
    """Every post on the Admiral's blog, drafts included: slug, title, status and dates."""
    try:
        return fetch_posts()
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def get_post(slug: str) -> dict:
    """One blog post in full, including its Markdown body, by slug."""
    try:
        return fetch_post(slug)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def generate_image(prompt: str) -> dict:
    """Make a hero image for a post and return its local path (1600x900 JPEG, JohnnyCode.ai style).

    Drawn by the Admiral's Codex CLI, falling back to Grok; the result says which one made it.
    One generation takes a minute or two, so make one per post unless the Admiral asks for another.
    """
    try:
        return make_image(prompt)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def save_draft(slug: str, title: str, summary: str, markdown: str, image_path: str,
               image_alt: str, tags: str = "") -> dict:
    """Create or update a blog post as a private draft, with its hero image.

    image_path (from generate_image) and image_alt are both required: every post carries an image.
    """
    try:
        return write_draft(slug, title, summary, markdown, image_path, image_alt, tags)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def publish_post(slug: str) -> dict:
    """Publish a draft to https://johnnycode.ai/blog and return its public URL."""
    try:
        return publish(slug)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@server.tool()
def unpublish_post(slug: str) -> dict:
    """Take a published post back off the blog, returning it to a draft. Nothing is deleted."""
    try:
        return unpublish(slug)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


if __name__ == "__main__":
    print(f"blog capability ready for {base_url()}/blog", file=sys.stderr)
    server.run()
