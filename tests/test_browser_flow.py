"""End-to-end batch flow against synthetic publisher pages (no network, no real content).

Run with: uv run pytest -m browser   (needs a local Google Chrome)
"""

from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import async_playwright

from realread import fetcher, notify
from realread.config import Config, default_chrome
from realread.fetcher import Control, run_batch
from realread.policy import Guard, Limits

pytestmark = pytest.mark.browser

BODY = " ".join(f"Sentence {i} about the method and its results, explained at length." for i in range(600))


def article(title: str, body_html: str, issue: str = "1") -> str:
    return f"""<html><head><title>{title}</title>
<meta name="citation_title" content="{title}"><meta name="citation_journal_title" content="Synthetic Journal">
<meta name="citation_volume" content="7"><meta name="citation_issue" content="{issue}"></head>
<body><header>Access provided by Test University</header>{body_html}</body></html>"""


PAGES = {
    "/science/article/pii/FULL1": (200, article("Full one", f'<div id="body"><h2>Introduction</h2><p>{BODY}</p></div>')),
    "/science/article/pii/FULL2": (200, article("Full two", f'<div id="body"><h2>Methods</h2><p>{BODY}</p></div>', "2")),
    "/science/article/pii/SAME1": (200, article("Same issue", f'<div id="body"><p>{BODY}</p></div>', "9")),
    "/science/article/pii/SAME2": (200, article("Same issue 2", f'<div id="body"><p>{BODY}</p></div>', "9")),
    "/science/article/pii/PREVIEW": (200, "<html><head><title>Preview</title></head><body><h1>Paper</h1>"
                                          "<p>Abstract only.</p><h2>Section snippets</h2><p>" + BODY[:6000] +
                                          "</p><button>Access through your organization</button></body></html>"),
    "/science/article/pii/CHECK": (200, "<html><head><title>Just a moment...</title></head><body>"
                                        "<div class='cf-challenge'>verify</div></body></html>"),
    "/science/article/pii/DOWN": (200, "<html><head><title>Maintenance</title></head><body>ScienceDirect is "
                                       "undergoing scheduled maintenance. We'll be back soon.</body></html>"),
    "/science/article/pii/LIMITED": (429, "<html><head><title>Too many</title></head><body>slow down</body></html>"),
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "desktop", lambda *a, **k: None)
    monkeypatch.setattr(fetcher, "DWELL_S", (0.05, 0.1))
    monkeypatch.setattr(fetcher, "PDF_DELAY_S", (0.0, 0.01))
    monkeypatch.setattr(fetcher, "AUTO_CLEAR_S", 1)
    monkeypatch.setattr(fetcher, "POLL_S", 0.3)
    cfg = Config(profile_dir=str(tmp_path / "p"), library_dir=str(tmp_path / "lib"), chrome_path=default_chrome(),
                 max_wait_for_user_s=3, direct_open_access=False)
    guard = Guard(cfg, Limits(min_interval_s=0.2, per_host_hour=100, per_host_day=100, total_day=100, per_issue_day=1))
    return cfg, guard


async def _with_context(fn):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()

        async def handle(route):
            path = route.request.url.split("sciencedirect.com", 1)[-1].split("?")[0]
            status, html = PAGES.get(path, (404, "<html><title>nf</title></html>"))
            await route.fulfill(status=status, body=html, content_type="text/html")

        await ctx.route("https://www.sciencedirect.com/**", handle)
        try:
            return await fn(ctx)
        finally:
            await browser.close()


def run(coro_fn):
    return asyncio.run(_with_context(coro_fn))


U = "https://www.sciencedirect.com/science/article/pii/"


def test_full_text_preview_listing_and_issue_cap(env):
    cfg, guard = env
    events = []
    ids = [U + "FULL1", "https://www.sciencedirect.com/search?qs=x", U + "SAME1", U + "SAME2", U + "PREVIEW"]
    results = run(lambda ctx: run_batch(cfg, ctx, ids, guard=guard, emit=lambda k, **d: events.append((k, d))))
    by = {m.id.rsplit("/", 1)[-1]: m for m in results}
    assert by["FULL1"].status == "ok" and by["FULL1"].method == "html" and by["FULL1"].chars > 15000
    assert by["search?qs=x"].status == "refused"
    assert by["SAME1"].status == "ok" and by["SAME2"].status == "deferred"  # one per issue in this test
    # The preview asks for institutional sign-in: the batch waited for the user, then deferred that publisher.
    assert by["PREVIEW"].status == "needs_login"
    assert any(k == "blocked" and d["kind"] == "login" for k, d in events)
    assert guard.audit_tail(10)


def test_bot_check_blocks_only_that_publisher(env):
    cfg, guard = env
    results = run(lambda ctx: run_batch(cfg, ctx, [U + "CHECK", U + "FULL2"], guard=guard))
    by = {m.id.rsplit("/", 1)[-1]: m for m in results}
    assert by["CHECK"].status == "needs_user"
    # Nothing else from this publisher is fetched while the user has not cleared the check.
    assert by["FULL2"].status == "deferred"
    assert guard.usage()["hosts"]["sciencedirect.com"]["bot_checks_24h"] == 1


