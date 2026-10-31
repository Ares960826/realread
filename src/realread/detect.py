"""Pure page classifiers (no browser needed, so they are unit-tested)."""

from __future__ import annotations

import re
import urllib.parse

# Interstitial titles are localised by the browser (Cloudflare "Just a moment..." is "请稍候…").
BOT_TITLE_HINTS = (
    "just a moment", "attention required", "are you a robot", "verify you are human",
    "captcha", "access denied", "request unsuccessful", "bot verification", "security check",
    "请稍候", "请稍等", "人机验证", "安全验证", "验证您是", "访问被拒绝",
)
# Not "/cdn-cgi/challenge-platform": Cloudflare injects that script into normal pages too.
BOT_HTML_HINTS = (
    "challenges.cloudflare.com", "_cf_chl_opt", "cf-challenge",
    "g-recaptcha", "h-captcha", "px-captcha", "perfdrive.com", "geo.captcha-delivery.com",
)
MAX_CHECK_PAGE_CHARS = 3000
CHECK_HOSTS = ("perfdrive.com", "captcha-delivery.com", "challenges.cloudflare.com")
LOGIN_URL_HINTS = ("shibboleth", "/idp/", "wayf", "/saml", "openathens", "/sso", "/cas/login", "/login")
# The publisher offers institutional sign-in and does not say access is already provided.
INSTITUTION_LOGIN_HINTS = (
    "access through your institution", "log in via your institution", "sign in through your institution",
    "institutional sign in", "institutional login", "access through your organization",
    "access through your organisation", "signing in with your organisation", "signing in with your organization",
    "via your institution", "through your library", "通过机构登录", "通过您的机构访问",
)
ACCESS_GRANTED_HINTS = ("access provided by", "access by:", "brought to you by", "access via your institution provided",
                        "you have full access")
# "Full access" alone on its line is a badge. Inside a sentence it may be an offer (RSNA, Wiley).
_FULL_ACCESS_BADGE = re.compile(r"^\s*full access\s*$", re.M)
# The publisher's own sign-in page says it does not know the institution (Atypon sites).
INSTITUTION_REJECTED_HINTS = ("has not been recognized", "has not been recognised")
# The publisher is down or in maintenance: "come back later", never "no access".
UNAVAILABLE_HINTS = (
    "scheduled maintenance", "undergoing maintenance", "under maintenance", "down for maintenance",
    "maintenance in progress", "temporarily unavailable", "service unavailable", "site is currently unavailable",
    "we'll be back", "we will be back", "系统维护", "正在维护",
)
UNAVAILABLE_STATUS = (500, 502, 503, 504)
# Abstract/preview pages on publishers without dedicated selectors.
PREVIEW_MARKERS = ("section snippets", "purchase pdf", "buy article", "rent this article", "get full access",
                   "get access to the full version of this article")  # Wiley, without a licence

# Sentences an article page uses about itself; a bare "open access" may be a journal's advertising.
OPEN_ACCESS_HINTS = (
    "this is an open access article", "this article is open access", "this article is licensed under a creative commons",
    "used under the terms of the creative commons", "distributed under the terms of the creative commons",
)

# Paths that are listings, not single articles. RealRead reads articles; it does not crawl.
_LISTING_RE = re.compile(
    r"(/search|/toc/|/issue[s]?/|/browse|/volumes?/?$|/latest|/sitemap|/rss|/feed|/action/dosearch|/journal/[^/]+/?$)",
    re.I,
)


def is_check_host(url: str) -> bool:
    """The address of a check service itself, which sites redirect visitors through."""
    host = urllib.parse.urlparse(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in CHECK_HOSTS)


def _visible_chars(html: str) -> int:
    text = re.sub(r"<(script|style|noscript)\b.*?</\1\s*>", " ", html, flags=re.I | re.S)
    return len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))


def is_bot_check(title: str, html: str) -> bool:
    t, h = title.lower(), html[:200_000].lower()
    if any(x in t for x in BOT_TITLE_HINTS):
        return True
    # Sites keep check scripts and addresses in their normal pages too (IOP's references
    # validate.perfdrive.com; forms use g-recaptcha). A check page itself has next to no text.
    return any(x in h for x in BOT_HTML_HINTS) and _visible_chars(html) < MAX_CHECK_PAGE_CHARS


def is_login_page(url: str, on_publisher: bool) -> bool:
    return not on_publisher and any(x in url.lower() for x in LOGIN_URL_HINTS)


def needs_institution_login(body_text: str) -> bool:
    return any(x in body_text.lower() for x in INSTITUTION_LOGIN_HINTS) and not access_granted(body_text)


def access_granted(body_text: str) -> bool:
    b = body_text.lower()
    return any(x in b for x in ACCESS_GRANTED_HINTS) or bool(_FULL_ACCESS_BADGE.search(b))


def institution_rejected(body_text: str) -> bool:
    """A short error page from the publisher's sign-in, not an article that mentions the phrase."""
    b = body_text.lower()
    return len(b) < MAX_CHECK_PAGE_CHARS and any(x in b for x in INSTITUTION_REJECTED_HINTS)


def says_open_access(body_text: str) -> bool:
    b = " ".join(body_text.lower().split())
    return any(x in b for x in OPEN_ACCESS_HINTS)


def is_doi_not_found(url: str, title: str) -> bool:
    """doi.org's own error page for a DOI it does not know (it can carry a Cloudflare script)."""
    return (urllib.parse.urlparse(url).hostname or "").endswith("doi.org") and "doi not found" in title.lower()


def is_home_page(url: str) -> bool:
    return urllib.parse.urlparse(url).path in ("", "/")


def clean_title(title: str) -> str:
    """Drop a site name after the last ' | ' of a browser page title."""
    head, sep, tail = title.strip().rpartition(" | ")
    return head.strip() if sep and len(head) >= 15 and len(tail) <= 60 else title.strip()


def is_unavailable(title: str, body_text: str, status: int | None = None) -> bool:
    if status in UNAVAILABLE_STATUS:
        return True
    # Outage pages are short; a long page that mentions maintenance is an article about it.
    if len(body_text) > 3000:
        return False
    t = (title + "\n" + body_text).lower()
    return any(x in t for x in UNAVAILABLE_HINTS)


def looks_like_preview(text: str) -> bool:
    t = text.lower()
    return any(x in t for x in PREVIEW_MARKERS)


def is_listing_url(url: str) -> bool:
    p = urllib.parse.urlparse(url)
    path = p.path or "/"
    if path in ("", "/"):
        return True
    if path.lower().endswith((".xml", ".rss")):
        return True
    q = p.query.lower()
    if any(k in q for k in ("query=", "q=", "searchtext=", "allfield=")):
        return True
    return bool(_LISTING_RE.search(path))
