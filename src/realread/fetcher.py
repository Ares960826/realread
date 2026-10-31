"""Open articles in the logged-in Chrome profile, politely, and store their full text.

Per article:
  1. Resolve DOI -> publisher URL via the doi.org handle API (no publisher request).
  2. Wait for the policy (policy.Guard) to allow a request to that publisher.
  3. Open it in a new tab (through the library proxy if configured) and stay a few
     seconds, scrolling, as a reader would; this also loads lazy full text.
  4. On a bot check or login page: pause that publisher, notify the user, bring the tab to the
     front and wait until they have dealt with it, then open the article again if they ended up
     elsewhere. Never solve or bypass checks. Other publishers go on meanwhile, in background tabs
     so that the user's tab keeps the focus. If the user does not respond in time, that
     publisher's papers are deferred.
  5. Extract the article body from HTML, or from the PDF when HTML lacks it.
A batch interleaves publishers so that no single one is hit in a burst.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from . import config, detect, direct, library, notify, signin, sso
from .config import Config
from .extract import (
    LONG_FULLTEXT_CHARS,
    MIN_FULLTEXT_CHARS,
    PdfBotCheck,
    citation_meta,
    extract_html,
    fetch_pdf,
    issue_key,
    pdf_candidates,
    pdf_to_text,
)
from .i18n import t
from .library import Meta
from .policy import Guard, QuotaExhausted, host_key
from .proxy import not_configured_page, proxy_host, rewrite
from .sites import site_key

DWELL_S = (4.0, 9.0)  # time on the article page before extracting
PDF_DELAY_S = (2.0, 5.0)  # pause between page and PDF request
AUTO_CLEAR_S = 20  # JS challenges often clear by themselves
POLL_S = 3.0
MAX_HOLD_S = 600  # longer cool-downs defer that publisher's items instead of holding the batch
AUTO_SIGN_IN_S = 90  # how long an automatic institutional re-sign-in may take (IOP's chain takes over 25 s)
BUTTON_AFTER_LINK_S = 20  # after a direct link, how long the chain may pass through the article before clicking
REMIND_EVERY_S = 300  # how often a waiting tab reminds the user and reports where it stands
RELOAD_AFTER_S = 5  # a page that failed to load gets one more try after this pause

Emit = Callable[..., None]


@dataclass
class Control:
    cancelled: bool = False
    # Publishers the library proxy is not set up for; opened directly for the rest of the batch.
    direct_hosts: set[str] = field(default_factory=set)
    # Publishers whose institutional session was renewed in this batch: a closed article there is
    # closed because it is not licensed, not because the session ended.
    renewed_hosts: set[str] = field(default_factory=set)
    # Publishers that answered with a maintenance / outage page; their remaining items wait.
    down_hosts: set[str] = field(default_factory=set)
    # Publishers whose sign-in page said it does not know the user's institution.
    rejected_hosts: set[str] = field(default_factory=set)
    # Tabs waiting for the user, oldest first; the first one is the tab the user is shown.
    waiting: list[Page] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def resolve_url(item_id: str) -> str | None:
    """DOI -> registered landing URL (doi.org handle API); URLs pass through.

    None: not a URL or DOI, or doi.org says the DOI does not exist. If doi.org cannot be asked
    right now, the DOI address itself is opened and its redirect is followed.
    """
    if item_id.lower().startswith("http"):
        return item_id
    if not library.is_doi(item_id):
        return None
    api = f"https://doi.org/api/handles/{urllib.parse.quote(item_id)}?type=URL"
    for _ in range(2):
        try:
            with urllib.request.urlopen(api, timeout=20) as r:
                data = json.load(r)
            for v in data.get("values", []):
                if v.get("type") == "URL":
                    return v["data"]["value"]
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except (OSError, ValueError, KeyError):
            pass
    return f"https://doi.org/{item_id}"


def _not_found(item_id: str) -> Meta:
    what = "doi.org does not know this DOI" if library.is_doi(item_id) else "This is neither a DOI nor a URL"
    return Meta(id=item_id, status="not_found", source_url="", fetched_at=_now(),
                message=f"{what}; check the identifier. No page was opened, and this says nothing "
                        "about your access.")


def _is_unresolved(source: str) -> bool:
    return (urllib.parse.urlparse(source).hostname or "").endswith("doi.org")


async def _sleep(seconds: float, control: Control) -> bool:
    """Sleep in small steps; False if cancelled."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        if control.cancelled:
            return False
        await asyncio.sleep(min(left, 1.0))
    return not control.cancelled


async def _read_like_a_person(page: Page, control: Control) -> None:
    dwell = random.uniform(*DWELL_S)
    steps = random.randint(3, 6)
    for _ in range(steps):
        if not await _sleep(dwell / steps, control):
            return
        try:
            await page.mouse.wheel(0, random.randint(500, 1400))
        except Exception:
            return


async def _classify(page: Page, cfg: Config) -> str | None:
    """'bot' (bot check), 'login' (login page) or None."""
    try:
        title, html = await page.title(), await page.content()
    except Exception:
        return None
    if detect.is_check_host(page.url) or detect.is_bot_check(title, html):
        return "bot"
    host = urllib.parse.urlparse(page.url).hostname or ""
    on_publisher = site_key(host) is not None and host != proxy_host(cfg)
    return "login" if detect.is_login_page(page.url, on_publisher) else None