def test_bot_check_resumes_when_user_clears_it(env):
    cfg, guard = env
    cfg.max_wait_for_user_s = 20

    async def scenario(ctx):
        async def user_solves(event, **d):
            if event == "blocked":
                async def later():
                    await asyncio.sleep(0.5)
                    page = next(p for p in ctx.pages if "CHECK" in p.url)
                    await page.goto(U + "FULL1")  # the check is gone, the article shows
                asyncio.get_running_loop().create_task(later())

        events = []

        def emit(event, **d):
            events.append(event)
            asyncio.get_running_loop().create_task(user_solves(event, **d))

        res = await run_batch(cfg, ctx, [U + "CHECK", U + "FULL2"], guard=guard, emit=emit)
        return res, events

    results, events = run(scenario)
    assert [m.status for m in results] == ["ok", "ok"], [m.message for m in results]
    assert "blocked" in events and "unblocked" in events


def test_rate_limited_cools_down(env):
    cfg, guard = env
    results = run(lambda ctx: run_batch(cfg, ctx, [U + "LIMITED", U + "FULL1"], guard=guard, control=Control()))
    # HTTP 429 puts the publisher into a cool-down; its remaining items are deferred, not hammered.
    assert guard.usage()["hosts"]["sciencedirect.com"]["cooldown_s"] >= 1700
    assert [m.status for m in results] == ["deferred", "deferred"]
    assert "cooling down" in results[1].message


CHALLENGE_HTML = PAGES["/science/article/pii/CHECK"][1]


def _pdf_bytes() -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for i in range(4):
        doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 560, 800), BODY[i * 2000:(i + 1) * 2000], fontsize=8)
    return doc.tobytes()


def test_abstract_with_long_references_is_not_full_text(env):
    """Wiley without a licence: the body element holds abstract + references, and a notice offers access."""
    cfg, guard = env
    refs = "".join(f"<li>{i} Author A, Author B. A cited paper about method {i}. Journal 1999; 7: 1-10.</li>"
                   for i in range(80))
    page_html = ("<html><head><title>No licence</title></head><body><nav>Login / Register</nav>"
                 "<strong>Get access to the full version of this article. View access options below.</strong>"
                 f"<div id='body'><h3>Abstract</h3><p>Short abstract.</p><h2>REFERENCES</h2><ul>{refs}</ul></div>"
                 "</body></html>")
    PAGES["/science/article/pii/NOLICENCE"] = (200, page_html)
    try:
        results = run(lambda ctx: run_batch(cfg, ctx, [U + "NOLICENCE"], guard=guard))
    finally:
        del PAGES["/science/article/pii/NOLICENCE"]
    # Not stored as a "partial" full text. No institution is known, so the sign-in cannot be renewed:
    # reported, without holding the batch.
    assert results[0].status == "no_access", results[0]
    assert "could not renew" in results[0].message


NO_LICENCE_HTML = ("<html><head><title>Pdf only</title></head><body><nav>Login / Register</nav>"
                   "<p>Abstract only.</p><a href='#'>Download PDF</a></body></html>")


async def _pdf_behind_check_context(fn, after_check: str | None):
    """An article whose HTML has only the abstract; its PDF sits behind a Cloudflare-style check.

    Scripted requests for the PDF always meet the check (as on Wiley). Opening the PDF in the tab
    passes it, as the browser solving the JS challenge, and then shows `after_check`: "pdf", or
    "abstract" (a redirect back, as publishers do without a licence). None: it never clears.
    """
    pdf = _pdf_bytes()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()

        async def handle(route):
            path = route.request.url.split("sciencedirect.com", 1)[-1].split("?")[0]
            if path.endswith("/pdfft"):
                if not route.request.is_navigation_request() or after_check is None:
                    await route.fulfill(status=403, body=CHALLENGE_HTML, content_type="text/html")
                elif after_check == "pdf":
                    await route.fulfill(status=200, body=pdf, content_type="application/pdf")
                else:
                    await route.fulfill(status=302, headers={"Location": path.removesuffix("/pdfft")})
            elif after_check == "abstract":
                await route.fulfill(status=200, body=NO_LICENCE_HTML, content_type="text/html")
            else:
                await route.fulfill(status=200, content_type="text/html",
                                    body=article("Pdf only", "<p>Abstract only; the body is in the PDF.</p>"))

        await ctx.route("https://www.sciencedirect.com/**", handle)
        try:
            return await fn(ctx)
        finally:
            await browser.close()


def test_pdf_behind_bot_check_is_not_reported_as_no_access(env):
    cfg, guard = env
    events = []
    results = asyncio.run(_pdf_behind_check_context(
        lambda ctx: run_batch(cfg, ctx, [U + "PDFONLY", U + "FULL2"], guard=guard,
                              emit=lambda k, **d: events.append((k, d))), None))
    assert results[0].status == "needs_user", results[0]
    assert any(k == "blocked" and d["kind"] == "bot" for k, d in events)
    assert results[1].status == "deferred"  # the batch waits for the user, as for any bot check
    assert guard.usage()["hosts"]["sciencedirect.com"]["bot_checks_24h"] == 1


def test_pdf_taken_from_the_tab_once_the_check_clears(env):
    from realread import library
    cfg, guard = env
    events = []
    results = asyncio.run(_pdf_behind_check_context(
        lambda ctx: run_batch(cfg, ctx, [U + "PDFONLY"], guard=guard,
                              emit=lambda k, **d: events.append((k, d))), "pdf"))
    assert results[0].status == "ok" and results[0].method == "pdf", results[0]
    assert "blocked" not in [k for k, _ in events]
    assert (library.entry_dir(cfg, results[0].id) / "source.pdf").exists()


