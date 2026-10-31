# RealRead

**Let AI agents read the papers you already have access to — through your own logged-in browser, politely.**

[中文说明](README.zh-CN.md)

Agents can search and read open-access papers, but most of the literature sits behind
subscriptions that you (through your university or library) already pay for. RealRead lets an
agent read those full texts **with your existing access** for literature reviews: it opens papers
in a dedicated Chrome window where you have signed in once, extracts the article body (HTML or
PDF), and keeps it in a local full-text library that the agent can read and search via MCP.

**The red line:** RealRead only reads what your own account can already open. It never solves
CAPTCHAs, never works around paywalls or access controls, and is built not to crawl.

## How it works

1. A DOI is resolved to the publisher page through doi.org (no publisher request). A paper anyone
   can open (arXiv, CVF, a direct PDF, or a publisher's own free copy listed by OpenAlex; only the DOI
   is sent there) is read by plain HTTP without the browser. It must still prove to be the full text,
   the limits below still apply, and anything that fails goes on to step 2.
2. The page opens in a new tab of the **dedicated Chrome profile**, so your publisher /
   institutional sign-in (SSO, campus IP, VPN or library proxy) applies. The browser keeps and
   renews its own cookies; RealRead never reads or exports their contents. It only looks up the
   end date of the university sign-in (see *Automatic re-sign-in*).
3. RealRead stays on the page for a few seconds and scrolls, like a reader (this also loads lazy
   full text), then extracts the article body. If the HTML lacks it, the PDF is fetched in the same
   session and converted to text.
4. Results are stored as Markdown with a status per paper:

   | status | meaning |
   |---|---|
   | `ok` | full text stored |
   | `partial` | possibly not the full text: short text and no PDF to compare (a short article, or only the abstract) |
   | `no_access` | your account cannot open this paper |
   | `not_extracted` | no article text found, and the page does not ask you to sign in; try another channel |
   | `not_found` | doi.org does not know this DOI, or the id is neither a DOI nor a URL; no page was opened |
   | `needs_login` / `needs_user` | the batch waited for you to sign in / clear a bot check, and you did not in time |
   | `deferred` | left for later (daily limit, cool-down, its publisher waiting for you, re-sign-ins used up, page failed to load, batch cancelled) |
   | `refused` | not a single-article address |

Tested publishers include IEEE Xplore, ScienceDirect, Springer, Nature, Wiley, ACS, ACM, Taylor &
Francis and Science; other sites fall back to a generic extractor.

## Built-in politeness: reading, not crawling

These limits are compiled in. The config file can make them **stricter, never looser**.

| mechanism | built-in value |
|---|---|
| one request at a time per publisher, spaced by | ≥ 20 s (× 1–1.5 random jitter) |
| longer break after every 8 papers from one publisher | 2–5 min |
| per publisher | ≤ 20 papers / hour, ≤ 80 / 24 h |
| all publishers together | no built-in cap (the config file may set one: `max_total_day`) |
| one journal issue | ≤ 10 papers / day (no whole-issue downloads) |
| a bot check that clears by itself | nothing; it is only logged |
| a bot check you answer | that publisher runs at half speed for 2 h; 3 within an hour: 30 min pause |
| a bot check nobody answers | half speed; from the second in 24 h: cool-down of 30 min, doubling, up to 4 h |
| HTTP 429 | cool-down of 30 min, doubling, up to 24 h, or the site's `Retry-After` if longer |
| listings, search and table-of-contents pages | refused; links are never followed |
| audit log | every opened paper is logged (`realread log`) |

Publishers are interleaved, so a batch of several hundred papers runs for hours to a day or two
without any single site seeing a burst. Quotas and cool-downs are stored on disk and survive
restarts.

**When a bot check or a sign-in page appears, that publisher pauses**: you get a desktop
notification, the tab comes to the front, and RealRead waits (30 min by default) until you have
dealt with it in the browser. Other publishers go on meanwhile in background tabs, under their own
limits. If you end up somewhere else, such as the publisher's home page, RealRead opens the paper
again. It then continues, more slowly for that publisher after a bot check. If nobody responds,
that publisher's papers are marked `deferred`.

These mechanisms protect you, your institution's licence and the publishers from accidental
over-use. As with any open-source tool they can be changed in the code — doing that to bulk
download content is exactly what this project is not for, and is likely to break your
institution's licence terms.

## Install