async def _wait_for_user(page: Page, kind: str, host: str, cfg: Config, emit: Emit, control: Control,
                         cleared: Callable[[], Awaitable[bool]]) -> bool:
    """Pause until the user has dealt with a bot check / login in the visible tab."""
    mins = max(1, cfg.max_wait_for_user_s // 60)
    emit("blocked", kind=kind, host=host, url=page.url, mins=mins)
    notify.desktop(t("notify.title"), t("notify.bot" if kind == "bot" else "notify.login", host=host))
    seen: list[str] = []

    def on_nav(frame) -> None:
        if frame == page.main_frame:
            seen.append(frame.url)

    page.on("framenavigated", on_nav)
    control.waiting.append(page)
    try:
        if control.waiting[0] is page:  # do not pull the user away from a check they are working on
            await page.bring_to_front()
    except Exception:
        pass
    try:
        started = time.monotonic()
        end = started + cfg.max_wait_for_user_s
        next_remind = started + REMIND_EVERY_S
        while time.monotonic() < end:
            if not await _sleep(POLL_S, control) or page.is_closed():
                return False
            try:
                if time.monotonic() >= next_remind:
                    next_remind += REMIND_EVERY_S
                    notify.desktop(t("notify.title"), t("notify.bot" if kind == "bot" else "notify.login", host=host))
                    emit("still_blocked", kind=kind, host=host, url=page.url, title=await page.title(),
                         waited_s=int(time.monotonic() - started))
                # Whatever the page looks like: readable article text means nothing is in the way.
                if await extract_html(page):
                    emit("unblocked", kind=kind, host=host)
                    _learn_idp(cfg, seen, emit)
                    return True
                # Twice, a poll apart: while a page navigates it can look clear for a moment.
                if await cleared() and await _sleep(POLL_S, control) and not page.is_closed() and await cleared():
                    emit("unblocked", kind=kind, host=host)
                    _learn_idp(cfg, seen, emit)
                    return True
            except Exception:
                pass  # page is navigating (SSO round trip); keep waiting
        return False
    finally:
        page.remove_listener("framenavigated", on_nav)
        control.waiting.remove(page)
        if control.waiting:
            try:
                await control.waiting[0].bring_to_front()
            except Exception:
                pass


def _learn_idp(cfg: Config, urls: list[str], emit: Emit) -> None:
    """Remember the institution the user just signed in with, for later automatic re-sign-in."""
    if cfg.idp_entity_id:
        return
    for url in urls:
        idp, fed = sso.idp_from_url(url)
        if idp:
            cfg.idp_entity_id, cfg.idp_federation = idp, fed
            try:
                config.save(cfg)
            except OSError:
                pass
            emit("idp_learned", idp=idp)
            return


async def _wait_quietly(cleared: Callable[[], Awaitable[bool]], seconds: float, control: Control) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not await _sleep(POLL_S, control):
            return False
        try:
            if await cleared():
                return True
        except Exception:
            pass
    return False


async def _auto_sign_in(page: Page, cfg: Config, guard: Guard, host: str, emit: Emit, control: Control,
                        signed_in: Callable[[], Awaitable[bool]]) -> bool | None:
    """Re-sign in through the user's own institution, without a password, if its SSO session is alive.

    True: the article opened. False: it did not (closed, or the institution wants the user).
    None: not attempted (off, nothing to open or click on this site, attempts spaced, or the
    university's own session has ended, so only the user can sign in).
    """
    if not cfg.auto_sign_in:
        return None
    link = sso.wayfless_url(page.url, cfg.idp_entity_id, cfg.idp_federation) if cfg.idp_entity_id else None
    if not link and not (cfg.institution or cfg.idp_entity_id) or not link and not await signin.login_button(page):
        return None
    until = await asyncio.to_thread(sso.university_session_until, cfg)
    if until and until < datetime.now(UTC):
        emit("university_session_ended", until=until.timestamp())
        return None
    if not guard.auto_sign_in_allowed(host):
        emit("auto_sign_in_skipped", host=host, resets_at=guard.auto_sign_in_resets_at(host))
        return None
    guard.record_auto_sign_in(host)
    emit("auto_sign_in", host=host)
    article_url = page.url

    async def rejected() -> bool:
        return detect.institution_rejected(await page.inner_text("body"))

    async def settled() -> bool:
        return await signed_in() or await rejected()

    for again in (True, False):
        if link:
            try:
                await page.goto(link, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
        if await signin.walk(page, settled, cfg.institution, AUTO_SIGN_IN_S, POLL_S, lambda s: _sleep(s, control),
                             BUTTON_AFTER_LINK_S if link else 0.0):
            if await rejected():
                control.rejected_hosts.add(host_key(host))
                return False
            return True
        # Some publishers finish the sign-in on their home page instead of the article (ACS). Back on
        # the same publisher, not on a login or check page: open the article again, once.
        here = urllib.parse.urlparse(page.url).hostname or ""
        if site_key(here) and site_key(here) == site_key(urllib.parse.urlparse(article_url).hostname or "") \
                and not same_article(page.url, article_url) and await _classify(page, cfg) is None:
            await _goto(page, article_url)
            if await _wait_quietly(signed_in, AUTO_CLEAR_S, control):
                return True
        if same_article(page.url, article_url) or page.is_closed() or await signin.needs_person(page):
            return False
        # A sign-in that broke on the way (IEEE now and then says the university cannot log in): as a
        # person would, go back to the article and sign in once more.
        if again:
            emit("auto_sign_in_again", host=host)
            await _goto(page, article_url)
            if await _wait_quietly(signed_in, POLL_S * 2, control):
                return True
    return False


def _attempts_used_up(page: Page, cfg: Config, guard: Guard, host: str) -> bool:
    return bool(cfg.auto_sign_in and cfg.idp_entity_id
                and sso.wayfless_url(page.url, cfg.idp_entity_id, cfg.idp_federation)
                and not guard.auto_sign_in_allowed(host))


async def _settled_read(page: Page, control: Control, read: Callable[[], Awaitable]):
    """Read the page, again if it redirected itself meanwhile (IEEE and ACS pages do)."""
    try:
        return await read()
    except PlaywrightError as e:
        if "context was destroyed" not in str(e):
            raise
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PlaywrightError:
        pass
    await _sleep(POLL_S, control)
    return await read()


def _failed_to_load(meta: Meta, host: str) -> Meta:
    meta.status = "deferred"
    meta.message = (f"{host} did not load as a page or a PDF (network or site error); not fetched now. Retry later "
                    "(this says nothing about your access).")
    return meta


async def _read_download(meta: Meta, page: Page, context: BrowserContext, cfg: Config, url: str, host: str) -> Meta:
    """The address starts a download instead of showing a page (direct PDF links on some sites)."""
    data = await fetch_pdf(page, context, url)
    if not data:  # a blank tab can also mean the page failed to load
        return _failed_to_load(meta, host)
    text = pdf_to_text(data)
    meta.status, meta.method, meta.chars = ("ok" if len(text) >= MIN_FULLTEXT_CHARS else "partial"), "pdf", len(text)
    meta.final_url, meta.fetched_at = url, _now()
    if meta.status == "partial":
        meta.message = "A short PDF: possibly not the full text. The text is stored; check it before relying on it."
    library.save(cfg, meta, text=text, pdf=data)
    return meta


async def _extract(page: Page, context: BrowserContext, control: Control) -> tuple[str, str, str, bytes | None, dict]:
    """Return (status, method, text, pdf_bytes, citation_meta)."""
    cmeta = await citation_meta(page)
    html_text = await extract_html(page)
    if len(html_text) >= LONG_FULLTEXT_CHARS:
        return "ok", "html", html_text, None, cmeta
    # Short or missing HTML: some articles carry only abstract + back matter in HTML, with
    # the body in the PDF. Try the PDF and keep whichever is more complete.
    blocked = ""
    for url in await pdf_candidates(page, cmeta):
        if not await _sleep(random.uniform(*PDF_DELAY_S), control):
            break
        try:
            data = await fetch_pdf(page, context, url)
        except PdfBotCheck:
            blocked = blocked or url
            continue
        if data:
            pdf_text = pdf_to_text(data)
            if len(pdf_text) >= MIN_FULLTEXT_CHARS and len(pdf_text) > 1.2 * len(html_text):
                cmeta["_pdf_url"] = url
                return "ok", "pdf", pdf_text, data, cmeta
            blocked = ""  # a real PDF came back, just not a better one
            break
    if blocked:  # the PDF exists but a bot check stands in front of it: not a missing licence
        cmeta["_pdf_url"] = blocked
        return "pdf_bot", "", html_text, None, cmeta
    if html_text:
        return "partial", "html", html_text, None, cmeta
    return "none", "", "", None, cmeta


async def _clear_pdf_check(page: Page, context: BrowserContext, cfg: Config, guard: Guard, host: str, emit: Emit,
                           control: Control, html_text: str,
                           cmeta: dict) -> tuple[str, str, str, bytes | None, dict] | None:
    """Open the PDF in the tab, as a reader would, and let the browser or the user clear its bot check.

    The PDF is taken from that navigation: scripted requests may keep meeting the check after
    the tab has passed it. Its body is read at the response stage (CDP Fetch), because Chrome
    hands PDF navigations to its viewer and the network log keeps only a truncated body. If the
    tab ends up on an HTML page instead (publishers redirect to the abstract without a licence),
    there is no PDF access. None: the check was not cleared.
    """
    article_url, pdf_url = page.url, cmeta["_pdf_url"]
    captured: list[bytes] = []
    cdp = await context.new_cdp_session(page)

    async def on_paused(event: dict) -> None:
        try:
            headers = {h["name"].lower(): h["value"] for h in event.get("responseHeaders") or []}
            if "pdf" in headers.get("content-type", ""):
                body = await cdp.send("Fetch.getResponseBody", {"requestId": event["requestId"]})
                captured.append(base64.b64decode(body["body"]) if body["base64Encoded"] else body["body"].encode())
        except Exception:
            pass
        finally:
            try:
                await cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
            except Exception:
                pass

    async def page_clear() -> bool:
        return bool(captured) or await _classify(page, cfg) is None

    cdp.on("Fetch.requestPaused", lambda event: asyncio.ensure_future(on_paused(event)))
    await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document",
                                                  "requestStage": "Response"}]})
    try:
        await _goto(page, pdf_url)
        if not await _wait_quietly(page_clear, AUTO_CLEAR_S, control):
            answered = await _wait_for_user(page, "bot", host, cfg, emit, control, page_clear)
            _record_check(guard, host, answered, page, control)
            if not answered:
                return None
        await _sleep(POLL_S, control)  # let a PDF response after the check arrive
    finally:
        try:
            await cdp.send("Fetch.disable")
            await cdp.detach()
        except Exception:
            pass
    data = next((d for d in captured if d.startswith(b"%PDF")), None)
    if data:
        pdf_text = pdf_to_text(data)
        if len(pdf_text) >= MIN_FULLTEXT_CHARS and len(pdf_text) > 1.2 * len(html_text):
            cmeta["_pdf_url"] = pdf_url
            return "ok", "pdf", pdf_text, data, cmeta
    await _goto(page, article_url)  # back to the article for the sign-in check; not needed after a PDF
    cmeta.pop("_pdf_url", None)
    return ("partial", "html", html_text, None, cmeta) if html_text else ("none", "", "", None, cmeta)


