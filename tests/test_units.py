from __future__ import annotations

import pytest

from realread import detect, library, proxy, sites, sso
from realread.config import Config
from realread.policy import HARD_LIMITS, Guard, Limits, QuotaExhausted, effective_limits, host_key


def make_cfg(tmp_path, **kw) -> Config:
    return Config(profile_dir=str(tmp_path / "profile"), library_dir=str(tmp_path / "lib"), chrome_path="x", **kw)


# -- ids ------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("10.1109/CVPR.2016.90", "10.1109/cvpr.2016.90"),
    ("https://doi.org/10.1038/nature14539", "10.1038/nature14539"),
    ("doi:10.1126/science.1127647.", "10.1126/science.1127647"),
    ("https://ieeexplore.ieee.org/document/7780459", "https://ieeexplore.ieee.org/document/7780459"),
])
def test_normalize_id(raw, expected):
    assert library.normalize_id(raw) == expected


def test_key_for_is_path_safe():
    assert "/" not in library.key_for("10.1000/a/../b")
    assert ".." in library.key_for("10.1000/a/../b")  # dots kept, but no separators
    assert library.key_for("10.1000/a/../b") == "10.1000_a_.._b"


# -- detection ------------------------------------------------------------------------
def test_bot_check_titles_and_markers():
    assert detect.is_bot_check("Just a moment...", "")
    assert detect.is_bot_check("请稍候…", "")
    assert detect.is_bot_check("Article", "<script src='https://challenges.cloudflare.com/x'></script>")
    # Cloudflare injects this script into normal pages too: not a check.
    assert not detect.is_bot_check("Article", "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'>")


def test_institution_login_hints():
    assert detect.needs_institution_login("Access through your institution\nPurchase PDF")
    assert detect.needs_institution_login("ALREADY A SUBSCRIBER? SIGN IN AS AN INDIVIDUAL OR VIA YOUR INSTITUTION")
    assert not detect.needs_institution_login("Access provided by University X. Access through your institution")
    assert not detect.needs_institution_login("FULL ACCESS\n... via your institution")
    assert not detect.needs_institution_login("Access by: University X. Access through your institution")
    # RSNA: "full access" inside a sentence is an offer, and its button names the library too.
    assert detect.needs_institution_login("Subscribe for full access. LOGIN THROUGH YOUR LIBRARY/INSTITUTION")
    assert detect.needs_institution_login("Get full access to this article. Access through your organization")
    # A personal-account menu is no sign-in prompt (it shows with institutional access too); an
    # ended session without a prompt is handled by renewing the session, not by wording.
    assert not detect.needs_institution_login("Login / Register\nDownload PDF")


def test_login_page():
    assert detect.is_login_page("https://idp.uni.ac.uk/idp/profile/SAML2", on_publisher=False)
    assert not detect.is_login_page("https://ieeexplore.ieee.org/login", on_publisher=True)


@pytest.mark.parametrize("url,listing", [
    ("https://www.sciencedirect.com/science/article/pii/S0893608014002135", False),
    ("https://ieeexplore.ieee.org/document/7780459", False),
    ("https://link.springer.com/article/10.1007/s11263-015-0816-y", False),
    ("https://pubs.acs.org/jcisd8/article/59/8/3370/863796/Analyzing", False),
    ("https://www.mdpi.com/1424-8220/20/1/1", False),
    ("https://www.sciencedirect.com/search?qs=deep+learning", True),
    ("https://ieeexplore.ieee.org/xpl/tocresult.jsp?isnumber=1&q=x", True),
    ("https://onlinelibrary.wiley.com/toc/14679868/1996/58/1", True),
    ("https://www.nature.com/nature/volumes/", True),
    ("https://www.science.org/", True),
    ("https://link.springer.com/journal/11263", True),
])
def test_listing_urls_refused(url, listing):
    assert detect.is_listing_url(url) is listing


# -- sites ----------------------------------------------------------------------------
def test_site_key_and_proxy_hosts():
    assert sites.site_key("www.sciencedirect.com") == "sciencedirect.com"
    assert sites.site_key("ieeexplore-ieee-org.ezproxy.lib.example.ac.uk") == "ieeexplore.ieee.org"
    assert sites.site_key("example.org") is None


def test_pdf_rules():
    assert sites.pdf_rule_url("https://ieeexplore.ieee.org/document/7780459") == \
        "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=7780459&ref="
    assert sites.pdf_rule_url("https://dl.acm.org/doi/10.1145/2939672.2939785") == \
        "https://dl.acm.org/doi/pdf/10.1145/2939672.2939785"
    assert sites.pdf_rule_url("https://www.science.org/doi/full/10.1126/science.1127647") == \
        "https://www.science.org/doi/pdf/10.1126/science.1127647"
    assert sites.pdf_rule_url("https://example.org/a") is None