def test_pdf_redirected_to_abstract_is_not_full_text(env):
    cfg, guard = env
    events = []
    results = asyncio.run(_pdf_behind_check_context(
        lambda ctx: run_batch(cfg, ctx, [U + "PDFONLY"], guard=guard,
                              emit=lambda k, **d: events.append((k, d))), "abstract"))
    assert results[0].status == "no_access", results[0]
    assert not [d for k, d in events if k == "blocked"]


IDP = "https://idp.example.ac.uk/shibboleth"
PREVIEW_HTML = PAGES["/science/article/pii/PREVIEW"][1]
FULL_HTML = PAGES["/science/article/pii/FULL1"][1]


# An ended session that says nothing about signing in (Wiley shows only a personal "Login / Register"): abstract only.
SILENT_HTML = ("<html><head><title>Paper</title></head><body><nav>My account</nav><h1>Paper</h1>"
               "<p>Abstract only.</p></body></html>")


async def _sso_context(fn, idp_alive: bool, licensed: bool = True, closed_html: str = PREVIEW_HTML, calls=None,
                       return_home: bool = False):
    """ScienceDirect article whose session has expired; the IdP either still has an SSO session or wants a password."""
    state = {"signed": False}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()

        async def article(route):
            await route.fulfill(status=200, body=FULL_HTML if state["signed"] and licensed else closed_html,
                                content_type="text/html")

        async def institution_login(route):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(route.request.url).query)
            assert q["entityID"] == [IDP]
            if calls is not None:
                calls.append(q["appReturnURL"][0])
            if idp_alive:
                state["signed"] = True  # IdPs return via an auto-submitting page, not a plain redirect
                back = "https://www.sciencedirect.com/" if return_home else q["appReturnURL"][0]
                await route.fulfill(status=200, content_type="text/html",
                                    body=f"<html><head><meta http-equiv='refresh' content='0;url={back}'></head></html>")
            else:
                await route.fulfill(status=200, content_type="text/html",
                                    body="<html><title>Sign in</title><form><input type=password></form></html>")

        await ctx.route("https://www.sciencedirect.com/**", article)
        await ctx.route("https://auth.elsevier.com/**", institution_login)
        try:
            return await fn(ctx)
        finally:
            await browser.close()


def test_auto_sign_in_through_institution(env):
    cfg, guard = env
    cfg.idp_entity_id = IDP
    events = []
    results = asyncio.run(_sso_context(
        lambda ctx: run_batch(cfg, ctx, [U + "EXPIRED"], guard=guard, emit=lambda k, **d: events.append(k)), True))
    assert results[0].status == "ok", (events, results[0])
    assert "auto_sign_in" in events and "blocked" not in events


def test_auto_sign_in_falls_back_to_user(env, monkeypatch):
    cfg, guard = env
    cfg.idp_entity_id = IDP
    monkeypatch.setattr(fetcher, "AUTO_SIGN_IN_S", 1.5)
    events = []
    results = asyncio.run(_sso_context(
        lambda ctx: run_batch(cfg, ctx, [U + "EXPIRED"], guard=guard, emit=lambda k, **d: events.append(k)), False))
    assert results[0].status == "needs_login"
    assert events.index("auto_sign_in") < events.index("blocked")


def test_signed_in_but_not_licensed_does_not_hold_the_batch(env, monkeypatch):
    cfg, guard = env
    cfg.idp_entity_id = IDP
    cfg.max_wait_for_user_s = 60
    monkeypatch.setattr(fetcher, "AUTO_SIGN_IN_S", 1.5)
    events = []
    results = asyncio.run(_sso_context(
        lambda ctx: run_batch(cfg, ctx, [U + "EXPIRED"], guard=guard, emit=lambda k, **d: events.append(k)),
        True, licensed=False))
    assert results[0].status == "no_access", results[0]
    assert "does not seem to license" in results[0].message
    assert "auto_sign_in" in events and "blocked" not in events


def test_ended_session_without_prompt_is_renewed(env):
    """The publisher shows no sign-in prompt at all: RealRead renews the session anyway, then reads."""
    cfg, guard = env
    cfg.idp_entity_id = IDP
    events = []
    results = asyncio.run(_sso_context(
        lambda ctx: run_batch(cfg, ctx, [U + "SILENT"], guard=guard, emit=lambda k, **d: events.append(k)),
        True, closed_html=SILENT_HTML))
    assert results[0].status == "ok", (events, results[0])
    assert "auto_sign_in" in events and "blocked" not in events


def test_sign_in_ending_on_publisher_home_page_returns_to_article(env, monkeypatch):
    """ACS finishes the institutional sign-in on its home page: RealRead opens the article again itself."""
    cfg, guard = env
    cfg.idp_entity_id = IDP
    monkeypatch.setattr(fetcher, "AUTO_SIGN_IN_S", 1.5)
    events = []
    results = asyncio.run(_sso_context(
        lambda ctx: run_batch(cfg, ctx, [U + "HOME"], guard=guard, emit=lambda k, **d: events.append(k)),
        True, closed_html=SILENT_HTML, return_home=True))
    assert results[0].status == "ok", (events, results[0])
    assert "blocked" not in events