async def _extract_clearing_pdf(page: Page, context: BrowserContext, cfg: Config, guard: Guard, host: str,
                                emit: Emit, control: Control) -> tuple[str, str, str, bytes | None, dict] | None:
    """_extract, letting a bot check in front of the PDF be cleared; None if it stays."""
    status, method, text, pdf, cmeta = await _extract(page, context, control)
    if status != "pdf_bot":
        return status, method, text, pdf, cmeta
    return await _clear_pdf_check(page, context, cfg, guard, host, emit, control, text, cmeta)


def _record_check(guard: Guard, host: str, answered: bool, page: Page, control: Control) -> None:
    """A check that cleared within seconds by itself is never recorded; nothing was asked of anyone.
    A cancelled batch says nothing about the site, and closing the tab is the user's own answer."""
    if answered or page.is_closed():
        guard.record_block(host, "answered")
    elif not control.cancelled:
        guard.record_block(host, "unanswered")


def _retry_after_s(resp) -> float:
    try:
        return float(resp.headers.get("retry-after", ""))
    except (ValueError, AttributeError):
        return 0.0


def _pdf_check_pending(meta: Meta, page: Page) -> Meta:
    meta.status, meta.final_url = "needs_user", page.url
    meta.message = "The PDF is behind a bot check: clear it in the RealRead Chrome window, then retry."
    return meta


