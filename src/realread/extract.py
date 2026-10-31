"""Turn an opened article page (or its PDF) into readable text."""

from __future__ import annotations

import base64
import re
import urllib.parse

import pymupdf
import trafilatura
from playwright.async_api import BrowserContext, Page

from .detect import is_bot_check, looks_like_preview
from .sites import BODY_SELECTORS, access_denied, pdf_rule_url, site_key

MIN_FULLTEXT_CHARS = 5000
# Below this, HTML is double-checked against the PDF.
LONG_FULLTEXT_CHARS = 15000


def to_markdown(html: str, url: str) -> str:
    text = trafilatura.extract(
        html, url=url, output_format="markdown", include_tables=True,
        include_comments=False, favor_recall=True,
    )
    return text or ""


async def extract_html(page: Page) -> str:
    key = site_key(urllib.parse.urlparse(page.url).hostname or "")
    if key and BODY_SELECTORS[key]:
        # Known publisher: only its article-body element counts. Abstract/preview pages
        # (e.g. ScienceDirect "Section snippets" + references) are long but not full text.
        for sel in BODY_SELECTORS[key]:
            loc = page.locator(sel).first
            if await loc.count():
                fragment = await loc.inner_html()
                text = to_markdown(f"<html><body><article>{fragment}</article></body></html>", page.url)
                if len(text) >= LONG_FULLTEXT_CHARS:
                    return text
                # Shorter: abstract + a long reference list also gets here. Trust it only if the
                # page does not offer access to the full version or say the account has none.
                if len(text) >= MIN_FULLTEXT_CHARS and not looks_like_preview(await page.inner_text("body")) \
                        and not access_denied(page.url, await page.content()):
                    return text
        return ""
    text = to_markdown(await page.content(), page.url)
    if len(text) < MIN_FULLTEXT_CHARS or looks_like_preview(text):
        return ""
    return text


async def citation_meta(page: Page) -> dict[str, str]:
    """Highwire/Google Scholar <meta name="citation_*"> tags, first value of each."""
    return await page.evaluate(
        """() => { const o = {}; for (const m of document.querySelectorAll('meta[name^="citation_"]')) {
             const k = m.getAttribute('name'); if (!(k in o)) o[k] = m.getAttribute('content') || ''; } return o; }"""
    )


def issue_key(meta: dict[str, str]) -> str | None:
    journal = meta.get("citation_journal_title") or meta.get("citation_conference_title") or ""
    volume, issue = meta.get("citation_volume", ""), meta.get("citation_issue", "")
    if journal and (volume or issue):
        return f"{journal.strip().lower()}|{volume}|{issue}"
    return None


_PAGE_PDF_JS = """() => {
  const out = [...document.querySelectorAll('link[rel="alternate"][type="application/pdf"]')].map(l => l.href);
  for (const a of document.querySelectorAll('a[href]')) {
    const text = a.textContent.trim().toLowerCase();
    const ownAddress = text === a.href.toLowerCase() && text.endsWith('.pdf');
    const named = ownAddress || /^(download )?(full[- ]text )?(pdf|paper)( file)?$/.test(text);
    if (named && new URL(a.href).hostname === location.hostname)
      out.push(a.href);
  }
  return out.slice(0, 2);
}"""


async def _is_pdf_document(page: Page) -> bool:
    if urllib.parse.urlparse(page.url).path.lower().endswith(".pdf"):
        return True
    try:
        return await page.evaluate("document.contentType") == "application/pdf"
    except Exception:
        return False


async def pdf_candidates(page: Page, meta: dict[str, str]) -> list[str]:
    urls = []
    if await _is_pdf_document(page):  # the address itself is the PDF
        urls.append(page.url)
    if rule := pdf_rule_url(page.url):
        urls.append(rule)
    for name in ("citation_pdf_url", "wkhealth_pdf_url"):
        if meta.get(name):
            urls.append(urllib.parse.urljoin(page.url, meta[name]))
    # A site without a publisher rule: this article's own PDF link on the same host. Other links
    # on the page are never followed.
    if not urls and not BODY_SELECTORS.get(site_key(urllib.parse.urlparse(page.url).hostname or "") or ""):
        try:
            urls.extend(await page.evaluate(_PAGE_PDF_JS))
        except Exception:
            pass
    return list(dict.fromkeys(urls))


_FETCH_JS = """async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  const buf = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return {status: r.status, b64: btoa(s)};
}"""


class PdfBotCheck(Exception):
    """The PDF address answered with a bot check (e.g. Cloudflare) instead of the PDF."""


def _is_bot_page(data: bytes) -> bool:
    html = data[:200_000].decode("utf-8", "replace")
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return is_bot_check(m.group(1) if m else "", html)


async def fetch_pdf(page: Page, context: BrowserContext, url: str) -> bytes | None:
    """The PDF bytes, None if there is no PDF, or PdfBotCheck if a bot check stands in front of it."""
    # First inside the page (the browser's own network stack, same origin), then via the
    # context's request client (shares the profile's cookies, no CORS).
    bot = False
    try:
        res = await page.evaluate(_FETCH_JS, url)
        data = base64.b64decode(res["b64"])
        if data.startswith(b"%PDF"):
            return data
        bot = _is_bot_page(data)
    except Exception:
        pass
    try:
        headers = {"Referer": page.url} if page.url.startswith("http") else {}
        resp = await context.request.get(url, headers=headers, timeout=60_000)
        data = await resp.body()
        if data.startswith(b"%PDF"):
            return data
        bot = bot or _is_bot_page(data)
    except Exception:
        pass
    if bot:
        raise PdfBotCheck(url)
    return None


def pdf_to_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n\n".join(f"<!-- page {i + 1} -->\n{p.get_text()}" for i, p in enumerate(doc))