def test_session_renewed_once_per_batch(env, monkeypatch):
    """After a renewal in this batch, a closed article on the same publisher is not licensed: no second attempt."""
    cfg, guard = env
    cfg.idp_entity_id = IDP
    monkeypatch.setattr(fetcher, "AUTO_SIGN_IN_S", 1.5)
    calls, events = [], []
    results = asyncio.run(_sso_context(
        lambda ctx: run_batch(cfg, ctx, [U + "CLOSED1", U + "CLOSED2"], guard=guard,
                              emit=lambda k, **d: events.append(k)),
        True, licensed=False, closed_html=SILENT_HTML, calls=calls))
    assert [m.status for m in results] == ["no_access", "no_access"], results
    assert len(calls) == 1 and "blocked" not in events
    assert all("does not seem to license" in m.message for m in results)


def test_library_proxy_falls_back_to_direct_when_not_set_up(env):
    cfg, guard = env
    cfg.proxy_template = "https://proxy.uni.example/login?url=$@"
    needhost = ("<html><head><title>Host not configured</title></head><body>To allow "
                "https://www.sciencedirect.com to be used, the following line must be added to the ezproxy.cfg "
                "file: Host www.sciencedirect.com</body></html>")

    async def scenario(ctx):
        proxied = []

        async def proxy_page(route):
            proxied.append(route.request.url)
            await route.fulfill(status=200, body=needhost, content_type="text/html")

        await ctx.route("https://proxy.uni.example/**", proxy_page)
        events = []
        results = await run_batch(cfg, ctx, [U + "FULL1", U + "FULL2"], guard=guard,
                                  emit=lambda k, **d: events.append(k))
        return results, proxied, events

    results, proxied, events = run(scenario)
    assert [m.status for m in results] == ["ok", "ok"]
    assert len(proxied) == 1  # tried once; the second paper went straight to the publisher
    assert events.count("proxy_skipped") == 1


def test_maintenance_page_defers_instead_of_no_access(env):
    cfg, guard = env
    results = run(lambda ctx: run_batch(cfg, ctx, [U + "DOWN", U + "FULL1"], guard=guard))
    assert [m.status for m in results] == ["deferred", "deferred"], results
    assert "maintenance" in results[0].message
    assert guard.usage()["hosts"]["sciencedirect.com"]["last_24h"] == 1  # the second paper was not requested


# -- sign-in and access reporting -----------------------------------------------------------------------

CHECK_HTML = CHALLENGE_HTML
HOME_HTML = "<html><title>Home</title><body>Welcome to the publisher</body></html>"
HOME = "https://www.sciencedirect.com/"