def same_article(url: str, other: str) -> bool:
    """Same article, also across a publisher's redirects (linkinghub /retrieve/pii/X -> /science/article/pii/X,
    Atypon /doi/X -> /doi/full/X): the last path segment names the article."""
    def last(u: str) -> str:
        return urllib.parse.unquote(urllib.parse.urlparse(u).path).rstrip("/").rsplit("/", 1)[-1].lower()
    return bool(last(url)) and last(url) == last(other)


async def _down(page: Page, resp) -> bool:
    try:
        title, body = await page.title(), await page.inner_text("body")
    except Exception:
        return False
    return detect.is_unavailable(title, body, resp.status if resp is not None else None)


def _unavailable(meta: Meta, host: str) -> Meta:
    meta.status = "deferred"
    meta.message = (f"{host} is down or in maintenance; not fetched now. Retry later (this says nothing "
                    "about your access).")
    return meta


async def _goto(page: Page, url: str):
    resp = None
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass  # networkidle often never happens on publisher pages
    return resp


def _still_blocked(meta: Meta, page: Page, kind: str, host: str) -> Meta:
    """The user did not finish in time. Recorded in the past tense: this outlives the batch."""
    meta.status, meta.final_url = ("needs_user" if kind == "bot" else "needs_login"), page.url
    at = datetime.now().strftime("%Y-%m-%d %H:%M")
    if page.is_closed():
        meta.message = f"At {at} the tab for {host} was closed before the paper was read. Retry to open it again."
    elif detect.is_home_page(page.url):
        meta.message = (f"At {at} the tab ended on the {host} home page instead of the article, so RealRead could not "
                        "read it. This says nothing about your access. Retry to open the article again.")
    elif kind == "bot":
        meta.message = (f"At {at} a bot check on {host} (not a sign-in) was not cleared in time. "
                        "Clear it in the RealRead Chrome window and retry.")
    else:
        meta.message = (f"At {at} a sign-in on {host} was not completed in time. Use 'Access through your institution' "
                        "in the RealRead Chrome window and retry. If you are signed in, your institution may not "
                        "subscribe to this title.")
    return meta