# -- proxy ----------------------------------------------------------------------------
def test_proxy_parse_and_rewrite(tmp_path):
    assert proxy.parse_link("https://login.ezproxy.uni.edu/login?url=https://x.org/a") == \
        ("https://login.ezproxy.uni.edu/login?url=", "")
    assert proxy.parse_link("https://www-sciencedirect-com.libproxy.ucl.ac.uk/science/article/pii/S1") == \
        ("", "libproxy.ucl.ac.uk")
    assert proxy.parse_link("https://ieeexplore.ieee.org/document/1") == ("", "")
    cfg = make_cfg(tmp_path, ezproxy_suffix="ezproxy.lib.example.ac.uk")
    assert proxy.rewrite(cfg, "https://pubs.acs-x.org/doi/1?a=b") == \
        "https://pubs-acs--x-org.ezproxy.lib.example.ac.uk/doi/1?a=b"
    assert proxy.detect_from_urls(["https://www-nature-com.ezp.lib.cam.ac.uk/a", "chrome://newtab/"]) == \
        ("", "ezp.lib.cam.ac.uk")


# -- policy ---------------------------------------------------------------------------
def test_config_cannot_loosen_limits(tmp_path):
    cfg = make_cfg(tmp_path, min_interval_s=5, max_per_host_day=1000, max_total_day=50)
    lim, ignored = effective_limits(cfg)
    assert lim.min_interval_s == HARD_LIMITS.min_interval_s
    assert lim.per_host_day == HARD_LIMITS.per_host_day
    assert lim.total_day == 50  # a cap across all publishers can only be added by the config
    assert len(ignored) == 2


def test_host_key():
    assert host_key("www.sciencedirect.com") == "sciencedirect.com"
    assert host_key("pdf.sciencedirectassets.com") == "sciencedirectassets.com"
    assert host_key("www.lib.example.ac.uk") == "example.ac.uk"


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_guard_interval_hourly_and_daily(tmp_path):
    clock = Clock()
    g = Guard(make_cfg(tmp_path), Limits(min_interval_s=20, per_host_hour=3, per_host_day=4, total_day=10,
                                        per_issue_day=1), clock=clock)
    assert g.plan("ieeexplore.ieee.org").ready_at <= clock.t
    g.record_request("ieeexplore.ieee.org")
    p = g.plan("ieeexplore.ieee.org")
    assert 20 <= p.ready_at - clock.t <= 30 and p.reason == "interval"
    # another publisher is independent
    assert g.plan("www.nature.com").ready_at <= clock.t
    for _ in range(2):
        clock.t += 100
        g.record_request("ieeexplore.ieee.org")
    clock.t += 100
    p = g.plan("ieeexplore.ieee.org")
    assert p.reason == "hourly_cap" and p.ready_at > clock.t
    clock.t += 3600
    g.record_request("ieeexplore.ieee.org")
    with pytest.raises(QuotaExhausted):
        g.plan("ieeexplore.ieee.org")
    # quotas survive a new Guard (persisted)
    g2 = Guard(make_cfg(tmp_path), g.limits, clock=clock)
    with pytest.raises(QuotaExhausted):
        g2.plan("ieeexplore.ieee.org")
    clock.t += 86400
    assert g2.plan("ieeexplore.ieee.org").ready_at <= clock.t + 30


def test_no_built_in_cap_across_publishers(tmp_path):
    assert HARD_LIMITS.total_day == 0 and effective_limits(make_cfg(tmp_path))[0].total_day == 0
    clock = Clock()
    g = Guard(make_cfg(tmp_path), Limits(0, 1000, 1000, 0, 1000), clock=clock)
    for i in range(300):
        g.record_request(f"p{i}.example.org")
    g.plan("q.example.org")  # 300 papers across publishers in a day: no quota raised
    assert g.usage()["remaining_today"] is None


def test_guard_total_cap(tmp_path):
    clock = Clock()
    g = Guard(make_cfg(tmp_path), Limits(0, 100, 100, 2, 10), clock=clock)
    g.record_request("a.example.org")
    g.record_request("b.example.org")
    with pytest.raises(QuotaExhausted) as e:
        g.plan("c.example.org")
    assert e.value.scope == "all publishers"