async def _site_context(fn, respond):
    """`respond(url)` returns (status, body, content type)."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()

        async def handle(route):
            status, body, ctype = respond(route.request.url)
            await route.fulfill(status=status, body=body, content_type=ctype)

        await ctx.route("**/*", handle)
        try:
            return await fn(ctx)
        finally:
            await browser.close()


def _leave_for_home_on_block(ctx, opened):
    def emit(event, **d):
        if event == "blocked":
            async def later():
                await asyncio.sleep(0.3)
                opened["signed_in"] = True
                page = next(p for p in ctx.pages if p.url == d["url"])
                await page.goto(HOME)
            asyncio.get_running_loop().create_task(later())
    return emit


def test_article_reopened_when_bot_check_ends_on_home_page(env):
    cfg, guard = env
    cfg.max_wait_for_user_s = 20
    state = {}

    def respond(url):
        if url == HOME:
            return 200, HOME_HTML, "text/html"
        return 200, FULL_HTML if state.get("signed_in") else CHECK_HTML, "text/html"

    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, [U + "A1"], guard=guard, emit=_leave_for_home_on_block(ctx, state)), respond))
    assert results[0].status == "ok", results[0]
    assert results[0].final_url == U + "A1"


def test_article_reopened_when_sign_in_ends_on_home_page(env):
    cfg, guard = env
    cfg.max_wait_for_user_s = 20
    state = {}

    def respond(url):
        if url == HOME:
            return 200, HOME_HTML, "text/html"
        return 200, FULL_HTML if state.get("signed_in") else PREVIEW_HTML, "text/html"

    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, [U + "A2"], guard=guard, emit=_leave_for_home_on_block(ctx, state)), respond))
    assert results[0].status == "ok", results[0]


def test_timed_out_bot_check_is_recorded_in_the_past_tense(env):
    cfg, guard = env
    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, [U + "A3"], guard=guard), lambda url: (200, CHECK_HTML, "text/html")))
    assert results[0].status == "needs_user"
    assert "not cleared in time" in results[0].message and "(not a sign-in)" in results[0].message
    assert results[0].message.startswith("At 20")


def test_closing_the_tab_during_a_check_is_not_taken_as_cleared(env):
    cfg, guard = env
    cfg.max_wait_for_user_s = 20

    async def scenario(ctx):
        def emit(event, **d):
            if event == "blocked":
                page = next(p for p in ctx.pages if p.url == d["url"])
                asyncio.get_running_loop().create_task(page.close())
        return await run_batch(cfg, ctx, [U + "A5"], guard=guard, emit=emit)

    results = asyncio.run(_site_context(scenario, lambda url: (200, CHECK_HTML, "text/html")))
    assert results[0].status == "needs_user", results[0]
    assert "was closed" in results[0].message


def test_quota_and_audit_follow_the_redirect_to_the_publisher(env, monkeypatch):
    cfg, guard = env
    monkeypatch.setattr(fetcher, "resolve_url", lambda i: f"https://doi.org/{i}")

    def respond(url):
        if "doi.org" in url:
            return 200, f"<script>location.replace('{U}A4')</script>", "text/html"  # routes do not see HTTP redirects
        return 200, FULL_HTML, "text/html"

    results = asyncio.run(_site_context(lambda ctx: run_batch(cfg, ctx, ["10.1000/a4"], guard=guard), respond))
    assert results[0].status == "ok", results[0]
    assert guard.usage()["hosts"]["sciencedirect.com"]["last_24h"] == 1
    assert guard.audit_tail(1)[0]["host"] == "www.sciencedirect.com"


def test_doi_error_page_is_not_found_not_a_bot_check(env, monkeypatch):
    cfg, guard = env
    monkeypatch.setattr(fetcher, "resolve_url", lambda i: f"https://doi.org/{i}")
    page = "<html><title>Error: DOI Not Found</title><script src='https://challenges.cloudflare.com/x.js'></script></html>"
    events = []
    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, ["10.1000/missing"], guard=guard, emit=lambda k, **d: events.append(k)),
        lambda url: (404, page, "text/html")))
    assert results[0].status == "not_found", results[0]
    assert "blocked" not in events
    assert guard.usage()["hosts"]["doi.org"]["bot_checks_24h"] == 0


def test_unknown_doi_opens_no_page(env, monkeypatch):
    cfg, guard = env
    monkeypatch.setattr(fetcher, "resolve_url", lambda i: None)
    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, ["10.1000/missing"], guard=guard), lambda url: pytest.fail(f"opened {url}")))
    assert results[0].status == "not_found"
    assert guard.audit_tail(1) == []


def test_address_that_is_a_pdf_is_read_as_the_full_text(env):
    cfg, guard = env
    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, ["https://papers.example.org/a/paper.pdf"], guard=guard),
        lambda url: (200, _pdf_bytes(), "application/pdf")))
    assert results[0].status == "ok", results[0]
    assert results[0].method == "pdf"


def test_own_pdf_link_used_on_a_site_without_a_rule(env):
    cfg, guard = env
    html = ("<html><title>A longer paper title | Site</title><body><p>Short abstract.</p>"
            "<a href='/p/1.pdf'>PDF</a></body></html>")

    def respond(url):
        return (200, _pdf_bytes(), "application/pdf") if url.endswith(".pdf") else (200, html, "text/html")

    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, ["https://papers.example.org/p/1.html"], guard=guard), respond))
    assert results[0].status == "ok" and results[0].method == "pdf", results[0]
    assert results[0].title == "A longer paper title"


def test_pdf_link_labelled_with_its_own_address(env):
    cfg, guard = env
    pdf = "https://papers.example.org/p/2_paper.pdf"
    html = f"<html><title>Another paper title</title><body><p>Abstract.</p><a href='{pdf}'>{pdf}</a></body></html>"

    def respond(url):
        return (200, _pdf_bytes(), "application/pdf") if url == pdf else (200, html, "text/html")

    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, ["https://papers.example.org/p/2.html"], guard=guard), respond))
    assert results[0].status == "ok" and results[0].method == "pdf", results[0]


def test_other_publishers_go_on_after_a_user_action_times_out(env):
    cfg, guard = env
    other = "https://papers.example.org/p/full"

    def respond(url):
        return 200, CHECK_HTML if "sciencedirect" in url else FULL_HTML, "text/html"

    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, [U + "B1", U + "B2", other], guard=guard), respond))
    by = {m.id.rsplit("/", 1)[-1]: m.status for m in results}
    assert by == {"B1": "needs_user", "B2": "deferred", "full": "ok"}


def test_open_access_page_does_not_ask_for_a_sign_in(env):
    cfg, guard = env
    html = ("<html><title>Paper</title><body>Access through your institution. This is an open access article. "
            "No body selector here.</body></html>")
    events = []
    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, ["https://iopscience.iop.org/article/10.1088/x"], guard=guard,
                              emit=lambda k, **d: events.append(k)),
        lambda url: (200, html, "text/html")))
    assert results[0].status == "not_extracted", results[0]
    assert "open access" in results[0].message
    assert "blocked" not in events


def test_other_publishers_are_read_while_one_waits_for_the_user(env):
    cfg, guard = env
    cfg.max_wait_for_user_s = 20
    other = "https://papers.example.org/p/full"
    state, order = {}, []

    def respond(url):
        if "sciencedirect" in url:
            return 200, FULL_HTML if state.get("cleared") else CHECK_HTML, "text/html"
        return 200, PAGES["/science/article/pii/FULL2"][1], "text/html"  # another issue: env allows one per issue

    async def scenario(ctx):
        def emit(event, **d):
            if event == "result":
                order.append(d["meta"].id)
                if d["meta"].id == other:  # the user clears the check only after the other paper is stored
                    state["cleared"] = True

                    async def clear():
                        page = next(p for p in ctx.pages if "C1" in p.url)
                        await page.reload()
                    asyncio.get_running_loop().create_task(clear())
        return await run_batch(cfg, ctx, [U + "C1", other], guard=guard, emit=emit)

    results = asyncio.run(_site_context(scenario, respond))
    assert [m.status for m in results] == ["ok", "ok"], results
    assert order == [other, U + "C1"]  # read while the first publisher still waited


def test_cancel_stops_a_fetch_waiting_for_the_user(env):
    cfg, guard = env
    cfg.max_wait_for_user_s = 60
    control = Control()

    def emit(event, **d):
        if event == "blocked":
            asyncio.get_running_loop().call_later(0.5, setattr, control, "cancelled", True)

    results = asyncio.run(_site_context(
        lambda ctx: run_batch(cfg, ctx, [U + "C2"], guard=guard, emit=emit, control=control),
        lambda url: (200, CHECK_HTML, "text/html")))
    assert results[0].status == "needs_user", results[0]


def test_wait_ends_when_the_article_is_readable_and_reminds_meanwhile(env, monkeypatch):
    cfg, guard = env
    cfg.max_wait_for_user_s = 20
    monkeypatch.setattr(fetcher, "REMIND_EVERY_S", 0.5)
    state = {}
    # The check's title stays, but the article body has arrived in the page: nothing is in the way.
    stuck = FULL_HTML.replace("<title>Full one</title>", "<title>Just a moment...</title>")
    events = []

    def respond(url):
        return 200, stuck if state.get("done") else CHECK_HTML, "text/html"

    async def scenario(ctx):
        def emit(event, **d):
            events.append((event, d))
            if event == "still_blocked" and not state.get("done"):
                state["done"] = True
                asyncio.get_running_loop().create_task(next(p for p in ctx.pages if "D1" in p.url).reload())
        return await run_batch(cfg, ctx, [U + "D1"], guard=guard, emit=emit)

    results = asyncio.run(_site_context(scenario, respond))
    assert results[0].status == "ok", results[0]
    reminder = next(d for k, d in events if k == "still_blocked")
    assert reminder["title"] and reminder["waited_s"] >= 0


def test_check_that_clears_by_itself_costs_nothing(env):
    cfg, guard = env
    state = {"n": 0}

    def respond(url):
        state["n"] += 1
        return 200, CHECK_HTML if state["n"] == 1 else FULL_HTML, "text/html"

    async def scenario(ctx):
        task = asyncio.create_task(run_batch(cfg, ctx, [U + "E1"], guard=guard))
        await asyncio.sleep(0.5)
        for p in ctx.pages:
            if "E1" in p.url:
                await p.reload()  # the site's own script lets the visitor through
        return await task

    results = asyncio.run(_site_context(scenario, respond))
    assert results[0].status == "ok", results[0]
    usage = guard.usage()["hosts"]["sciencedirect.com"]
    assert usage["bot_checks_24h"] == 0 and usage["slowed_s"] == 0


def test_answered_check_slows_down_once_per_article_without_freezing(env):
    cfg, guard = env
    cfg.max_wait_for_user_s = 20
    state = {}

    def respond(url):
        if url == HOME:
            return 200, HOME_HTML, "text/html"
        return 200, FULL_HTML if state.get("n", 0) >= 2 else CHECK_HTML, "text/html"

    async def scenario(ctx):
        def emit(event, **d):
            if event == "blocked":
                state["n"] = state.get("n", 0) + 1

                async def later():
                    await asyncio.sleep(0.3)
                    await next(p for p in ctx.pages if p.url == d["url"]).goto(HOME)
                asyncio.get_running_loop().create_task(later())
        return await run_batch(cfg, ctx, [U + "E2"], guard=guard, emit=emit)

    results = asyncio.run(_site_context(scenario, respond))
    assert results[0].status == "ok", results[0]
    usage = guard.usage()["hosts"]["sciencedirect.com"]
    assert usage["bot_checks_24h"] == 1 and usage["cooldown_s"] == 0 and usage["slowed_s"] > 0


def test_visit_through_a_check_service_is_recorded_as_evidence(env):
    cfg, guard = env
    bounce = f"<script>location.replace('{U}E3')</script>"

    def respond(url):
        if "perfdrive" in url:
            return 200, bounce, "text/html"
        return 200, FULL_HTML, "text/html"

    async def scenario(ctx):
        return await run_batch(cfg, ctx, ["https://validate.perfdrive.com/x"], guard=guard)

    results = asyncio.run(_site_context(scenario, respond))
    assert results[0].status == "ok", results[0]
    assert results[0].extra.get("passed_through_check") is True
    assert guard.audit_tail(1)[0]["through_check"] is True
    assert guard.usage()["hosts"].get("sciencedirect.com", {}).get("bot_checks_24h", 0) == 0


def test_used_up_sign_ins_defer_instead_of_no_access(env):
    """Not knowing whether the session ended says nothing about access."""
    cfg, guard = env
    cfg.idp_entity_id = IDP
    for _ in range(3):
        guard.record_auto_sign_in("www.sciencedirect.com")
    events = []
    results = asyncio.run(_sso_context(
        lambda ctx: run_batch(cfg, ctx, [U + "SILENT"], guard=guard, emit=lambda k, **d: events.append(k)),
        True, closed_html=SILENT_HTML))
    assert results[0].status == "deferred" and "says nothing" in results[0].message, results[0]
    assert "auto_sign_in_skipped" in events and "blocked" not in events


def test_renewal_is_remembered_between_batches(env, monkeypatch):
    """Each new batch with a closed article must not spend another of the day's re-sign-ins."""
    cfg, guard = env
    cfg.idp_entity_id = IDP
    monkeypatch.setattr(fetcher, "AUTO_SIGN_IN_S", 1.5)
    calls = []

    async def both(ctx):
        await run_batch(cfg, ctx, [U + "CLOSED1"], guard=guard)
        return await run_batch(cfg, ctx, [U + "CLOSED2"], guard=guard)

    results = asyncio.run(_sso_context(both, True, licensed=False, closed_html=SILENT_HTML, calls=calls))
    assert results[0].status == "no_access" and "does not seem to license" in results[0].message, results
    assert len(calls) == 1