async def _new_page(context: BrowserContext, control: Control) -> Page:
    """A new tab; in the background while the user is working on a check in another tab."""
    if control.waiting and context.browser:
        try:
            opened = asyncio.get_running_loop().create_future()
            context.once("page", lambda p: opened.done() or opened.set_result(p))
            session = await context.browser.new_browser_cdp_session()
            await session.send("Target.createTarget", {"url": "about:blank", "background": True})
            page = await asyncio.wait_for(opened, 3)
            await session.detach()
            return page
        except Exception:
            pass
    return await context.new_page()


async def fetch_one(cfg: Config, context: BrowserContext, guard: Guard, item_id: str, source: str,
                    emit: Emit, control: Control) -> Meta:
    meta = Meta(id=item_id, status="error", source_url=source, fetched_at=_now())
    host = urllib.parse.urlparse(source).hostname or "unknown"
    if host_key(host) in control.down_hosts:
        return _unavailable(meta, host)
    guard.record_request(host)
    page = await _new_page(context, control)
    keep_tab = False
    # Evidence for how a site treats RealRead: did this visit pass through a check service?
    visited: list[str] = []
    page.on("framenavigated", lambda frame: visited.append(frame.url) if frame == page.main_frame else None)
    try:
        target = source if host_key(host) in control.direct_hosts else rewrite(cfg, source)
        resp = await _goto(page, target)
        if target != source:
            try:
                body = await page.inner_text("body")
            except Exception:
                body = ""
            if not_configured_page(page.url, body, cfg):
                control.direct_hosts.add(host_key(host))
                emit("proxy_skipped", host=host)
                resp = await _goto(page, source)
        if page.url.startswith("chrome-error://"):  # the browser's own error page, not the site's answer
            await _sleep(RELOAD_AFTER_S, control)
            resp = await _goto(page, target)
            if page.url.startswith("chrome-error://"):
                return _failed_to_load(meta, host)
        if page.url == "about:blank":
            return await _read_download(meta, page, context, cfg, target, host)
        try:
            if detect.is_doi_not_found(page.url, await page.title()):
                not_found = _not_found(item_id)
                meta.status, meta.message, meta.final_url = not_found.status, not_found.message, page.url
                return meta
        except Exception:
            pass
        # Count and report the site this actually reached (a DOI address redirects to the publisher),
        # but not an institution's sign-in page or the library proxy.
        here = urllib.parse.urlparse(page.url).hostname or ""
        if here and here != host and target == source and await _classify(page, cfg) != "login":
            if host_key(here) != host_key(host):
                guard.record_request(here)
            host = here
        if await _down(page, resp):
            control.down_hosts.add(host_key(host))
            return _unavailable(meta, host)
        if resp is not None and resp.status == 429:
            until = guard.record_block(host, "rate_limited", _retry_after_s(resp))
            meta.status, meta.message = "rate_limited", f"HTTP 429 from {host}; cooling down until {until:.0f}"
            return meta

        open_access = False
        kind = await _classify(page, cfg)
        if kind == "bot":
            for _ in range(int(AUTO_CLEAR_S / POLL_S)):
                if not await _sleep(POLL_S, control) or (kind := await _classify(page, cfg)) is None:
                    break
        async def page_clear() -> bool:
            return await _classify(page, cfg) is None

        recorded = False  # one article counts as one check, however often the page asks
        attempts = 3
        for attempt in range(attempts):
            if not kind:
                break
            answered = await _wait_for_user(page, kind, host, cfg, emit, control, page_clear)
            if kind == "bot" and not recorded:
                _record_check(guard, host, answered, page, control)
                recorded = True
            if not answered:
                keep_tab = True
                return _still_blocked(meta, page, kind, host)
            kind = None
            # The check or sign-in is done, but the user may be somewhere else now (a home page).
            if attempt < attempts - 1 and not same_article(page.url, source) \
                    and not (await citation_meta(page)).get("citation_title"):
                await _goto(page, target)
                kind = await _classify(page, cfg)
        if detect.is_home_page(page.url) and not detect.is_home_page(source):  # never read a home page as the paper
            keep_tab = True
            return _still_blocked(meta, page, "bot", host)
        await _read_like_a_person(page, control)

        result = await _settled_read(page, control, lambda: _extract_clearing_pdf(
            page, context, cfg, guard, host, emit, control))
        if result is None:
            keep_tab = True
            return _pdf_check_pending(meta, page)
        status, method, text, pdf, cmeta = result
        closed_reason = ""
        article_title = await page.title()
        if status in ("none", "partial"):
            hk = host_key(host)
            body = await _settled_read(page, control, lambda: page.inner_text("body"))
            # An open access article needs no sign-in, whatever the page header offers.
            open_access = detect.says_open_access(body)
            prompt = detect.needs_institution_login(body) and not open_access
            article_url = page.url

            async def back_on_article() -> bool:
                return not await _classify(page, cfg) and same_article(page.url, article_url)

            async def signed_in() -> bool:
                """Back on the article, and it now shows the body or no longer asks to sign in."""
                if not await back_on_article():
                    return False
                return bool(await extract_html(page)) or not detect.needs_institution_login(
                    await page.inner_text("body"))

            async def signed_in_strict() -> bool:
                """For the automatic attempt: the body is readable or access is shown as provided."""
                if not await back_on_article():
                    return False
                return bool(await extract_html(page)) or detect.access_granted(await page.inner_text("body"))

            # No full text on a known publisher may just mean its session ended, and many publishers do
            # not say so (Wiley shows only "Login / Register"). So the wording is not relied on: the
            # session is renewed through the institution once, then the article is read again.
            auto = None
            if hk in control.rejected_hosts:
                closed_reason = "rejected"
            elif hk in control.renewed_hosts or (guard.renewed_recently(host) and not prompt):
                closed_reason = "renewed"
            elif prompt or (site_key(host) and not open_access):
                auto = await _auto_sign_in(page, cfg, guard, host, emit, control, signed_in_strict)
            # auto False: the attempt ran but the article did not open. Back on the article means the
            # institution let the user through without a password; elsewhere it wants the user.
            renewed = bool(auto) or (auto is False and await back_on_article())
            if auto is not None and hk not in control.rejected_hosts:  # the status line says why otherwise
                emit("auto_sign_in_done", host=host, ok=renewed)
            if renewed:
                control.renewed_hosts.add(hk)
                guard.record_renewal(host)
            elif hk in control.rejected_hosts:
                closed_reason = "rejected"
            elif auto is False or (auto is None and prompt and not closed_reason):
                reopens, home_polls = 0, 0

                async def signed_in_or_reopen() -> bool:
                    """Signing in may end on the publisher's home page: open the article again from there."""
                    nonlocal reopens, home_polls
                    if await signed_in():
                        return True
                    at_home = (detect.is_home_page(page.url) and host_key(urllib.parse.urlparse(page.url).hostname or "")
                               == host_key(urllib.parse.urlparse(article_url).hostname or ""))
                    home_polls = home_polls + 1 if at_home else 0
                    if home_polls >= 2 and reopens < 2:  # the user has settled there, not just passing through
                        reopens, home_polls = reopens + 1, 0
                        await _goto(page, article_url)
                        return await signed_in()
                    return False

                if await _wait_for_user(page, "login", host, cfg, emit, control, signed_in_or_reopen):
                    renewed = True
                else:
                    keep_tab = True
                    return _still_blocked(meta, page, "login", host)
            elif not closed_reason:
                closed_reason = "attempts_used_up" if _attempts_used_up(page, cfg, guard, host) else "unchecked"
            if renewed:
                await _read_like_a_person(page, control)
                result = await _settled_read(page, control, lambda: _extract_clearing_pdf(
                    page, context, cfg, guard, host, emit, control))
                if result is None:
                    keep_tab = True
                    return _pdf_check_pending(meta, page)
                if result[0] == "ok" or status == "none":
                    status, method, text, pdf, cmeta = result
                closed_reason = "renewed"

        rejected = closed_reason == "rejected"  # the tab shows the sign-in's error page, not the article
        meta.final_url = article_url if rejected else page.url
        meta.title = (cmeta.get("citation_title")
                      or detect.clean_title(article_title if rejected else await page.title())).strip()
        if issue := issue_key(cmeta):
            if not guard.issue_allowed(issue):
                meta.status = "deferred"
                meta.message = "Daily limit for articles from one journal issue reached (no whole-issue downloads)."
                return meta
            guard.record_issue(issue)
        if status in ("ok", "partial"):
            meta.status, meta.method, meta.chars = status, method, len(text)
            if "_pdf_url" in cmeta:
                meta.extra["pdf_url"] = cmeta["_pdf_url"]
            if status == "partial":
                meta.message = ("Possibly not the full text: short text and no PDF to compare. It may be only the "
                                "abstract, or a complete short article. The text is stored; check it before relying on it.")
            meta.fetched_at = _now()
            library.save(cfg, meta, text=text, pdf=pdf)
            return meta
        if closed_reason == "attempts_used_up":
            when = datetime.fromtimestamp(guard.auto_sign_in_resets_at(host)).strftime("%Y-%m-%d %H:%M")
            meta.status = "deferred"
            meta.message = (f"No full text visible on {host}, but its session may just have ended and an automatic "
                            f"re-sign-in was tried too recently (the next is possible after {when}). This says "
                            "nothing about "
                            "your access. Retry after that, or sign in in the RealRead Chrome window and retry now.")
            return meta
        if await _down(page, None):  # e.g. the article went into maintenance during the sign-in
            control.down_hosts.add(host_key(host))
            control.renewed_hosts.discard(host_key(host))
            return _unavailable(meta, host)
        if closed_reason != "renewed" and (open_access or not (site_key(host) or prompt)):
            meta.status = "not_extracted"
            meta.message = (f"RealRead found no article text on {host}, and the page does not ask you to sign in, so this "
                            "is not an access problem as far as RealRead can tell. "
                            + ("The page says the article is open access. " if open_access else "")
                            + "It may be readable through another channel.")
            return meta
        meta.status = "no_access"
        if rejected:
            meta.message = (f"{host}'s institutional sign-in says it does not recognise your institution, so your "
                            "institution probably has no access there. If you have a personal account, sign in with "
                            "it in the RealRead Chrome window and retry.")
        elif closed_reason == "renewed":
            meta.message = (f"Signed in through your institution on {host} a short while ago, but the article stays "
                            "closed: your institution does not seem to license it.")
        else:
            meta.message = (f"Only metadata/abstract visible on {host}, and RealRead could not renew your sign-in "
                            "there (no automatic institutional sign-in for this publisher). If you should have "
                            "access, sign in in the RealRead Chrome window and retry.")
        return meta
    except Exception as e:  # keep batches going; record the failure
        meta.message = f"{type(e).__name__}: {e}"
        return meta
    finally:
        through_check = any(detect.is_check_host(u) for u in visited)
        if through_check:
            meta.extra["passed_through_check"] = True
        if meta.status not in library.TEXT_STATUSES:
            meta.fetched_at = _now()
            library.save(cfg, meta)
        elif through_check:
            library.save(cfg, meta)
        guard.audit(id=item_id, host=host, status=meta.status, method=meta.method, chars=meta.chars,
                    **({"through_check": True} if through_check else {}))
        if not keep_tab:
            try:
                await page.close()
            except Exception:
                pass