def test_guard_slowdown_and_cooldown(tmp_path):
    clock = Clock()
    g = Guard(make_cfg(tmp_path), Limits(20, 100, 100, 100, 10), clock=clock)
    assert g.record_block("www.sciencedirect.com", "answered") == 0  # an answered check: slow down only
    g.record_request("www.sciencedirect.com")
    p = g.plan("www.sciencedirect.com")
    assert p.reason == "slowdown" and p.ready_at - clock.t >= 40
    # Answered checks spread over the day never freeze a publisher ...
    for _ in range(7):
        clock.t += 2 * 3600
        assert g.record_block("www.sciencedirect.com", "answered") == 0
    # ... but three within an hour mean the site keeps asking: a short pause.
    assert g.record_block("www.sciencedirect.com", "answered") == 0
    until = g.record_block("www.sciencedirect.com", "answered")
    assert until - clock.t == 1800
    assert g.plan("www.sciencedirect.com").reason == "cooldown"
    # HTTP 429 cools down at once, and for as long as the site asks
    assert g.record_block("www.nature.com", "rate_limited") - clock.t == 1800
    assert g.record_block("www.acs.org", "rate_limited", retry_after_s=7200) - clock.t == 7200


def test_unanswered_checks_cool_down_for_hours_not_days(tmp_path):
    clock = Clock()
    g = Guard(make_cfg(tmp_path), Limits(20, 100, 100, 100, 10), clock=clock)
    assert g.record_block("iopscience.iop.org", "unanswered") == 0  # the user may just be away
    assert g.record_block("iopscience.iop.org", "unanswered") - clock.t == 1800
    for _ in range(8):
        until = g.record_block("iopscience.iop.org", "unanswered")
    assert until - clock.t == 4 * 3600
    # 429 keeps its longer limit
    for _ in range(9):
        until = g.record_block("www.nature.com", "rate_limited")
    assert until - clock.t == 24 * 3600


def test_guard_burst_pause(tmp_path):
    clock = Clock()
    g = Guard(make_cfg(tmp_path), Limits(1, 100, 100, 100, 10), clock=clock)
    for _ in range(8):
        clock.t += 10
        g.record_request("www.nature.com")
    p = g.plan("www.nature.com")
    assert p.reason == "burst_pause" and p.ready_at - clock.t >= 120


def test_issue_cap(tmp_path):
    g = Guard(make_cfg(tmp_path), Limits(0, 100, 100, 100, 2), clock=Clock())
    assert g.issue_allowed("nature|521|7553")
    g.record_issue("nature|521|7553")
    g.record_issue("nature|521|7553")
    assert not g.issue_allowed("nature|521|7553")
    assert g.issue_allowed("nature|521|7554")


def test_audit_log(tmp_path):
    g = Guard(make_cfg(tmp_path), clock=Clock())
    g.audit(id="10.1/x", host="h", status="ok")
    assert g.audit_tail(5)[-1]["id"] == "10.1/x"


def test_event_consumers_accept_all_events(capsys):
    from realread.cli import _printer
    from realread.jobs import Job
    from realread.library import Meta

    events = [
        ("wait", {"host": "h", "secs": 120, "reason": "burst_pause"}),
        ("blocked", {"kind": "bot", "host": "h", "url": "u", "mins": 30}),
        ("unblocked", {"kind": "bot", "host": "h"}),
        ("result", {"meta": Meta(id="x", status="ok", chars=10, method="html"), "cached": False}),
        ("quota", {"scope": "h", "resets_at": 1_000_000.0}),
        ("cooldown", {"host": "h", "until": 1_000_000.0}),
        ("proxy_skipped", {"host": "h"}),
        ("stopped", {"why": "cancelled"}),
    ]
    emit = _printer(verbose=True)
    job = Job(id="j", ids=["x"])
    for name, data in events:
        emit(name, **data)
        job.on_event(name, **data)
    out = capsys.readouterr().out
    assert "h" in out and "[ok] x" in out
    job.on_event("blocked", kind="login", host="h", url="u", mins=1)
    s = job.summary()
    assert s["state"] == "waiting_for_user" and "action_required" in s


# -- sso ------------------------------------------------------------------------------
IDP = "https://idp.example.ac.uk/shibboleth"


def test_idp_from_publisher_sso_links():
    assert sso.idp_from_url(
        "https://www.tandfonline.com/action/ssostart?idp=https%3A%2F%2Fidp.example.ac.uk%2Fshibboleth"
        "&redirectUri=%2Fdoi%2Ffull%2F10.1080%2F1&federationId=http%3A%2F%2Fukfederation.org.uk"
    ) == (IDP, "http://ukfederation.org.uk")
    assert sso.idp_from_url(
        "https://auth.elsevier.com/ShibAuth/institutionLogin?entityID=https%3A%2F%2Fidp.example.ac.uk%2Fshibboleth"
        "&appReturnURL=https%3A%2F%2Fwww.sciencedirect.com%2Fx") == (IDP, "")
    assert sso.idp_from_url("https://ieeexplore.ieee.org/servlet/wayf.jsp?entityId=https://idp.example.ac.uk/"
                            "shibboleth&url=https%3A%2F%2Fieeexplore.ieee.org%2FXplore%2Fhome.jsp") == (IDP, "")
    assert sso.idp_from_url("https://example.org/?idp=1") == ("", "")