def test_recent_renewal_does_not_override_a_page_asking_to_sign_in(env):
    """The session can end within the hour: a page that asks to sign in is not a missing licence."""
    cfg, guard = env
    cfg.idp_entity_id = IDP
    guard.record_renewal("www.sciencedirect.com")
    events = []
    results = asyncio.run(_sso_context(
        lambda ctx: run_batch(cfg, ctx, [U + "EXPIRED"], guard=guard, emit=lambda k, **d: events.append(k)), True))
    assert results[0].status == "ok" and "auto_sign_in" in events, (events, results[0])


def test_publisher_not_recognising_the_institution_is_no_access_at_once(env, monkeypatch):
    """RSNA: its sign-in page says the institution is unknown; waiting for the user would not help."""
    cfg, guard = env
    cfg.idp_entity_id = IDP
    calls = []
    closed = ('<html><head><title>A closed paper</title></head><body><h1>Paper</h1><p>Abstract.</p>'
              '<a>LOGIN THROUGH YOUR LIBRARY/INSTITUTION</a></body></html>')
    error = ("<html><head><title>Error</title></head><body>Institutional Login. Your organization "
             f"({IDP}) has not been recognized. Please contact your administrator.</body></html>")

    async def with_ctx(fn):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            ctx = await browser.new_context()

            async def handle(route):
                sign_in = "/action/ssostart" in route.request.url
                if sign_in:
                    calls.append(route.request.url)
                await route.fulfill(status=200, body=error if sign_in else closed, content_type="text/html")

            await ctx.route("https://pubs.rsna.org/**", handle)
            try:
                return await fn(ctx)
            finally:
                await browser.close()

    events = []
    ids = ["https://pubs.rsna.org/doi/10.1148/one", "https://pubs.rsna.org/doi/10.1148/two"]
    results = asyncio.run(with_ctx(lambda ctx: run_batch(cfg, ctx, ids, guard=guard,
                                                         emit=lambda k, **d: events.append(k))))
    assert [m.status for m in results] == ["no_access", "no_access"], results
    assert all("does not recognise" in m.message for m in results)
    assert results[0].title == "A closed paper" and results[0].final_url == ids[0]
    assert len(calls) == 1 and "blocked" not in events


