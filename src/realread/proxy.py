"""Library proxy (EZproxy-style) support without asking the user for an address.

Forms:
  * template: any link pattern with "$@" (article URL as is) or "$%" (percent-encoded), e.g.
              http://proxy.lib.uni.ac.uk/login?url=$@ (libproxy-db) or
              https://go.openathens.net/redirector/uni.ac.uk?url=$% (OpenAthens redirector)
  * prefix:   https://login.ezproxy.uni.edu/login?url=<target>
  * hostname: https://ieeexplore-ieee-org.ezproxy.uni.edu/document/1  (dots -> hyphens + proxy suffix)
Most users never see any of these; they click "Access through your institution". So the
proxy is optional. It can be looked up by institution name (institutions.py), or learned
from any proxied link or from the tabs the user opened while logging in.
"""

from __future__ import annotations

import re
import urllib.parse

from .config import Config

# A rewritten publisher host followed by the proxy's own domain, e.g.
# "www-sciencedirect-com" + "." + "ezproxy.lib.uni.ac.uk".
_HOSTNAME_RE = re.compile(
    r"^((?:[a-z0-9]+-)+(?:com|org|net|edu|gov|io|ac-uk|co-uk|ac-cn|edu-cn|com-cn|de|fr|jp|ch|nl))\.(.+\..+)$"
)
_PREFIX_RE = re.compile(r"^(https?://[^/]+/login\?q?url=)", re.I)


def parse_link(link: str) -> tuple[str, str]:
    """Return (prefix, suffix) learned from one proxied link; empty strings if neither."""
    link = link.strip()
    if m := _PREFIX_RE.match(link):
        return m.group(1), ""
    host = (urllib.parse.urlparse(link).hostname or "").lower()
    if m := _HOSTNAME_RE.match(host):
        return "", m.group(2)
    return "", ""


def expand(template: str, url: str) -> str:
    """Fill a link template. EZproxy's "qurl=" expects an encoded URL, "url=" the URL as is."""
    enc = urllib.parse.quote(url, safe="")
    return template.replace("qurl=$@", "qurl=" + enc).replace("$%", enc).replace("$@", url)


def openathens_template(domain: str) -> str:
    return f"https://go.openathens.net/redirector/{domain}?url=$%"


def rewrite(cfg: Config, url: str) -> str:
    if cfg.proxy_template:
        return expand(cfg.proxy_template, url)
    if cfg.ezproxy_prefix:
        return cfg.ezproxy_prefix + url
    if cfg.ezproxy_suffix:
        p = urllib.parse.urlparse(url)
        host = (p.hostname or "").replace("-", "--").replace(".", "-")
        return p._replace(netloc=f"{host}.{cfg.ezproxy_suffix}").geturl()
    return url


def proxied(cfg: Config) -> bool:
    return bool(cfg.proxy_template or cfg.ezproxy_prefix or cfg.ezproxy_suffix)


def proxy_host(cfg: Config) -> str | None:
    if cfg.proxy_template:
        return urllib.parse.urlparse(cfg.proxy_template).hostname
    if cfg.ezproxy_prefix:
        return urllib.parse.urlparse(cfg.ezproxy_prefix).hostname
    return cfg.ezproxy_suffix or None


def detect_from_urls(urls: list[str]) -> tuple[str, str]:
    """Pick the proxy suffix seen in open tabs (the most frequent one)."""
    counts: dict[str, int] = {}
    for u in urls:
        _, suffix = parse_link(u)
        if suffix:
            counts[suffix] = counts.get(suffix, 0) + 1
    return ("", max(counts, key=counts.get)) if counts else ("", "")


# EZproxy's default "needhost" page and common library rewordings of it: the proxy is not set up
# for this publisher, so the publisher has to be opened directly.
_NOT_CONFIGURED_HINTS = (
    "ezproxy.cfg", "is not configured", "not been configured", "not set up to proxy",
    "is not in the list of", "the following line must be added",
)


def not_configured_page(url: str, body_text: str, cfg: Config) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    ph = (proxy_host(cfg) or "").lower()
    if not ph or not (host == ph or host.endswith("." + ph)):
        return False
    b = body_text.lower()
    return any(x in b for x in _NOT_CONFIGURED_HINTS)