def test_wayfless_links_match_observed_publisher_formats():
    ieee = sso.wayfless_url("https://ieeexplore.ieee.org/document/7780459", IDP)
    assert ieee == ("https://ieeexplore.ieee.org/servlet/wayf.jsp?entityId=https://idp.example.ac.uk/shibboleth"
                    "&url=https%3A%2F%2Fieeexplore.ieee.org%2Fdocument%2F7780459")
    sd = sso.wayfless_url("https://www.sciencedirect.com/science/article/pii/S0893608014002135", IDP)
    assert sd.startswith("https://auth.elsevier.com/ShibAuth/institutionLogin?entityID=https%3A%2F%2Fidp.example")
    assert sso.idp_from_url(sd) == (IDP, "")
    tf = sso.wayfless_url("https://www.tandfonline.com/doi/full/10.1080/01621459.2017.1285773", IDP,
                          "http://ukfederation.org.uk")
    assert tf.startswith("https://www.tandfonline.com/action/ssostart?idp=")
    assert "redirectUri=%2Fdoi%2Ffull%2F10.1080%2F01621459.2017.1285773" in tf
    sp = sso.wayfless_url("https://link.springer.com/article/10.1007/x", IDP)
    assert sp == ("https://fsso.springer.com/saml/login?idp=https%3A%2F%2Fidp.example.ac.uk%2Fshibboleth"
                  "&targetUrl=https%3A%2F%2Flink.springer.com%2Farticle%2F10.1007%2Fx")
    assert sso.idp_from_url(sp) == (IDP, "")
    nat = sso.wayfless_url("https://www.nature.com/articles/nature14539", IDP)
    assert nat.startswith("https://sp.nature.com/saml/login?idp=https%3A%2F%2Fidp.example")
    assert nat.endswith("&targetUrl=https%3A%2F%2Fwww.nature.com%2Farticles%2Fnature14539")
    assert sso.wayfless_url("https://example.org/article/1", IDP) is None


def test_openathens_and_rsna_links():
    from urllib.parse import parse_qs, urlparse
    art = "https://iopscience.iop.org/article/10.1088/1361-6560/ae0efc"
    iop = urlparse(sso.wayfless_url(art, IDP))
    assert iop.netloc == "connect.openathens.net" and iop.path.startswith("/iop.com/") and iop.path.endswith("/login")
    assert parse_qs(iop.query) == {"entity": [IDP], "target": [art]}
    spie = urlparse(sso.wayfless_url("https://www.spiedigitallibrary.org/conference-proceedings-of-spie/1/x.full", IDP))
    assert spie.path.startswith("/spie.org/")
    assert "/action/ssostart?idp=" in sso.wayfless_url("https://pubs.rsna.org/doi/10.1148/x", IDP)


def test_institution_rejected_only_on_short_pages():
    page = "Institutional Login Your organization (https://idp.example/shibboleth) has not been recognized."
    assert detect.institution_rejected(page)
    assert not detect.institution_rejected(page + " text" * 1000)
    assert not detect.institution_rejected("Institutional Login Find your institution")


def test_spie_is_a_known_publisher_read_by_the_generic_extractor():
    from realread.sites import BODY_SELECTORS, site_key
    key = site_key("www.spiedigitallibrary.org")
    assert key and BODY_SELECTORS[key] == []


def test_renewal_is_remembered_for_an_hour(tmp_path):
    from realread.config import Config
    from realread.policy import Guard, Limits
    now = [100_000.0]
    guard = Guard(Config(profile_dir=str(tmp_path / "p"), library_dir=str(tmp_path / "l")),
                  Limits(), clock=lambda: now[0])
    assert not guard.renewed_recently("pubs.example.org")
    guard.record_renewal("pubs.example.org")
    now[0] += 3599
    assert guard.renewed_recently("www.example.org")  # the same publisher
    now[0] += 2
    assert not guard.renewed_recently("pubs.example.org")