def test_page_that_fails_to_load_is_not_no_access(env, monkeypatch):
    """A browser error page (chrome-error://) or a blank tab says nothing about access."""
    import socket

    cfg, guard = env
    monkeypatch.setattr(fetcher, "RELOAD_AFTER_S", 0.05)
    with socket.socket() as sock:  # a port nobody listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    results = run(lambda ctx: run_batch(cfg, ctx, [f"http://127.0.0.1:{port}/article/1"], guard=guard))
    assert results[0].status == "deferred" and "did not load" in results[0].message, results[0]


def test_aborted_load_leaving_a_blank_tab_is_not_no_access(env):
    cfg, guard = env

    async def with_ctx(fn):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            ctx = await browser.new_context()
            await ctx.route("https://www.jstage.jst.go.jp/**", lambda route: route.abort("connectionreset"))
            try:
                return await fn(ctx)
            finally:
                await browser.close()

    results = asyncio.run(with_ctx(lambda ctx: run_batch(
        cfg, ctx, ["https://www.jstage.jst.go.jp/article/x/1/1/1_1/_article"], guard=guard)))
    assert results[0].status == "deferred" and "did not load" in results[0].message, results[0]


def test_address_that_starts_a_download_is_read_as_pdf(env, tmp_path):
    """Direct-download links (OJS and others) leave the tab on about:blank."""
    import http.server
    import threading

    import pymupdf

    cfg, guard = env
    doc = pymupdf.open()
    for _ in range(4):
        doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 800), BODY[:2500], fontsize=9)
    pdf = doc.tobytes()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="paper.pdf"')
            self.send_header("Content-Length", str(len(pdf)))
            self.end_headers()
            self.wfile.write(pdf)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/index.php/x/article/download/1/2"
        results = run(lambda ctx: run_batch(cfg, ctx, [url], guard=guard))
    finally:
        server.shutdown()
    assert results[0].status == "ok" and results[0].method == "pdf" and results[0].chars > 5000, results[0]