async def _read_directly(cfg: Config, guard: Guard, item_id: str, source: str, emit: Emit) -> Meta | None:
    """A paper anyone can open, read without the browser. None: the browser route takes over."""
    host = urllib.parse.urlparse(source).hostname or "unknown"
    try:
        found = await direct.read(item_id, source)
    except direct.RateLimited as e:
        until = guard.record_block(host, "rate_limited", e.retry_after_s)
        meta = Meta(id=item_id, status="rate_limited", source_url=source, fetched_at=_now(),
                    message=f"HTTP 429 from {host}; cooling down until {until:.0f}")
        guard.audit(id=item_id, host=host, status=meta.status, route="direct")
        return meta
    if not found:
        return None
    meta = Meta(id=item_id, status="ok", source_url=source, final_url=found.url, title=detect.clean_title(found.title),
                method=found.method, chars=len(found.text), fetched_at=_now(), extra={"route": "direct"})
    if found.via_openalex:
        meta.extra["found_through"] = "openalex"
    if issue := issue_key(found.meta):
        if not guard.issue_allowed(issue):
            meta.status, meta.method, meta.chars = "deferred", "", 0
            meta.message = "Daily limit for articles from one journal issue reached (no whole-issue downloads)."
            library.save(cfg, meta)
            return meta
        guard.record_issue(issue)
    library.save(cfg, meta, text=found.text, pdf=found.pdf)
    guard.audit(id=item_id, host=host, status=meta.status, method=meta.method, chars=meta.chars, route="direct")
    return meta