def test_read_is_repeated_when_the_page_redirected_meanwhile():
    import asyncio

    from playwright.async_api import Error as PlaywrightError

    from realread import fetcher

    class FakePage:
        async def wait_for_load_state(self, *a, **k):
            pass

    calls = []

    async def read():
        calls.append(1)
        if len(calls) == 1:
            raise PlaywrightError("Page.evaluate: Execution context was destroyed, most likely because of a navigation")
        return "text"

    async def other():
        raise PlaywrightError("Target closed")

    fetcher.POLL_S, poll = 0.01, fetcher.POLL_S
    try:
        assert asyncio.run(fetcher._settled_read(FakePage(), fetcher.Control(), read)) == "text" and len(calls) == 2
        with pytest.raises(PlaywrightError):
            asyncio.run(fetcher._settled_read(FakePage(), fetcher.Control(), other))
    finally:
        fetcher.POLL_S = poll


def test_auto_sign_in_is_spaced_not_rationed(tmp_path):
    from realread.policy import AUTO_SIGN_IN_SPACING_S, AUTO_SIGN_INS_PER_DAY
    now = [100_000.0]
    host = "ieeexplore.ieee.org"
    g = Guard(make_cfg(tmp_path), clock=lambda: now[0])
    for _ in range(5):  # more than the old limit of 3 a day
        assert g.auto_sign_in_allowed(host)
        g.record_auto_sign_in(host)
        assert not g.auto_sign_in_allowed(host)
        assert g.auto_sign_in_resets_at(host) == now[0] + AUTO_SIGN_IN_SPACING_S
        now[0] += AUTO_SIGN_IN_SPACING_S
    assert AUTO_SIGN_IN_SPACING_S * AUTO_SIGN_INS_PER_DAY <= 86400  # the ceiling only stops a failing loop


# -- generic institution access -------------------------------------------------------
def test_proxy_templates(tmp_path):
    url = "https://www.sciencedirect.com/science/article/pii/S1?via=x&y=1"
    enc = "https%3A%2F%2Fwww.sciencedirect.com%2Fscience%2Farticle%2Fpii%2FS1%3Fvia%3Dx%26y%3D1"
    assert proxy.expand("http://proxy.lib.example.ac.uk/login?url=$@", url) == \
        "http://proxy.lib.example.ac.uk/login?url=" + url
    assert proxy.expand("http://zorac.aub.aau.dk/login?qurl=$@", url) == "http://zorac.aub.aau.dk/login?qurl=" + enc
    assert proxy.expand(proxy.openathens_template("kcl.ac.uk"), url) == \
        "https://go.openathens.net/redirector/kcl.ac.uk?url=" + enc
    # the template wins over the older EZproxy fields
    cfg = make_cfg(tmp_path, proxy_template="https://p.uni.edu/login?url=$@", ezproxy_suffix="ezp.uni.edu")
    assert proxy.rewrite(cfg, url) == "https://p.uni.edu/login?url=" + url
    assert proxy.proxy_host(cfg) == "p.uni.edu" and proxy.proxied(cfg)
    assert not proxy.proxied(make_cfg(tmp_path))


def test_proxy_not_configured_page(tmp_path):
    cfg = make_cfg(tmp_path, proxy_template="https://p.uni.edu/login?url=$@")
    needhost = ("To allow https://www.example-publisher.com to be used, the following line must be added "
                "to the ezproxy.cfg file: Host www.example-publisher.com")
    assert proxy.not_configured_page("https://p.uni.edu/login?url=https://x", needhost, cfg)
    # the same words on a publisher page are not the proxy's error page
    assert not proxy.not_configured_page("https://www.example-publisher.com/a", needhost, cfg)
    assert not proxy.not_configured_page("https://p.uni.edu/login", "Please sign in", cfg)
    assert not proxy.not_configured_page("https://p.uni.edu/x", needhost, make_cfg(tmp_path))


MDQ_SAMPLE = [
    {"title": "University of Exampleton", "entityID": "https://idp.example.ac.uk/shibboleth",
     "domain": "example.ac.uk", "registrationAuthority": ["http://ukfederation.org.uk"], "type": "idp",
     "hidden": "false"},
    {"title": "University of Exampleton - Test", "entityID": "https://t.example.ac.uk/idp", "domain": "example.ac.uk",
     "type": "idp", "hidden": "false"},
    {"title": "Exampleton hidden", "entityID": "https://h.example.ac.uk/idp", "type": "idp", "hidden": "true"},
]
PROXY_SAMPLE = [
    {"name": "University of Exampleton", "url": "http://proxy.lib.example.ac.uk/login?url=$@", "country": "UK"},
    {"name": "Exampleton Business School Library", "url": "https://ezp.sbs.example/login?url=$@", "country": "UK"},
    {"name": "Harvard University", "url": "http://ezp-prod1.hul.harvard.edu/login?url=$@", "country": "US"},
]