Requires Python ≥ 3.11, [uv](https://docs.astral.sh/uv/) and Google Chrome.

```bash
git clone https://github.com/Ares960826/realread && cd realread && uv sync
uv run realread init     # shows every directory it will use; nothing is written until you confirm
uv run realread login    # opens the dedicated Chrome: sign in as you normally do, keep the window open
uv run realread doctor   # checks the setup
```

`init` asks for:

1. the dedicated Chrome profile directory (created with mode 700);
2. the library directory (full texts, PDFs, quota state, audit log);
3. the Chrome executable;
4. your institution's name (optional). RealRead looks it up in public directories and shows
   what it found — your institution's sign-in (for automatic re-sign-in) and, if listed, its
   library proxy — and lets you pick how papers are opened (see *Off-campus access* below);
5. if you skipped 4: how you usually reach paywalled papers. Most people choose 1: "Access
   through your institution" on the publisher site, or campus network / VPN — nothing to fill
   in. If you use your library's database links (addresses containing `ezproxy`), choose 2 and
   paste one such link, or run `realread detect-proxy` later to learn the proxy from your open tabs.

The config lives at the platform config directory (e.g. `~/Library/Application Support/realread/config.toml`);
set `REALREAD_CONFIG` to use another file.

Sign in once per publisher: open a paper there, click "Access through your institution", choose
your university. The session stays in the dedicated profile, also across Chrome restarts (RealRead
starts Chrome with `--restore-last-session`, which keeps session-only sign-in cookies). Publishers
still end sessions on their side after a while (IEEE after a few hours, for example); RealRead then
pauses and asks you to sign in again — unless it can do that itself (below).

**Automatic re-sign-in.** Universities usually keep their own single-sign-on session much longer
than publishers do (for example Microsoft's "Stay signed in", ~90 days). RealRead remembers which
institution you signed in with (its SAML entity ID, learned from your own sign-ins; see
`realread sso`), and when a publisher session expires it opens that publisher's direct
institutional login link. If your university's session is still alive you are signed back in
without any interaction; if the university asks for a password or a confirmation, RealRead
pauses and asks you. RealRead never handles passwords or tokens. Attempts are at least 10 minutes
apart per publisher; a ceiling of 48 a day only stops a sign-in that keeps failing. Where there is no
direct link, or the link stops on a page that wants a click, RealRead clicks through as you would:
"Access through your institution", your institution in the list, your only account, "Stay signed in".
It never types a password or code, never touches a bot check and never accepts a consent page.
With Microsoft sign-in, `realread status` shows the date that session ends at most; after it, you sign
in once yourself. Supported: IEEE, ScienceDirect, Springer, Nature, IOP, SPIE, and Atypon
sites (Taylor & Francis, Science, Wiley, ACS, ACM, RSNA). After a successful re-sign-in, a paper that
stays closed within the next hour counts as not licensed, without another attempt.

**PDFs.** RealRead's job is to reach the full text with your access. When the body is only in the
PDF, it stores the original as `source.pdf` in the paper's library folder next to its own plain
text extraction; use your own PDF tools on that file when you need a closer reading.

## Off-campus access, for any institution

RealRead has no built-in list of universities. It looks your institution up by name in public
directories and offers the access methods it finds; you confirm before anything is saved.

```bash
uv run realread institution "University of Exampleton"   # list matches, pick yours, choose a method
uv run realread access                                    # show the current method
```

| method | when | how it is found |
|---|---|---|
| direct (default) | campus network, VPN, or "Access through your institution" on publisher sites | — |
| library proxy | your library gives links like `…/login?url=…` (EZproxy and similar) | [libproxy-db](https://github.com/tom5760/ezproxy-db) (~1500 institutions), a pasted link, or `realread detect-proxy` |
| OpenAthens redirector | your institution uses OpenAthens | checked live at `go.openathens.net/redirector/<domain>` |
| custom link pattern | any other proxy or redirector | `realread access --template 'https://proxy.example.edu/login?url=$@'` (`$@` = article URL, `$%` = percent-encoded) |
| network proxy for the RealRead Chrome | your institution publishes a proxy server or PAC file | `realread access --browser-proxy http://proxy.example.edu:8080` or `--pac https://example.edu/proxy.pac` |

Your institution's single-sign-on identity (SAML entity ID) comes from the
[SeamlessAccess](https://seamlessaccess.org) discovery service, which lists the identity providers
of eduGAIN federations and OpenAthens. It is used for automatic re-sign-in (below).

A library proxy only covers publishers the library has set up. When the proxy answers "this host is
not configured", RealRead opens that publisher directly for the rest of the batch. The network
proxy applies only to the dedicated Chrome, never to your normal browser; quit the RealRead Chrome
after changing it.

libproxy-db is CC BY-SA 4.0; RealRead downloads it at run time, caches it for a week and does not
redistribute it.

## Use

Command line:

```bash
uv run realread fetch -f dois.txt          # one DOI or article URL per line; long batches: run in background
uv run realread fetch 10.1109/CVPR.2016.90
uv run realread search "contrastive loss"  # regex over stored full texts
uv run realread read 10.1109/CVPR.2016.90
uv run realread open 10.1109/CVPR.2016.90  # show the paper to yourself in the RealRead Chrome
uv run realread status                     # library, limits, per-publisher usage and cool-downs
uv run realread log                        # audit log
uv run realread close                      # quit the RealRead Chrome; it starts again when a fetch needs it
uv run realread sso --learn                # remember your institution for automatic re-sign-in
uv run realread institution "<name>"       # look up your institution's sign-in and library proxy
```

MCP (Claude Code shown; any MCP client works):

```bash
claude mcp add --scope user realread -- uv run --directory /path/to/realread realread serve
```

| tool | purpose |
|---|---|
| `fetch_fulltext(ids)` | start a background job (up to 300 ids); returns progress after `wait_s` |
| `job_status(job_id)` | progress; `state: waiting_for_user` + `action_required` when you are needed |
| `cancel_job(job_id)` | stop your own job after the current paper |
| `read_fulltext(id, offset)` | read a stored full text in chunks |
| `search_fulltext(pattern)` | regex search across the library, with snippets |
| `library_status()` | stored items, problems and remaining quota |

The server speaks stdio only: RealRead is a personal, local tool, not a shared service. After
updating RealRead, restart your MCP client so it loads the new code.

## Security notes

- The dedicated Chrome listens on a debugging port (default 9333) bound to 127.0.0.1. While it
  runs, local processes can control that profile, so use it only for literature accounts.
- The profile directory holds your session cookies; it is created private (700). RealRead never
  copies cookies out of the browser.
- Full texts stay on your machine. Respect your licence when sharing them.

## Limitations

- Publisher layouts change; selectors live in `src/realread/sites.py` (see CONTRIBUTING.md).
- Chinese WebVPN systems (encrypted host rewriting) are not supported; use a VPN client or
  publisher-side institutional sign-in.
- PDF text is plain text (no layout reconstruction).

## License

MIT