async def fetch_item(cfg: Config, context: BrowserContext | Callable[[], Awaitable[BrowserContext]], guard: Guard,
                     item_id: str, source: str, emit: Emit, control: Control) -> Meta:
    """Plain HTTP if the paper is open to anyone, otherwise (or if that fails) the signed-in browser."""
    host = urllib.parse.urlparse(source).hostname or "unknown"
    if cfg.direct_open_access and host_key(host) not in control.down_hosts:
        guard.record_request(host)
        try:
            if meta := await _read_directly(cfg, guard, item_id, source, emit):
                return meta
        except Exception:
            pass  # the browser route still has its chance
    try:
        ctx = await context() if callable(context) else context
    except Exception as e:
        meta = Meta(id=item_id, status="error", source_url=source, fetched_at=_now(),
                    message=f"The RealRead Chrome could not be reached: {type(e).__name__}: {e}")
        library.save(cfg, meta)
        return meta
    return await fetch_one(cfg, ctx, guard, item_id, source, emit, control)


def _noting_user_action(emit: Emit, keys: set[str], needs_user: asyncio.Event) -> Emit:
    def item_emit(event: str, **data) -> None:
        if event == "blocked":
            keys.add(host_key(data["host"]))  # a DOI address may have led to another publisher's queue
            needs_user.set()
        emit(event, **data)
    return item_emit