def test_institution_lookup_merges_sources():
    from realread import institutions

    found = institutions.combine("exampleton", MDQ_SAMPLE, PROXY_SAMPLE)
    assert [i.name for i in found] == ["University of Exampleton", "Exampleton Business School Library"]
    uni = found[0]
    assert uni.entity_id == "https://idp.example.ac.uk/shibboleth"
    assert uni.federation == "http://ukfederation.org.uk"
    assert uni.proxy_template == "http://proxy.lib.example.ac.uk/login?url=$@"
    assert not found[1].entity_id
    assert institutions.combine("univ exampl", [], PROXY_SAMPLE)[0].name == "University of Exampleton"
    assert institutions.combine("nowhere", [], PROXY_SAMPLE) == []


def test_libproxy_cache(tmp_path, monkeypatch):
    from realread import institutions

    calls = []
    monkeypatch.setattr(institutions, "_cache_file", lambda: tmp_path / "db.json")
    monkeypatch.setattr(institutions, "_get_json", lambda url, timeout=20: calls.append(url) or PROXY_SAMPLE + [
        {"name": "No placeholder", "url": "https://x.example/"}])
    assert len(institutions.libproxy_entries()) == 3
    assert len(institutions.libproxy_entries()) == 3 and len(calls) == 1  # second call served from the cache

    def offline(url, timeout=20):
        raise OSError("offline")

    monkeypatch.setattr(institutions, "_get_json", offline)
    assert len(institutions.libproxy_entries(refresh=True)) == 3  # stale copy when offline


def test_browser_proxy_args(tmp_path):
    from realread.browser import chrome_args

    assert not any(a.startswith("--proxy") for a in chrome_args(make_cfg(tmp_path)))
    args = chrome_args(make_cfg(tmp_path, browser_proxy="socks5://127.0.0.1:1080"))
    assert "--proxy-server=socks5://127.0.0.1:1080" in args
    args = chrome_args(make_cfg(tmp_path, browser_proxy="http://p:8080", browser_proxy_pac="https://uni.edu/proxy.pac"))
    assert "--proxy-pac-url=https://uni.edu/proxy.pac" in args and not any(a.startswith("--proxy-server") for a in args)


def test_choose_institution_flow(tmp_path, monkeypatch, capsys):
    from realread import cli, institutions

    monkeypatch.setattr(institutions, "search", lambda q, refresh=False: institutions.combine(q, MDQ_SAMPLE, PROXY_SAMPLE))
    monkeypatch.setattr(institutions, "openathens_available", lambda domain, timeout=15: False)
    cfg = make_cfg(tmp_path, ezproxy_suffix="old.example")
    assert cli._choose_institution(cfg, "exampleton", pick=1, interactive=False)
    assert cfg.institution == "University of Exampleton"
    assert cfg.idp_entity_id == "https://idp.example.ac.uk/shibboleth"
    assert not proxy.proxied(cfg)  # direct is the default
    assert cli._choose_institution(cfg, "exampleton", pick=1, use="proxy", interactive=False)
    assert cfg.proxy_template == "http://proxy.lib.example.ac.uk/login?url=$@"
    with pytest.raises(SystemExit):
        cli._choose_institution(cfg, "exampleton", pick=1, use="openathens", interactive=False)
    assert not cli._choose_institution(cfg, "nowhere", interactive=False)
    assert "libproxy-db" in capsys.readouterr().out


def test_pdf_bot_page_detection():
    from realread.extract import _is_bot_page
    assert _is_bot_page(b'<!DOCTYPE html><html><head><title>Just a moment...</title></head><body></body></html>')
    assert not _is_bot_page(b"<html><head><title>Article</title></head><body>no pdf here</body></html>")


def test_search_skips_front_matter_and_chars_match_stored_text(tmp_path):
    cfg = make_cfg(tmp_path)
    meta = library.Meta(id="10.1/x", status="ok", title="Region Proposal Networks", method="html")
    library.save(cfg, meta, text="Intro.\n\nWe propose Region Proposal Networks here.")
    hits = library.search(cfg, "region proposal")
    assert len(hits) == 1 and "We propose" in hits[0]["snippet"]
    assert meta.chars == len(library.load_text(cfg, "10.1/x"))
    assert library.load_meta(cfg, "10.1/x").chars == meta.chars


