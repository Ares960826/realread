"""Plain HTTP reading of papers anyone can open: no browser, no sign-in, no time on the page.

RealRead still answers for the result (it must be the full text, not the abstract) and for the
load: this runs inside the batch, so per-publisher spacing, quotas and the audit log all apply.
Whatever does not clearly work here goes on to the browser route.
"""

from __future__ import annotations

import asyncio
import json
import random
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

import pymupdf

from . import __version__, library
from .detect import is_bot_check, looks_like_preview
from .extract import MIN_FULLTEXT_CHARS, pdf_to_text, to_markdown
from .sites import site_key

OPEN_HOSTS = (
    "arxiv.org", "pmc.ncbi.nlm.nih.gov", "mdpi.com", "frontiersin.org", "openaccess.thecvf.com",
    "proceedings.mlr.press", "proceedings.neurips.cc", "papers.nips.cc", "jmlr.org", "aclanthology.org",
    "openreview.net", "biorxiv.org", "medrxiv.org", "hal.science", "journals.plos.org", "par.nsf.gov",
)
USER_AGENT = f"RealRead/{__version__} (personal literature reading tool)"
OPENALEX = "https://api.openalex.org/works/doi:"
PDF_DELAY_S = (2.0, 5.0)
MAX_BYTES = 200_000_000


class RateLimited(Exception):
    def __init__(self, retry_after_s: float):
        super().__init__("HTTP 429")
        self.retry_after_s = retry_after_s


@dataclass
class Found:
    method: str
    text: str
    url: str
    title: str = ""
    pdf: bytes | None = None
    meta: dict[str, str] = field(default_factory=dict)
    via_openalex: bool = False


def is_open_source(url: str) -> bool:
    """A host that serves everyone the same paper, or a PDF address outside the subscription publishers."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in OPEN_HOSTS):
        return True
    return p.path.lower().endswith(".pdf") and not site_key(host)


def _get(url: str) -> tuple[int, bytes, str, float]:
    """(status, body, final url, Retry-After seconds). Failures count as status 0."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(MAX_BYTES), r.geturl(), 0.0
    except urllib.error.HTTPError as e:
        try:
            retry = float(e.headers.get("Retry-After", 0))
        except ValueError:
            retry = 0.0
        return e.code, b"", url, retry
    except (OSError, ValueError):
        return 0, b"", url, 0.0


def published_copy(doi: str) -> str | None:
    """The publisher's own version, if OpenAlex lists it as free to read. Only the DOI is sent."""
    status, body, _, _ = _get(OPENALEX + urllib.parse.quote(doi, safe="/"))
    if status != 200:
        return None
    try:
        work = json.loads(body)
        for loc in [work.get("best_oa_location") or {}, *(work.get("oa_locations") or [])]:
            if loc.get("pdf_url") and loc.get("version") == "publishedVersion":
                return loc["pdf_url"]
    except (ValueError, AttributeError, TypeError):
        pass
    return None


class _Head(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs) -> None:
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta" and (a.get("name") or "").startswith("citation_"):
            self.meta.setdefault(a["name"], a.get("content") or "")

    def handle_endtag(self, tag) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data) -> None:
        if self._in_title:
            self.title += data


def _pdf_found(data: bytes, url: str, head: _Head | None) -> Found | None:
    if not data.startswith(b"%PDF"):
        return None
    try:
        text = pdf_to_text(data)
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            pdf_title = (doc.metadata or {}).get("title") or ""
    except Exception:
        return None
    if len(text) < MIN_FULLTEXT_CHARS:
        return None
    meta = head.meta if head else {}
    return Found("pdf", text, url, meta.get("citation_title") or pdf_title.strip(), data, meta)


async def _read_url(url: str) -> Found | None:
    status, data, final, retry = await asyncio.to_thread(_get, url)
    if status == 429:
        raise RateLimited(retry)
    if status != 200:
        return None
    if found := _pdf_found(data, final, None):
        return found
    html = data.decode("utf-8", "replace")
    head = _Head()
    try:
        head.feed(html[:500_000])
    except Exception:
        pass
    if is_bot_check(head.title, html):
        return None
    if pdf_url := head.meta.get("citation_pdf_url"):
        await asyncio.sleep(random.uniform(*PDF_DELAY_S))
        status, pdf, pdf_final, retry = await asyncio.to_thread(_get, urllib.parse.urljoin(final, pdf_url))
        if status == 429:
            raise RateLimited(retry)
        if status == 200 and (found := _pdf_found(pdf, pdf_final, head)):
            return found
    text = to_markdown(html, final)
    if len(text) >= MIN_FULLTEXT_CHARS and not looks_like_preview(html):
        return Found("html", text, final, head.meta.get("citation_title") or head.title.strip(), None, head.meta)
    return None


async def read(item_id: str, source: str) -> Found | None:
    """The full text by plain HTTP, or None if the browser route should look at this paper."""
    if is_open_source(source):
        return await _read_url(source)
    if library.is_doi(item_id) and (copy := await asyncio.to_thread(published_copy, item_id)):
        found = await _read_url(copy)
        if found:
            found.via_openalex = True
        return found
    return None
