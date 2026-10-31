"""Per-publisher knowledge: where the article body is and how to reach the PDF.

Adding a publisher = one entry in BODY_SELECTORS (and optionally PDF_RULES). An empty selector
list means the generic extractor reads the page.
Keys also match EZproxy hostname rewriting (ieeexplore.ieee.org -> ieeexplore-ieee-org.<proxy>).
"""

from __future__ import annotations

import re
import urllib.parse

BODY_SELECTORS: dict[str, list[str]] = {
    "ieeexplore.ieee.org": ["#article", "div.document-text", "xpl-document-full-text"],
    "sciencedirect.com": ["#body", "div.Body"],
    "onlinelibrary.wiley.com": ["section.article-section__full", "div.article__body"],
    "link.springer.com": ["div.c-article-body"],
    "nature.com": ["div.c-article-body"],
    "dl.acm.org": ["section#bodymatter", "div.article__body"],
    "pubs.rsna.org": ["section#bodymatter", "div.article__body"],
    "pubs.acs.org": ["div.widget-ArticleFulltext", "div.article_content"],
    "tandfonline.com": ["div.hlFld-Fulltext"],
    "iopscience.iop.org": ["div.article-content", "#article-content"],
    "journals.aps.org": ["section.article.fulltext", "div.article-section"],
    "science.org": ["section#bodymatter"],
    "pubs.rsc.org": ["div#pnlArticleContent"],
    "mdpi.com": ["div.html-body"],
    "journals.plos.org": ["div.article-text"],
    "spiedigitallibrary.org": [],  # hashed class names: a known publisher, read by the generic extractor
}

# Site key -> (regex on the article path, PDF path template). The host is kept, so
# EZproxy hostname rewriting keeps working.
_DOI_PATH = r"/doi/(?:abs/|full/|epdf/|fullHtml/)?(10\.\d+/[^?#]+)"
PDF_RULES: dict[str, tuple[str, str]] = {
    "ieeexplore.ieee.org": (r"/document/(\d+)", "/stampPDF/getPDF.jsp?tp=&arnumber={0}&ref="),
    "sciencedirect.com": (r"/pii/(\w+)", "/science/article/pii/{0}/pdfft?isDTMRedir=true&download=true"),
    "dl.acm.org": (_DOI_PATH, "/doi/pdf/{0}"),
    "onlinelibrary.wiley.com": (_DOI_PATH, "/doi/pdfdirect/{0}"),
    "tandfonline.com": (_DOI_PATH, "/doi/pdf/{0}"),
    "pubs.acs.org": (_DOI_PATH, "/doi/pdf/{0}"),
    "science.org": (_DOI_PATH, "/doi/pdf/{0}"),
    "pubs.rsna.org": (_DOI_PATH, "/doi/pdf/{0}"),
}

# Site key -> pattern in the page HTML that says this account cannot read the article, for
# publishers that show no visible notice (Wiley's data layer; the issue entry has its own "access").
ACCESS_DENIED_HTML: dict[str, re.Pattern[str]] = {
    "onlinelibrary.wiley.com": re.compile(r'"access":"no","type":"article"'),
}


def site_key(host: str) -> str | None:
    host = (host or "").lower()
    for key in BODY_SELECTORS:
        if host == key or host.endswith("." + key) or key.replace(".", "-") in host:
            return key
    return None


def pdf_rule_url(page_url: str) -> str | None:
    parsed = urllib.parse.urlparse(page_url)
    rule = PDF_RULES.get(site_key(parsed.hostname or "") or "")
    if rule and (m := re.search(rule[0], parsed.path)):
        return f"{parsed.scheme}://{parsed.netloc}{rule[1].format(m.group(1))}"
    return None


def access_denied(page_url: str, html: str) -> bool:
    rule = ACCESS_DENIED_HTML.get(site_key(urllib.parse.urlparse(page_url).hostname or "") or "")
    return bool(rule and rule.search(html))