def test_sign_in_clicked_through_like_a_person(env, monkeypatch):
    """No direct link: article button, institution search, account tile, "stay signed in", back to the article."""
    from realread import signin

    cfg, guard = env
    cfg.institution = "Test University"
    monkeypatch.setattr(signin, "CLICK_PAUSE_S", (0.0, 0.01))
    art = "https://journal.example.org/article/1"
    state = {"signed": False}
    full = article("Open now", f"<article><h2>Introduction</h2><p>{BODY}</p></article>")
    closed = ("<html><head><title>Closed</title></head><body><p>Abstract.</p>"
              "<a href='https://wayfinder.example.net/'>Access through your institution</a></body></html>")
    wayf = ("<html><head><title>Find</title></head><body><input type=search oninput=\"document.getElementById('r')"
            ".hidden=!this.value\"><a id=r hidden href='https://login.microsoftonline.com/pick'>Test University</a>"
            "</body></html>")
    pick = ("<html><head><title>Pick an account</title></head><body><div role=button aria-label='Sign in with me@test.ac.uk'"
            " onclick=\"location='/kmsi'\">me@test.ac.uk</div></body></html>")
    kmsi = ("<html><head><title>Stay</title></head><body><div id=kmsiTitle>Stay signed in?</div><button id=idSIButton9 "
            "onclick=\"location='https://journal.example.org/return'\">Yes</button></body></html>")

    async def with_ctx(fn):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            ctx = await browser.new_context()

            async def journal(route):
                if route.request.url.endswith("/return"):
                    state["signed"] = True
                    await route.fulfill(status=200, content_type="text/html",
                                        body=f"<html><head><meta http-equiv=refresh content='0;url={art}'></head></html>")
                else:
                    await route.fulfill(status=200, content_type="text/html", body=full if state["signed"] else closed)

            async def page_of(route, body):
                await route.fulfill(status=200, content_type="text/html", body=body)

            await ctx.route("https://journal.example.org/**", journal)
            await ctx.route("https://wayfinder.example.net/**", lambda r: page_of(r, wayf))
            await ctx.route("https://login.microsoftonline.com/**",
                            lambda r: page_of(r, kmsi if r.request.url.endswith("/kmsi") else pick))
            try:
                return await fn(ctx)
            finally:
                await browser.close()

    events = []
    results = asyncio.run(with_ctx(lambda ctx: run_batch(cfg, ctx, [art], guard=guard,
                                                         emit=lambda k, **d: events.append(k))))
    assert results[0].status == "ok", (events, results[0])
    assert "auto_sign_in" in events and "blocked" not in events


def test_password_page_goes_to_the_user_without_typing(env, monkeypatch):
    cfg, guard = env
    cfg.institution = "Test University"
    art = "https://journal.example.org/article/2"
    closed = ("<html><head><title>Closed</title></head><body><p>Abstract.</p>"
              "<a href='https://idp.example.net/login'>Access through your institution</a></body></html>")
    typed = []

    async def with_ctx(fn):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            ctx = await browser.new_context()

            async def idp(route):
                if route.request.method == "POST":
                    typed.append(route.request.post_data)
                await route.fulfill(status=200, content_type="text/html",
                                    body="<html><title>Sign in</title><form method=post><input type=password name=p>"
                                         "<button>Sign in</button></form></html>")

            async def journal(route):
                await route.fulfill(status=200, content_type="text/html", body=closed)

            await ctx.route("https://journal.example.org/**", journal)
            await ctx.route("https://idp.example.net/**", idp)
            try:
                return await fn(ctx)
            finally:
                await browser.close()

    events = []
    results = asyncio.run(with_ctx(lambda ctx: run_batch(cfg, ctx, [art], guard=guard,
                                                         emit=lambda k, **d: events.append(k))))
    assert results[0].status == "needs_login" and "blocked" in events and not typed, (events, results[0])


def test_open_paper_is_read_without_opening_a_browser(env, monkeypatch):
    """Plain HTTP for a paper anyone can open: the browser getter is never called."""
    import http.server
    import threading

    from realread import direct

    cfg, guard = env
    cfg.direct_open_access = True
    html = article("Open paper", f"<article><h2>Intro</h2><p>{BODY}</p></article>").encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(direct, "OPEN_HOSTS", ("127.0.0.1",))

    async def no_browser():
        raise AssertionError("the browser must not be opened")

    try:
        results = asyncio.run(run_batch(cfg, no_browser, [f"http://127.0.0.1:{server.server_port}/article/1"], guard=guard))
    finally:
        server.shutdown()
    assert results[0].status == "ok" and results[0].extra["route"] == "direct", results[0]
    assert results[0].title == "Open paper"
    assert guard.audit_tail(1)[0]["route"] == "direct"


def test_sign_in_error_on_the_way_is_retried_once(env, monkeypatch):
    """IEEE now and then reports that the university cannot log in; going back and signing in again works."""
    cfg, guard = env
    cfg.idp_entity_id = IDP
    state = {"signed": False, "calls": 0}

    async def with_ctx(fn):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            ctx = await browser.new_context()

            async def article_page(route):
                await route.fulfill(status=200, content_type="text/html",
                                    body=FULL_HTML if state["signed"] else PREVIEW_HTML)

            async def login(route):
                from urllib.parse import parse_qs, urlparse
                state["calls"] += 1
                if state["calls"] == 1:
                    await route.fulfill(status=200, content_type="text/html",
                                        body="<html><title>Error</title><body>We are unable to log in through your "
                                             "institution right now.</body></html>")
                    return
                state["signed"] = True
                back = parse_qs(urlparse(route.request.url).query)["appReturnURL"][0]
                await route.fulfill(status=200, content_type="text/html",
                                    body=f"<html><head><meta http-equiv='refresh' content='0;url={back}'></head></html>")

            await ctx.route("https://www.sciencedirect.com/**", article_page)
            await ctx.route("https://auth.elsevier.com/**", login)
            try:
                return await fn(ctx)
            finally:
                await browser.close()

    events = []
    results = asyncio.run(with_ctx(lambda ctx: run_batch(cfg, ctx, [U + "FLAKY"], guard=guard,
                                                         emit=lambda k, **d: events.append(k))))
    assert results[0].status == "ok" and state["calls"] == 2, (events, results[0])
    assert "auto_sign_in_again" in events and "blocked" not in events
    assert events.count("auto_sign_in") == 1  # one attempt in the books

