"""Look up how an institution gives off-campus access, from public directories.

Nothing here is specific to one university. Sources:
  * SeamlessAccess discovery service (https://seamlessaccess.org): every identity provider
    in eduGAIN and OpenAthens, with its SAML entity ID and domain. Used for automatic
    institutional re-sign-in (sso.py).
  * libproxy-db (https://github.com/tom5760/ezproxy-db, CC BY-SA 4.0): ~1500 library proxy
    link patterns. Downloaded at run time and cached; not shipped with RealRead.
  * OpenAthens redirector (https://go.openathens.net/redirector/<domain>): works for
    institutions that registered their domain with OpenAthens. Checked with one request.

Results are suggestions the user confirms; RealRead never guesses silently.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir

from . import __version__
from .config import APP
from .proxy import openathens_template

MDQ_SEARCH = "https://md.seamlessaccess.org/entities/?q={q}"
LIBPROXY_DB = "https://libproxy-db.org/proxies.json"
CACHE_MAX_AGE_S = 7 * 86400
_UA = {"User-Agent": f"RealRead/{__version__} (institution lookup)"}


@dataclass
class Institution:
    name: str
    domain: str = ""
    entity_id: str = ""  # SAML IdP, for automatic re-sign-in
    federation: str = ""  # registration authority of the IdP
    proxy_template: str = ""  # library proxy link pattern from libproxy-db
    country: str = ""


def _get_json(url: str, timeout: float = 20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return json.load(r)


def _cache_file() -> Path:
    return Path(user_cache_dir(APP)) / "libproxy-db.json"


def libproxy_entries(refresh: bool = False) -> list[dict]:
    """libproxy-db entries ({name, url, country}), cached for a week."""
    path = _cache_file()
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < CACHE_MAX_AGE_S:
        return json.loads(path.read_text())
    try:
        data = [{"name": e.get("name", ""), "url": e.get("url", ""), "country": e.get("country", "")}
                for e in _get_json(LIBPROXY_DB) if "$@" in e.get("url", "")]
    except OSError:
        if path.exists():  # offline: an old copy is better than nothing
            return json.loads(path.read_text())
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def idp_entries(query: str) -> list[dict]:
    return _get_json(MDQ_SEARCH.format(q=urllib.parse.quote(query)))


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", s.casefold())


def _matches(query: str, name: str) -> bool:
    words = _words(name)
    return all(any(w.startswith(q) for w in words) for q in _words(query))


def _same_org(host: str, domain: str) -> bool:
    host, domain = host.lower(), domain.lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def combine(query: str, idps: list[dict], proxies: list[dict]) -> list[Institution]:
    """Merge identity providers and library proxies that belong to the same institution."""
    found: list[Institution] = []
    for e in idps:
        if e.get("hidden") == "true" or e.get("type", "idp") != "idp" or not e.get("entityID"):
            continue
        name = e.get("title") or e.get("descr") or e["entityID"]
        if "test" in _words(name):
            continue
        found.append(Institution(
            name=name, domain=e.get("domain") or "", entity_id=e["entityID"],
            federation=next(iter(e.get("registrationAuthority") or []), ""),
        ))
    for p in proxies:
        if not _matches(query, p["name"]):
            continue
        host = urllib.parse.urlparse(p["url"]).hostname or ""
        same = next((i for i in found if not i.proxy_template and
                     (_same_org(host, i.domain) or _words(i.name) == _words(p["name"]))), None)
        if same:
            same.proxy_template, same.country = p["url"], p["country"]
        else:
            found.append(Institution(name=p["name"], proxy_template=p["url"], country=p["country"]))
    found.sort(key=lambda i: (not _matches(query, i.name), not (i.entity_id and i.proxy_template), i.name))
    return found


def search(query: str, refresh: bool = False) -> list[Institution]:
    errors: list[str] = []
    idps: list[dict] = []
    proxies: list[dict] = []
    try:
        idps = idp_entries(query)
    except OSError as e:
        errors.append(f"SeamlessAccess: {e}")
    try:
        proxies = libproxy_entries(refresh)
    except OSError as e:
        errors.append(f"libproxy-db: {e}")
    if errors and not idps and not proxies:
        raise OSError("; ".join(errors))
    return combine(query, idps, proxies)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def openathens_available(domain: str, timeout: float = 15) -> bool:
    """Does the OpenAthens redirector know this domain? (It redirects if so, answers 403 if not.)"""
    if not domain:
        return False
    url = openathens_template(domain).replace("$%", urllib.parse.quote("https://doi.org/", safe=""))
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
            return False if r.status >= 400 else 300 <= r.status < 400
    except urllib.error.HTTPError as e:
        return 300 <= e.code < 400
    except OSError:
        return False