async def run_batch(cfg: Config, context: BrowserContext | Callable[[], Awaitable[BrowserContext]], ids: list[str], *,
                    guard: Guard | None = None,
                    refresh: bool = False, emit: Emit | None = None, control: Control | None = None) -> list[Meta]:
    """Fetch many articles, interleaving publishers and obeying the policy."""
    guard = guard or Guard(cfg)
    user_emit = emit or (lambda *a, **k: None)

    def emit(event: str, **data) -> None:  # a failing progress callback must not break a fetch
        try:
            user_emit(event, **data)
        except Exception:
            pass

    control = control or Control()
    order = list(dict.fromkeys(library.normalize_id(i) for i in ids if i.strip()))
    results: dict[str, Meta] = {}
    queues: dict[str, deque[tuple[str, str]]] = {}
    for item_id in order:
        cached = library.load_meta(cfg, item_id)
        if cached and cached.status == "ok" and not refresh:
            results[item_id] = cached
            emit("result", meta=cached, cached=True)
            continue
        source = cached.source_url if cached and cached.source_url and not _is_unresolved(cached.source_url) \
            else await asyncio.to_thread(resolve_url, item_id)
        if source is None:
            results[item_id] = _not_found(item_id)
            library.save(cfg, results[item_id])
            emit("result", meta=results[item_id], cached=False)
            continue
        if detect.is_listing_url(source):
            results[item_id] = Meta(id=item_id, status="refused", source_url=source,
                                    message="Not a single-article address; RealRead does not crawl listings.")
            emit("result", meta=results[item_id], cached=False)
            continue
        queues.setdefault(host_key(urllib.parse.urlparse(source).hostname or ""), deque()).append((item_id, source))

    retried: set[str] = set()
    stop_reason = ""  # ends the whole batch
    given_up: list[str] = []  # publishers whose user action did not happen in time
    parked: dict[str, tuple[asyncio.Task, str, str, set[str]]] = {}  # fetches waiting for the user, by queue
    last_wait = ""

    def settle(key: str, item_id: str, source: str, meta: Meta) -> None:
        if meta.status == "rate_limited" and item_id not in retried:
            retried.add(item_id)
            queues.setdefault(key, deque()).append((item_id, source))
            return
        results[item_id] = meta
        emit("result", meta=meta, cached=False)
        if meta.status in ("needs_user", "needs_login"):
            # Only this publisher waits for the user; the others go on.
            given_up.append(key)
            for later_id, later_source in queues.pop(key, ()):
                results[later_id] = Meta(id=later_id, status="deferred", source_url=later_source,
                                         message=f"Not fetched: a bot check or sign-in on {key} was not completed in time.")
                emit("result", meta=results[later_id], cached=False)

    while (queues or parked) and not control.cancelled:
        for key, (task, item_id, source, _) in list(parked.items()):
            if task.done():
                del parked[key]
                settle(key, item_id, source, task.result())
        held = set().union(*(p[3] for p in parked.values())) if parked else set()
        plans = {}
        for key in [k for k in queues if k not in held]:
            item_host = urllib.parse.urlparse(queues[key][0][1]).hostname or key
            try:
                plans[key] = guard.plan(item_host)
            except QuotaExhausted as e:
                emit("quota", scope=e.scope, resets_at=e.resets_at)
                for item_id, source in queues.pop(key):
                    results[item_id] = Meta(id=item_id, status="deferred", source_url=source,
                                            message=f"Daily limit reached for {e.scope}; try again later.")
                if e.scope == "all publishers":
                    for k in list(queues):
                        for item_id, source in queues.pop(k):
                            results[item_id] = Meta(id=item_id, status="deferred", source_url=source,
                                                    message="Daily limit reached for all publishers.")
                    break
        now = time.time()
        for key in [k for k in plans if k in queues and plans[k].reason == "cooldown"]:
            if plans[key].ready_at - now > MAX_HOLD_S:
                # A long cool-down: leave this publisher for later instead of holding the batch.
                until = datetime.fromtimestamp(plans[key].ready_at).strftime("%Y-%m-%d %H:%M")
                emit("cooldown", host=key, until=plans[key].ready_at)
                for item_id, source in queues.pop(key):
                    results[item_id] = Meta(id=item_id, status="deferred", source_url=source,
                                            message=f"{key} is cooling down after rate limiting / bot checks until {until}.")
        plans = {k: v for k, v in plans.items() if k in queues}
        if not plans:
            if parked:  # nothing else to read: wait for the user's publishers
                await asyncio.wait([p[0] for p in parked.values()], timeout=POLL_S, return_when=asyncio.FIRST_COMPLETED)
                continue
            break
        key = min(plans, key=lambda k: plans[k].ready_at)
        wait = plans[key].ready_at - time.time()
        if wait > 0:
            if last_wait != key or not parked:
                emit("wait", host=key, secs=int(wait), reason=plans[key].reason)
                last_wait = key
            if parked:  # look at the waiting fetches regularly too
                await _sleep(min(wait, POLL_S), control)
                continue
            if not await _sleep(wait, control):
                break
        last_wait = ""
        item_id, source = queues[key].popleft()
        if not queues[key]:
            del queues[key]
        keys = {key}
        needs_user = asyncio.Event()
        task = asyncio.create_task(fetch_item(cfg, context, guard, item_id, source,
                                             _noting_user_action(emit, keys, needs_user), control))
        waiter = asyncio.create_task(needs_user.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if task.done():
            settle(key, item_id, source, task.result())
        else:
            parked[key] = (task, item_id, source, keys)  # goes on by itself once the user has acted

    for key, (task, item_id, source, _) in parked.items():  # cancelled: they stop at their next poll
        settle(key, item_id, source, await task)

    if control.cancelled:
        stop_reason = stop_reason or "cancelled"
    for key in list(queues):
        for item_id, source in queues.pop(key):
            results[item_id] = Meta(id=item_id, status="deferred", source_url=source,
                                    message=f"Not fetched: batch stopped ({stop_reason}).")
    if given_up and not stop_reason:
        stop_reason = "bot check or sign-in not completed in time on " + ", ".join(given_up)
    if stop_reason:
        emit("stopped", why=stop_reason)
    return [results[i] for i in order if i in results]
