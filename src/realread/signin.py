"""Walk through an institutional sign-in the way a person does, clicking what they would click.

Used when no direct link exists, or a direct link stops on a page that wants a click (an account
to pick, "stay signed in", a list of institutions). It only clicks through a sign-in whose
university session is still alive. It never types a password or code, never touches a bot check
and never accepts a consent page: those go to the user.
"""

from __future__ import annotations

import random
import re
import time
import urllib.parse
from collections import Counter
from collections.abc import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from . import detect
from .sites import site_key

CLICK_PAUSE_S = (0.8, 2.0)
MAX_CLICKS = 8
LOGIN_BUTTON = re.compile(
    r"(access|log ?in|sign ?in)\b.{0,20}\b(institution|organi[sz]ation|library)"
    r"|institutional (sign ?in|log ?in|login)|find your institution|通过.{0,6}机构", re.I)
PERSON_TEXT = ("verification code", "approve sign in", "approve the request", "enter code", "authenticator",
               "two-step", "two-factor", "验证码", "批准登录")
# An error on the way back from the university. IEEE shows one now and then; signing in again works.
FAILED_TEXT = ("unable to log in", "unable to sign in", "cannot log in", "cannot sign in", "could not log you in",
               "could not sign you in", "login failed", "sign-in failed", "sign in failed", "无法登录", "登录失败")
MAX_FAILED_PAGE_CHARS = 3000
_SEARCH_BOX = ('input[type=search], input[role=combobox], input[placeholder*="institution" i], '
               'input[placeholder*="organi" i], input[aria-label*="institution" i], input[placeholder*="机构"]')
_MICROSOFT_HOSTS = ("login.microsoftonline.com", "login.live.com")


def _host(page: Page) -> str:
    return (urllib.parse.urlparse(page.url).hostname or "").lower()


async def needs_person(page: Page) -> bool:
    """A password, a one-time code or a bot check is on the screen."""
    if detect.is_check_host(page.url) or detect.is_bot_check(await page.title(), await page.content()):
        return True
    if site_key(_host(page)):  # publishers keep personal sign-in forms on normal pages
        return False
    if await page.locator("input[type=password]:visible").count():
        return True
    body = (await page.inner_text("body")).lower()
    return any(x in body for x in PERSON_TEXT)


async def sign_in_failed(page: Page) -> bool:
    """A short error page; an article may well discuss failing logins."""
    body = (await page.inner_text("body")).lower()
    return len(body) < MAX_FAILED_PAGE_CHARS and any(x in body for x in FAILED_TEXT)


async def _visible(locator: Locator) -> Locator | None:
    for i in range(min(await locator.count(), 5)):
        item = locator.nth(i)
        if await item.is_visible():
            return item
    return None


async def login_button(page: Page) -> Locator | None:
    for role in ("link", "button"):
        if found := await _visible(page.get_by_role(role, name=LOGIN_BUTTON)):
            return found
    return None


async def _microsoft_step(page: Page) -> tuple[str, Locator] | None:
    stay = page.locator("#KmsiCheckboxField, #KmsiDescription, #kmsiTitle")
    yes = page.locator("#idSIButton9")
    if await stay.count() and await yes.count() and await yes.first.is_visible():
        return "stay signed in", yes.first
    tiles = page.get_by_role("button", name=re.compile("@"))
    if await tiles.count() == 1 and await tiles.first.is_visible():  # several accounts: the user's choice
        return "account", tiles.first
    return None


async def _institution_choice(page: Page, institution: str) -> Locator | None:
    name = re.compile(re.escape(institution), re.I)
    for role in ("option", "link", "button", "listitem"):
        if found := await _visible(page.get_by_role(role, name=name)):
            return found
    return None


async def walk(page: Page, done: Callable[[], Awaitable[bool]], institution: str, seconds: float, poll_s: float,
               sleep: Callable[[float], Awaitable[bool]], button_after_s: float = 0.0) -> bool:
    """True once `done` says so. False if a person is needed, nothing can be clicked or time runs out.

    `button_after_s`: the publisher's own login button only counts after this long, because a
    sign-in chain that is still running can pass through the article page.
    """
    started = time.monotonic()
    clicks: Counter[str] = Counter()
    searched = False
    while time.monotonic() - started < seconds:
        if not await sleep(poll_s):
            return False
        try:
            if await done():
                return True
            if page.is_closed() or await needs_person(page) or await sign_in_failed(page):
                return False
            label, target = "", None
            if _host(page) in _MICROSOFT_HOSTS:
                if step := await _microsoft_step(page):
                    label, target = step
            elif not site_key(_host(page)) or time.monotonic() - started >= button_after_s:
                if institution and not site_key(_host(page)):
                    if target := await _institution_choice(page, institution):
                        label = "institution"
                    elif not searched and (box := await _visible(page.locator(_SEARCH_BOX))):
                        searched = True
                        await box.press_sequentially(institution, delay=random.uniform(40, 90))
                        continue
                if target is None and (button := await login_button(page)):
                    label, target = "login button", button
            if target is None or clicks[label] >= 2 or sum(clicks.values()) >= MAX_CLICKS:
                continue
            clicks[label] += 1
            await sleep(random.uniform(*CLICK_PAUSE_S))
            await target.click(timeout=5_000)
        except PlaywrightError:
            continue  # the page is navigating; look again
    return False