def test_wiley_access_denied_from_data_layer():
    url = "https://onlinelibrary.wiley.com/doi/10.1002/sim.1047"
    closed = '{"doi":"10.1002/sim.1047","access":"no","type":"article"},"group":{"access":"no","type":"issue"}'
    open_ = '{"doi":"10.1002/sim.1186","access":"yes","type":"article"},"group":{"access":"no","type":"issue"}'
    assert sites.access_denied(url, closed)
    assert not sites.access_denied(url, open_)  # the issue entry does not count
    assert not sites.access_denied("https://www.nature.com/articles/x", closed)


def test_same_article_across_publisher_redirects():
    from realread.fetcher import same_article

    assert same_article("https://www.sciencedirect.com/science/article/pii/S0925231206000385?via%3Dihub",
                        "https://linkinghub.elsevier.com/retrieve/pii/S0925231206000385")
    assert same_article("https://onlinelibrary.wiley.com/doi/full/10.1002/sim.1186",
                        "https://onlinelibrary.wiley.com/doi/10.1002/sim.1186")
    assert same_article("https://ieeexplore.ieee.org/document/7780459/", "https://ieeexplore.ieee.org/document/7780459")
    assert not same_article("https://onlinelibrary.wiley.com/doi/10.1002/sim.1047",
                            "https://onlinelibrary.wiley.com/doi/10.1002/sim.1186")
    assert not same_article("https://idp.example.ac.uk/", "https://idp.example.ac.uk/")


def test_unavailable_pages():
    assert detect.is_unavailable("IEEE Xplore", "IEEE Xplore is undergoing scheduled maintenance. We'll be back soon.")
    assert detect.is_unavailable("", "", 503)
    assert not detect.is_unavailable("Deep learning | Nature", "Access through your institution", 200)
    article = "Predictive maintenance. " + "Machines under maintenance are temporarily unavailable. " * 100
    assert not detect.is_unavailable("A survey of predictive maintenance", article, 200)


# -- sign-in and access reporting -----------------------------------------------------------------------


