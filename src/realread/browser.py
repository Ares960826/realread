"""Launch / attach to the dedicated Chrome profile over CDP (bound to 127.0.0.1)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright

from .config import Config


def cdp_url(cfg: Config) -> str:
    return f"http://127.0.0.1:{cfg.cdp_port}"


def chrome_running(cfg: Config) -> bool:
    try:
        with urllib.request.urlopen(cdp_url(cfg) + "/json/version", timeout=2):
            return True
    except OSError:
        return False


def chrome_args(cfg: Config, start_url: str | None = None) -> list[str]:
    args = [
        cfg.chrome_path,
        f"--user-data-dir={cfg.profile}",
        f"--remote-debugging-port={cfg.cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        # Many institutional sign-ins live in session-only cookies, which Chrome drops on quit.
        # Restoring the last session keeps them, so a restart does not log the user out.
        "--restore-last-session",
    ]
    # A network proxy for this Chrome only; the user's normal browser is not affected.
    if cfg.browser_proxy_pac:
        args.append(f"--proxy-pac-url={cfg.browser_proxy_pac}")
    elif cfg.browser_proxy:
        args.append(f"--proxy-server={cfg.browser_proxy}")
    if start_url:
        args.append(start_url)
    return args


def ensure_chrome(cfg: Config, start_url: str | None = None) -> None:
    """Start Chrome on the dedicated profile if it is not already listening."""
    if chrome_running(cfg):
        return
    cfg.profile.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.profile, 0o700)
    args = chrome_args(cfg, start_url)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if chrome_running(cfg):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Chrome did not open its debugging port {cfg.cdp_port}")


def open_tab(cfg: Config, url: str) -> None:
    """Show `url` to the user in the dedicated Chrome."""
    if not chrome_running(cfg):
        ensure_chrome(cfg, start_url=url)
        return
    req = urllib.request.Request(f"{cdp_url(cfg)}/json/new?{urllib.parse.quote(url, safe='')}", method="PUT")
    urllib.request.urlopen(req, timeout=10).close()


def has_window(cfg: Config) -> bool:
    """Whether Chrome shows at least one tab (on macOS it keeps running after its last window is closed)."""
    with urllib.request.urlopen(cdp_url(cfg) + "/json/list", timeout=5) as r:
        return any(t.get("type") == "page" for t in json.load(r))


def _in_use_dir(cfg: Config) -> Path:
    return cfg.library / ".realread-in-use"


def fetches_running(cfg: Config) -> list[int]:
    """Processes fetching through the dedicated Chrome right now; leftovers of dead ones are removed."""
    running = []
    folder = _in_use_dir(cfg)
    for marker in folder.glob("*") if folder.exists() else ():
        pid = int(marker.name.split("-", 1)[0])
        try:
            os.kill(pid, 0)
            running.append(pid)
        except ProcessLookupError:
            marker.unlink(missing_ok=True)
        except PermissionError:
            running.append(pid)
    return running


async def _quit_once(cfg: Config) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_url(cfg))
        session = await browser.new_browser_cdp_session()
        try:
            await asyncio.wait_for(session.send("Browser.close"), 10)
        except Exception:
            pass  # the connection ends with the browser


async def close_chrome(cfg: Config, settle_s: float = 8.0, attempts: int = 3) -> bool:
    """Quit the dedicated Chrome. True when it is gone.

    Chrome may start again by itself right after quitting, without a window, to apply a downloaded
    update. So the port is watched for a while, and a Chrome that comes back is quit again.
    """
    for _ in range(attempts):
        if chrome_running(cfg):
            await _quit_once(cfg)
        end = time.monotonic() + settle_s
        came_back = False
        gone = False
        while time.monotonic() < end:
            await asyncio.sleep(0.5)
            up = chrome_running(cfg)
            came_back = came_back or (gone and up)
            gone = gone or not up
            if came_back:
                break
        if gone and not came_back:
            return True
    return not chrome_running(cfg)


@asynccontextmanager
async def attached_context(cfg: Config):
    """Yield the profile's BrowserContext. Disconnecting leaves Chrome running."""
    folder = _in_use_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    marker.touch()
    try:
        async with _attached(cfg) as context:
            yield context
    finally:
        marker.unlink(missing_ok=True)


@asynccontextmanager
async def lazy_context(cfg: Config):
    """Yield a getter for the profile's BrowserContext. Chrome is only reached once a paper needs it."""
    opened = None
    context = None
    lock = asyncio.Lock()

    async def get() -> BrowserContext:
        nonlocal opened, context
        async with lock:
            if context is None:
                opened = attached_context(cfg)
                context = await opened.__aenter__()
        return context

    getter: Callable[[], Awaitable[BrowserContext]] = get
    try:
        yield getter
    finally:
        if opened is not None:
            await opened.__aexit__(None, None, None)


@asynccontextmanager
async def _attached(cfg: Config):
    ensure_chrome(cfg)
    if not has_window(cfg):
        # Without a window there is no default context to attach to ("Browser context management is
        # not supported"), and the user would not see tabs that need them.
        open_tab(cfg, "about:blank")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_url(cfg))
        context: BrowserContext = browser.contexts[0]
        try:
            yield context
        finally:
            await browser.close()
