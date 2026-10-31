"""Automatic re-sign-in through the user's institution (SAML / Shibboleth "WAYFless" links).

Publisher sessions expire after hours, but the university's identity provider (IdP) often
keeps a much longer single-sign-on session (e.g. Microsoft "Stay signed in"). When a
publisher asks for institutional sign-in again, RealRead can open that publisher's direct
institutional login link for the user's own IdP. The browser and the IdP do the rest; if
the IdP wants a password or a consent click, RealRead stops and asks the user.

RealRead never sees passwords or tokens. It only remembers which institution the user
picked (the IdP entity ID), learned from the user's own sign-ins in the dedicated profile.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Config
from .sites import site_key

_IDP_PARAMS = ("entityid", "idp", "entity_id")
_FED_PARAMS = ("federationid",)

# Publishers on the Atypon platform share one SSO entry point.
ATYPON = {"tandfonline.com", "science.org", "onlinelibrary.wiley.com", "pubs.acs.org", "dl.acm.org", "pubs.rsna.org"}

# Springer Nature's SAML service providers (documented WAYFless format: idp + targetUrl, both encoded).
SPRINGER_NATURE = {"link.springer.com": "https://fsso.springer.com/saml/login",
                   "nature.com": "https://sp.nature.com/saml/login"}

# Publishers signing in through OpenAthens Connect: (publisher's OpenAthens domain, its application id).
# Both are public: they appear in the address of the publisher's own "find your institution" page.
OPENATHENS = {"iopscience.iop.org": ("iop.com", "23cd4589-e9c2-4a1d-8038-de6b5c1a227b"),
              "spiedigitallibrary.org": ("spie.org", "70cc7182-e28c-4862-b8c4-46853d2d0bd0")}


def idp_from_url(url: str) -> tuple[str, str]:
    """(idp entity id, federation id) found in a publisher SSO link, else empty strings."""
    p = urllib.parse.urlparse(url)
    q = {k.lower(): v for k, v in urllib.parse.parse_qsl(p.query)}
    idp = next((q[k] for k in _IDP_PARAMS if q.get(k, "").startswith(("http://", "https://", "urn:"))), "")
    fed = next((q[k] for k in _FED_PARAMS if q.get(k)), "")
    return idp, fed


def wayfless_url(article_url: str, idp: str, federation: str = "") -> str | None:
    """Direct institutional sign-in link that returns to `article_url`, if the publisher is known."""
    p = urllib.parse.urlparse(article_url)
    key = site_key(p.hostname or "")
    origin = f"{p.scheme}://{p.netloc}"
    q = urllib.parse.quote
    if key == "ieeexplore.ieee.org":
        return f"{origin}/servlet/wayf.jsp?entityId={idp}&url={q(article_url, safe='')}"
    if key == "sciencedirect.com":
        return (f"https://auth.elsevier.com/ShibAuth/institutionLogin?entityID={q(idp, safe='')}"
                f"&appReturnURL={q(article_url, safe='')}")
    if key in SPRINGER_NATURE:
        return f"{SPRINGER_NATURE[key]}?idp={q(idp, safe='')}&targetUrl={q(article_url, safe='')}"
    if key in ATYPON:
        path = p.path + (f"?{p.query}" if p.query else "")
        url = f"{origin}/action/ssostart?idp={q(idp, safe='')}&redirectUri={q(path, safe='')}"
        return url + (f"&federationId={q(federation, safe='')}" if federation else "")
    if key in OPENATHENS:
        domain, app = OPENATHENS[key]
        return (f"https://connect.openathens.net/{domain}/{app}/login?entity={q(idp, safe='')}"
                f"&target={q(article_url, safe='')}")
    return None


def university_session_until(cfg: Config) -> datetime | None:
    """When the remembered Microsoft sign-in ("Stay signed in") ends, None if unknown or another provider.

    Only that one cookie's date is read from the profile, never a value. The date is an upper
    bound: the university can end the session earlier.
    """
    for name in ("Cookies", "Network/Cookies"):
        db = cfg.profile / "Default" / name
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(f"{db.as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                row = con.execute("select max(expires_utc) from cookies where host_key like '%login.microsoftonline.com' "
                                  "and name = 'ESTSAUTHPERSISTENT' and is_persistent = 1").fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        return datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=row[0]) if row and row[0] else None
    return None


def learn_from_history(cfg: Config) -> tuple[str, str]:
    """Find the IdP the user picked, from the dedicated profile's own browsing history."""
    hist = cfg.profile / "Default" / "History"
    if not hist.exists():
        return "", ""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "History"
        shutil.copy2(hist, copy)  # Chrome keeps the original locked
        con = sqlite3.connect(copy)
        try:
            rows = con.execute(
                "select url from urls where url like '%entityId=%' or url like '%entityID=%' or url like '%idp=%' "
                "order by last_visit_time desc limit 200"
            ).fetchall()
        finally:
            con.close()
    counts: dict[tuple[str, str], int] = {}
    for (url,) in rows:
        idp, fed = idp_from_url(url)
        if idp:
            counts[(idp, fed)] = counts.get((idp, fed), 0) + 1
    if not counts:
        return "", ""
    best_idp = max({i for i, _ in counts}, key=lambda i: sum(n for (j, _), n in counts.items() if j == i))
    fed = next((f for (i, f) in counts if i == best_idp and f), "")
    return best_idp, fed
