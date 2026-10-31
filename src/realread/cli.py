"""Command line: realread init | login | institution | access | detect-proxy | sso | open | fetch | read | search |
status | log | doctor | serve."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import __version__, config, institutions, library, proxy, sso
from .browser import cdp_url, chrome_running, close_chrome, fetches_running, lazy_context, open_tab
from .fetcher import Control, resolve_url, run_batch
from .i18n import set_language, t
from .policy import Guard, effective_limits


def _ask(prompt: str, default: str) -> str:
    answer = input(f"{prompt}\n  [{default}]: ").strip()
    return answer or default


def _access_desc(cfg: config.Config) -> str:
    if cfg.proxy_template:
        desc = t("access.template", v=cfg.proxy_template)
    elif cfg.ezproxy_prefix:
        desc = t("access.prefix", v=cfg.ezproxy_prefix)
    elif cfg.ezproxy_suffix:
        desc = t("access.suffix", v=cfg.ezproxy_suffix)
    else:
        desc = t("access.direct")
    if net := cfg.browser_proxy_pac or cfg.browser_proxy:
        desc += t("access.browser_proxy", v=net)
    return desc


MAX_LISTED = 12


def _choose_institution(cfg: config.Config, query: str, pick: int | None = None, use: str | None = None,
                        interactive: bool = True) -> bool:
    """Look the institution up in public directories and let the user confirm how to use it."""
    print(t("inst.searching", q=query))
    try:
        found = institutions.search(query)
    except OSError as e:
        print(t("inst.offline", v=e))
        return False
    if not found:
        print(t("inst.none", q=query))
        return False
    listed = found[:MAX_LISTED]
    for n, inst in enumerate(listed, 1):
        where = f" ({inst.domain})" if inst.domain else (f" ({inst.country})" if inst.country else "")
        print(t("inst.row", n=n, name=inst.name, where=where))
        if inst.entity_id:
            print(t("inst.row.sso", v=inst.entity_id))
        if inst.proxy_template:
            print(t("inst.row.proxy", v=inst.proxy_template))
    if len(found) > MAX_LISTED:
        print(t("inst.more", n=len(found) - MAX_LISTED))
    if pick is None:
        answer = input(t("inst.pick") + ": ").strip() if interactive else ""
        if not answer.isdigit():
            return False
        pick = int(answer)
    if not 1 <= pick <= len(listed):
        raise SystemExit(f"no entry {pick} in the list")
    inst = listed[pick - 1]

    templates = {"direct": ""}
    print(t("inst.methods", name=inst.name))
    if inst.proxy_template:
        templates["proxy"] = inst.proxy_template
        print(t("inst.method.proxy", v=inst.proxy_template))
    if inst.domain and institutions.openathens_available(inst.domain):
        templates["openathens"] = proxy.openathens_template(inst.domain)
        print(t("inst.method.openathens", v=inst.domain))
    if use is None:
        use = "direct"
        if interactive and len(templates) > 1:
            numbers = {"1": "direct", "2": "proxy", "3": "openathens"}
            use = numbers.get(_ask(t("init.choose"), "1"), "direct")
    if use not in templates:
        raise SystemExit(f"'{use}' is not available for {inst.name} (available: {', '.join(templates)})")

    cfg.institution = inst.name
    cfg.proxy_template, cfg.ezproxy_prefix, cfg.ezproxy_suffix = templates[use], "", ""
    if inst.entity_id:
        cfg.idp_entity_id, cfg.idp_federation = inst.entity_id, inst.federation
    sso_note = t("inst.saved.sso", v=cfg.idp_entity_id) if cfg.idp_entity_id else ""
    print(t("inst.saved", name=inst.name, access=_access_desc(cfg), sso=sso_note))
    print(t("inst.attribution"))
    return True


def _load() -> config.Config:
    try:
        cfg = config.load()
    except config.NotInitialised as e:
        raise SystemExit(str(e)) from None
    set_language(cfg.language)
    _, ignored = effective_limits(cfg)
    if ignored:
        print(t("limits.ignored", v="; ".join(ignored)), file=sys.stderr)
    return cfg


def cmd_init(args: argparse.Namespace) -> None:
    if config.config_path().exists():
        cfg = config.load()
    elif config.legacy_config_path().exists() and (
        args.yes or input(t("init.legacy", path=config.legacy_config_path())).strip().lower() in ("", "y", "yes")
    ):
        cfg = config.read(config.legacy_config_path())
    else:
        cfg = config.defaults()
    for name in ("profile_dir", "library_dir", "chrome_path"):
        if getattr(args, name) is not None:
            setattr(cfg, name, getattr(args, name))
    proxy_link = args.proxy_link

    if not args.yes:
        print(t("init.intro"))
        cfg.profile_dir = _ask(t("init.profile"), cfg.profile_dir)
        cfg.library_dir = _ask(t("init.library"), cfg.library_dir)
        cfg.chrome_path = _ask(t("init.chrome"), cfg.chrome_path)
        query = _ask(t("inst.ask"), cfg.institution) if cfg.institution else input(t("inst.ask") + "\n  : ").strip()
        if query.lower() in ("none", "-") or not (query and _choose_institution(cfg, query)):
            print(t("init.access"))
            if _ask(t("init.choose"), "2" if proxy.proxied(cfg) else "1") == "2":
                proxy_link = input(t("init.proxy_link")).strip() or proxy_link
            else:
                proxy_link = "none"
    elif args.institution:
        _choose_institution(cfg, args.institution, pick=args.pick or 1, use=args.use, interactive=False)
    if proxy_link and proxy_link.lower() != "none":
        cfg.ezproxy_prefix, cfg.ezproxy_suffix = proxy.parse_link(proxy_link)
        cfg.proxy_template = ""
        if not (cfg.ezproxy_prefix or cfg.ezproxy_suffix):
            print(t("init.proxy_unknown"))
    elif proxy_link:
        cfg.ezproxy_prefix = cfg.ezproxy_suffix = cfg.proxy_template = ""

    lim, _ = effective_limits(cfg)
    print(t("init.summary"))
    print(t("init.row.config", v=config.config_path()))
    print(t("init.row.profile", v=cfg.profile))
    print(t("init.row.library", v=cfg.library))
    print(t("init.row.chrome", v=cfg.chrome_path))
    print(t("init.row.access", v=_access_desc(cfg)))
    print(t("init.row.limits", interval=lim.min_interval_s, hour=lim.per_host_hour, day=lim.per_host_day,
            total=t("init.total", n=lim.total_day) if lim.total_day else "", issue=lim.per_issue_day))
    if not args.yes and input(t("init.confirm")).strip().lower() != "y":
        print(t("init.cancelled"))
        return
    if not Path(cfg.chrome_path).exists():
        print(t("init.no_chrome", path=cfg.chrome_path), file=sys.stderr)
    cfg.library.mkdir(parents=True, exist_ok=True)
    cfg.profile.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.profile, 0o700)
    if not cfg.idp_entity_id:
        try:
            cfg.idp_entity_id, cfg.idp_federation = sso.learn_from_history(cfg)
        except Exception:
            pass
        if cfg.idp_entity_id:
            print(t("ev.idp_learned", idp=cfg.idp_entity_id))
    print(t("init.saved", path=config.save(cfg)))


def cmd_login(args: argparse.Namespace) -> None:
    cfg = _load()
    url = args.url
    if not url and cfg.ezproxy_prefix:
        p = urllib.parse.urlparse(cfg.ezproxy_prefix)
        url = f"{p.scheme}://{p.netloc}/login"
    url = url or "https://ieeexplore.ieee.org/"
    open_tab(cfg, proxy.rewrite(cfg, url) if cfg.proxy_template and not args.url else url)
    print(t("login.opened"))


def cmd_open(args: argparse.Namespace) -> None:
    """Open an article in the RealRead Chrome for the user to look at (one request, counted)."""
    cfg = _load()
    item_id = library.normalize_id(args.id)
    source = resolve_url(item_id)
    host = urllib.parse.urlparse(source).hostname or ""
    guard = Guard(cfg)
    plan = guard.plan(host)
    if (wait := plan.ready_at - time.time()) > 0:
        print(t("ev.wait", secs=int(wait), host=host, reason=t(f"reason.{plan.reason or 'interval'}")))
        time.sleep(wait)
    guard.record_request(host)
    guard.audit(id=item_id, host=host, status="opened_for_user")
    open_tab(cfg, proxy.rewrite(cfg, source))
    print(source)


def cmd_close(args: argparse.Namespace) -> None:
    cfg = _load()
    if not chrome_running(cfg):
        print(t("close.not_running"))
        return
    if (running := fetches_running(cfg)) and not args.force:
        raise SystemExit(t("close.busy", pids=", ".join(map(str, running))))
    print(t("close.done") if asyncio.run(close_chrome(cfg)) else t("close.failed"))


def _open_tab_urls(cfg: config.Config) -> list[str]:
    with urllib.request.urlopen(f"{cdp_url(cfg)}/json/list", timeout=5) as r:
        return [x.get("url", "") for x in json.load(r) if x.get("type") == "page"]


def cmd_detect_proxy(args: argparse.Namespace) -> None:
    cfg = _load()
    if not chrome_running(cfg):
        raise SystemExit(t("chrome.not_running"))
    _, suffix = proxy.detect_from_urls(_open_tab_urls(cfg))
    if not suffix:
        print(t("proxy.not_found"))
        return
    print(t("proxy.found", v=suffix))
    if args.yes or input(t("proxy.save")).strip().lower() == "y":
        cfg.proxy_template, cfg.ezproxy_prefix, cfg.ezproxy_suffix = "", "", suffix
        config.save(cfg)
        print(t("saved"))


def cmd_institution(args: argparse.Namespace) -> None:
    cfg = _load()
    if _choose_institution(cfg, " ".join(args.name), pick=args.pick, use=args.use, interactive=not args.yes):
        config.save(cfg)
        print(t("saved"))


def cmd_access(args: argparse.Namespace) -> None:
    cfg = _load()
    before = (cfg.browser_proxy, cfg.browser_proxy_pac)
    if args.direct:
        cfg.proxy_template = cfg.ezproxy_prefix = cfg.ezproxy_suffix = ""
    if args.template:
        if "$@" not in args.template and "$%" not in args.template:
            raise SystemExit("the template needs $@ (article URL) or $% (percent-encoded article URL)")
        cfg.proxy_template, cfg.ezproxy_prefix, cfg.ezproxy_suffix = args.template, "", ""
    if args.browser_proxy is not None:
        cfg.browser_proxy = args.browser_proxy
    if args.pac is not None:
        cfg.browser_proxy_pac = args.pac
    if args.direct or args.template or args.browser_proxy is not None or args.pac is not None:
        config.save(cfg)
        print(t("saved"))
    print(_access_desc(cfg))
    if (cfg.browser_proxy, cfg.browser_proxy_pac) != before and chrome_running(cfg):
        print(t("access.restart"))


def cmd_sso(args: argparse.Namespace) -> None:
    cfg = _load()
    if args.set:
        cfg.idp_entity_id, cfg.idp_federation = args.set, args.federation or ""
    elif args.learn:
        idp, fed = sso.learn_from_history(cfg)
        if idp:
            cfg.idp_entity_id, cfg.idp_federation = idp, fed
    if args.off or args.on:
        cfg.auto_sign_in = bool(args.on)
    if args.set or args.learn or args.off or args.on:
        config.save(cfg)
    if not cfg.idp_entity_id:
        print(t("sso.none"))
        return
    print(t("sso.show", idp=cfg.idp_entity_id, on="on" if cfg.auto_sign_in else "off"))


def _read_ids(args: argparse.Namespace) -> list[str]:
    ids = list(args.ids)
    if args.file:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        ids += [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return ids


def _printer(verbose: bool):
    def emit(event: str, **d) -> None:
        if event == "wait" and (verbose or d["secs"] >= 60):
            print(t("ev.wait", secs=d["secs"], host=d["host"], reason=t(f"reason.{d['reason'] or 'interval'}")),
                  flush=True)
        elif event == "blocked":
            print(t("ev.blocked.bot" if d["kind"] == "bot" else "ev.blocked.login", host=d["host"], mins=d["mins"]),
                  flush=True)
        elif event == "still_blocked":
            print(t("ev.still_blocked", host=d["host"], mins=d["waited_s"] // 60, title=d["title"][:70]), flush=True)
        elif event == "unblocked":
            print(t("ev.unblocked" if d["kind"] == "bot" else "ev.signed_in"), flush=True)
        elif event == "result":
            m: library.Meta = d["meta"]
            extra = f" {m.method} {m.chars} chars" if m.status in library.TEXT_STATUSES else f" {m.message}"
            print(f"[{m.status}]{' (stored)' if d['cached'] else ''} {m.id}{extra}", flush=True)
        elif event == "quota":
            when = datetime.fromtimestamp(d["resets_at"]).strftime("%Y-%m-%d %H:%M")
            print(t("ev.quota", scope=d["scope"], when=when), flush=True)
        elif event == "cooldown":
            print(t("ev.cooldown", host=d["host"], when=datetime.fromtimestamp(d["until"]).strftime("%H:%M")), flush=True)
        elif event == "auto_sign_in":
            print(t("ev.auto_sign_in", host=d["host"]), flush=True)
        elif event == "auto_sign_in_done":
            print(t("ev.auto_sign_in_ok" if d["ok"] else "ev.auto_sign_in_failed"), flush=True)
        elif event == "auto_sign_in_skipped":
            print(t("ev.auto_sign_in_skipped", host=d["host"],
                    when=datetime.fromtimestamp(d["resets_at"]).strftime("%H:%M")), flush=True)
        elif event == "auto_sign_in_again":
            print(t("ev.auto_sign_in_again", host=d["host"]), flush=True)
        elif event == "university_session_ended":
            print(t("ev.university_session_ended", when=datetime.fromtimestamp(d["until"]).strftime("%Y-%m-%d")), flush=True)
        elif event == "proxy_skipped":
            print(t("ev.proxy_skipped", host=d["host"]), flush=True)
        elif event == "idp_learned":
            print(t("ev.idp_learned", idp=d["idp"]), flush=True)
        elif event == "stopped":
            print(t("ev.stopped", why=d["why"]), flush=True)

    return emit


def cmd_fetch(args: argparse.Namespace) -> None:
    cfg = _load()
    ids = _read_ids(args)
    if not ids:
        raise SystemExit("no DOIs/URLs given")
    control = Control()

    async def run() -> list[library.Meta]:
        async with lazy_context(cfg) as ctx:
            return await run_batch(cfg, ctx, ids, refresh=args.refresh, emit=_printer(args.verbose), control=control)

    try:
        results = asyncio.run(run())
    except KeyboardInterrupt:
        control.cancelled = True
        print(t("ev.stopped", why="Ctrl+C"))
        return
    print(t("fetch.summary", v=dict(Counter(m.status for m in results))))


def cmd_read(args: argparse.Namespace) -> None:
    cfg = _load()
    item_id = library.normalize_id(args.id)
    text = library.load_text(cfg, item_id)
    if text is None:
        meta = library.load_meta(cfg, item_id)
        raise SystemExit(f"no full text for {item_id}" + (f" (status {meta.status}: {meta.message})" if meta else ""))
    print(text[args.offset : args.offset + args.max_chars])


def cmd_search(args: argparse.Namespace) -> None:
    cfg = _load()
    for h in library.search(cfg, args.pattern, max_hits=args.max_hits):
        print(f"== {h['id']} @{h['offset']}  {h['title']}\n{h['snippet']}\n")


def cmd_status(args: argparse.Namespace) -> None:
    cfg = _load()
    metas = library.all_meta(cfg)
    usage = Guard(cfg).usage()
    print(f"library: {cfg.library}")
    print(f"chrome running: {chrome_running(cfg)}")
    if until := sso.university_session_until(cfg):
        print(f"university sign-in valid at most until: {until.astimezone():%Y-%m-%d}")
    print("entries:", dict(Counter(m.status for m in metas)))
    for m in metas:
        if m.status != "ok":
            print(f"  [{m.status}] {m.id} {m.message}")
    print(f"limits: {usage['limits']}")
    remaining = usage["remaining_today"]
    print(f"last 24h: {usage['total_last_24h']}" + (f" (remaining {remaining})" if remaining is not None else ""))
    for host, h in usage["hosts"].items():
        print(f"  {host}: {h}")


def cmd_log(args: argparse.Namespace) -> None:
    cfg = _load()
    for e in Guard(cfg).audit_tail(args.n):
        ts = datetime.fromtimestamp(e.pop("ts")).strftime("%Y-%m-%d %H:%M:%S")
        print(ts, json.dumps(e, ensure_ascii=False))


def cmd_doctor(args: argparse.Namespace) -> None:
    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"[{'ok' if passed else '!!'}] {label}" + (f"  -> {hint}" if hint and not passed else ""))

    print(f"realread {__version__}, python {sys.version.split()[0]}, {sys.platform}")
    path = config.config_path()
    check(f"config {path}", path.exists(), "run: realread init")
    if not path.exists():
        return
    cfg = config.load()
    check(f"chrome {cfg.chrome_path}", Path(cfg.chrome_path).exists(), "set chrome_path in the config")
    check(f"profile {cfg.profile}", cfg.profile.exists(), "run: realread init")
    if cfg.profile.exists() and os.name == "posix":
        check("profile is private (700)", (cfg.profile.stat().st_mode & 0o077) == 0, f"chmod 700 '{cfg.profile}'")
    check(f"library {cfg.library}", cfg.library.exists() and os.access(cfg.library, os.W_OK), "check permissions")
    check(f"chrome debugging port {cfg.cdp_port}", chrome_running(cfg), "run: realread login (optional)")
    lim, ignored = effective_limits(cfg)
    check(f"limits {asdict(lim)}", True)
    if ignored:
        print("     ignored looser settings: " + "; ".join(ignored))
    print(f"access: {_access_desc(cfg)}")
    sys.exit(0 if ok else 1)


def cmd_serve(args: argparse.Namespace) -> None:
    from .server import mcp

    mcp.run()


def main() -> None:
    p = argparse.ArgumentParser(prog="realread", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("init", help="choose directories and write the config")
    s.add_argument("--profile-dir")
    s.add_argument("--library-dir")
    s.add_argument("--chrome-path")
    s.add_argument("--proxy-link", help="any paper link opened through your library proxy, or 'none'")
    s.add_argument("--institution", help="with --yes: look up this institution and use its entry")
    s.add_argument("--pick", type=int, help="with --institution: which entry of the list (default 1)")
    s.add_argument("--use", choices=("direct", "proxy", "openathens"), help="with --institution: access method")
    s.add_argument("--yes", action="store_true", help="accept the shown settings without prompting")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("institution", help="look up your institution's sign-in and library proxy (public directories)")
    s.add_argument("name", nargs="+")
    s.add_argument("--pick", type=int, help="entry number from the list")
    s.add_argument("--use", choices=("direct", "proxy", "openathens"), help="access method (default: direct)")
    s.add_argument("--yes", action="store_true", help="do not prompt (needs --pick)")
    s.set_defaults(func=cmd_institution)

    s = sub.add_parser("access", help="show or set how RealRead opens publisher pages")
    s.add_argument("--direct", action="store_true", help="open publishers directly (no library proxy)")
    s.add_argument("--template", help="library link pattern with $@ or $%%, e.g. https://proxy.uni.edu/login?url=$@")
    s.add_argument("--browser-proxy", metavar="URL", help="network proxy for the RealRead Chrome ('' to remove)")
    s.add_argument("--pac", metavar="URL", help="proxy auto-config (PAC) URL for the RealRead Chrome ('' to remove)")
    s.set_defaults(func=cmd_access)

    s = sub.add_parser("login", help="open the RealRead Chrome so you can sign in")
    s.add_argument("url", nargs="?")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("detect-proxy", help="learn the library proxy from tabs in the RealRead Chrome")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_detect_proxy)

    s = sub.add_parser("sso", help="automatic re-sign-in through your institution when a publisher session expires")
    s.add_argument("--learn", action="store_true", help="learn your institution from sign-ins in the RealRead Chrome")
    s.add_argument("--set", metavar="ENTITY_ID", help="set your institution's SAML entity ID")
    s.add_argument("--federation", help="federation ID (optional)")
    s.add_argument("--on", action="store_true")
    s.add_argument("--off", action="store_true")
    s.set_defaults(func=cmd_sso)

    s = sub.add_parser("open", help="open one article in the RealRead Chrome for you to look at")
    s.add_argument("id")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("close", help="quit the dedicated Chrome; your sign-ins stay in its profile")
    s.add_argument("--force", action="store_true", help="quit even while a fetch is running")
    s.set_defaults(func=cmd_close)

    s = sub.add_parser("fetch", help="fetch full texts for DOIs/URLs (politely; may take long)")
    s.add_argument("ids", nargs="*")
    s.add_argument("-f", "--file", help="file with one DOI/URL per line ('-' for stdin)")
    s.add_argument("--refresh", action="store_true", help="refetch even if already stored")
    s.add_argument("-v", "--verbose", action="store_true", help="show every wait")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("read", help="print stored full text")
    s.add_argument("id")
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--max-chars", type=int, default=1_000_000)
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("search", help="regex search across stored full texts")
    s.add_argument("pattern")
    s.add_argument("--max-hits", type=int, default=50)
    s.set_defaults(func=cmd_search)

    sub.add_parser("status", help="library, limits and per-publisher usage").set_defaults(func=cmd_status)
    s = sub.add_parser("log", help="audit log of articles opened")
    s.add_argument("-n", type=int, default=50)
    s.set_defaults(func=cmd_log)
    sub.add_parser("doctor", help="check the installation").set_defaults(func=cmd_doctor)
    sub.add_parser("serve", help="run the MCP server (stdio)").set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)