def test_resolve_url_distinguishes_missing_from_unreachable(monkeypatch):
    import io
    import urllib.error

    from realread import fetcher

    def missing(*a, **k):
        raise urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(fetcher.urllib.request, "urlopen", missing)
    assert fetcher.resolve_url("10.1000/none") is None

    def unreachable(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(fetcher.urllib.request, "urlopen", unreachable)
    assert fetcher.resolve_url("10.1000/x") == "https://doi.org/10.1000/x"
    assert fetcher.resolve_url("arxiv:2106.10053") is None
    assert fetcher.resolve_url("https://a.example/p") == "https://a.example/p"


def test_elsevier_redirect_host_shares_the_publisher_quota():
    from realread.policy import host_key

    assert host_key("linkinghub.elsevier.com") == host_key("www.sciencedirect.com")


def test_page_wording_helpers():
    from realread import detect

    assert detect.clean_title("PET Image Reconstruction | IEEE Journals & Maga…") == "PET Image Reconstruction"
    assert detect.clean_title("A | B") == "A | B"
    assert detect.says_open_access("This is an open\n access article under CC BY")
    assert not detect.says_open_access("Our journal offers open access options")
    assert detect.is_doi_not_found("https://doi.org/10.1/x", "Error: DOI Not Found")
    assert not detect.is_doi_not_found("https://a.example/x", "Error: DOI Not Found")
    assert detect.is_home_page("https://iopscience.iop.org/")
    assert not detect.is_home_page("https://iopscience.iop.org/article/1")


def test_job_status_lists_what_is_still_queued_and_names_the_page():
    from realread.jobs import Job
    from realread.library import Meta

    job = Job(id="j", ids=["10.1000/A", "10.1000/b"])
    job.on_event("result", meta=Meta(id="10.1000/a", status="ok"), cached=False)
    job.on_event("blocked", kind="bot", host="iop.org", url="https://iop.org/x", mins=30)
    out = job.summary()
    assert out["queued"] == {"count": 1, "next": ["10.1000/b"]}
    assert "a bot check (not a sign-in)" in out["action_required"]
    job.on_event("cooldown", host="iop.org", until=1_800_000_000)
    assert "until 20" in job.notes[-1]


def test_close_chrome_quits_again_when_chrome_restarts_for_an_update(monkeypatch, tmp_path):
    import asyncio

    from realread import browser
    from realread.config import Config

    cfg = Config(profile_dir=str(tmp_path / "p"), library_dir=str(tmp_path / "lib"), chrome_path="x")
    # Up; the first quit brings it down, then Chrome restarts itself; the second quit is final.
    states = iter([True, False, True, True, False])
    quits = []

    async def quit_once(_cfg):
        quits.append(1)

    monkeypatch.setattr(browser, "_quit_once", quit_once)
    monkeypatch.setattr(browser, "chrome_running", lambda _cfg: next(states, False))
    assert asyncio.run(browser.close_chrome(cfg, settle_s=1.2))
    assert len(quits) == 2


def test_fetches_running_ignores_dead_processes(tmp_path):
    import os

    from realread import browser
    from realread.config import Config

    cfg = Config(profile_dir=str(tmp_path / "p"), library_dir=str(tmp_path / "lib"), chrome_path="x")
    folder = browser._in_use_dir(cfg)
    folder.mkdir(parents=True)
    (folder / f"{os.getpid()}-a").touch()
    (folder / "99999999-b").touch()
    assert browser.fetches_running(cfg) == [os.getpid()]
    assert not (folder / "99999999-b").exists()


def test_job_tracks_several_publishers_waiting_for_the_user():
    from realread.jobs import Job
    from realread.library import Meta

    job = Job(id="j", ids=["10.1000/a"])
    job.on_event("blocked", kind="bot", host="a.example", url="https://a.example/x", mins=30)
    job.on_event("blocked", kind="login", host="b.example", url="https://idp.example/x", mins=30)
    text = job.summary()["action_required"]
    assert "a.example shows a bot check" in text and "b.example (now at idp.example) shows a sign-in page" in text
    job.on_event("unblocked", kind="bot", host="a.example")
    assert job.state == "waiting_for_user"
    job.on_event("result", meta=Meta(id="10.1000/b", status="needs_login", final_url="https://b.example/x"), cached=False)
    assert job.state == "running" and "action_required" not in job.summary()


def test_check_address_in_a_normal_page_is_not_a_bot_check():
    from realread import detect

    script = "<script src='https://x.example/a?refurl=https://validate.perfdrive.com/'></script>"
    article = "<html><body>" + script + "<p>" + "Sentence of the article. " * 400 + "</p></body></html>"
    assert not detect.is_bot_check("A paper", article)
    assert detect.is_bot_check("A paper", "<html><body>" + script + "<p>Please verify.</p></body></html>")
    assert detect.is_bot_check("Just a moment...", article)


def test_open_sources_and_openalex_only_take_the_published_version(monkeypatch):
    import json

    from realread import direct
    assert direct.is_open_source("https://arxiv.org/abs/2106.10053")
    assert direct.is_open_source("https://hal.science/x/file/paper.pdf")
    assert not direct.is_open_source("https://www.sciencedirect.com/science/article/pii/X.pdf")
    assert not direct.is_open_source("https://www.tandfonline.com/doi/full/10.1/x")

    def answer(locations):
        body = json.dumps({"best_oa_location": locations[0] if locations else None, "oa_locations": locations}).encode()
        monkeypatch.setattr(direct, "_get", lambda url: (200, body, url, 0.0))

    answer([{"pdf_url": "https://repo.example/a.pdf", "version": "acceptedVersion"}])
    assert direct.published_copy("10.1/x") is None
    answer([{"pdf_url": "https://repo.example/a.pdf", "version": "acceptedVersion"},
            {"pdf_url": "https://pub.example/a.pdf", "version": "publishedVersion"}])
    assert direct.published_copy("10.1/x") == "https://pub.example/a.pdf"
    answer([])
    assert direct.published_copy("10.1/x") is None


def test_university_session_date_comes_from_the_one_cookie(tmp_path):
    import sqlite3
    from datetime import UTC, datetime

    cfg = Config(profile_dir=str(tmp_path), library_dir=str(tmp_path / "l"))
    assert sso.university_session_until(cfg) is None
    (tmp_path / "Default").mkdir()
    con = sqlite3.connect(tmp_path / "Default" / "Cookies")
    con.execute("create table cookies (host_key text, name text, is_persistent int, expires_utc int)")
    micros = int((datetime(2027, 1, 29, tzinfo=UTC) - datetime(1601, 1, 1, tzinfo=UTC)).total_seconds() * 1e6)
    con.execute("insert into cookies values ('.login.microsoftonline.com', 'other', 1, ?)", (micros * 2,))
    con.commit()
    assert sso.university_session_until(cfg) is None
    con.execute("insert into cookies values ('.login.microsoftonline.com', 'ESTSAUTHPERSISTENT', 1, ?)", (micros,))
    con.commit()
    con.close()
    assert sso.university_session_until(cfg).date().isoformat() == "2027-01-29"


def test_nothing_about_an_institution_is_built_in():
    """A user's institution lives only in their own config file, written by `realread institution`."""
    from realread.config import Config
    cfg = Config(profile_dir="p", library_dir="l")
    assert cfg.institution == "" and cfg.idp_entity_id == "" and cfg.idp_federation == ""

