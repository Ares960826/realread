# Changelog

## 0.1.0 (unreleased)

- Windows: checking whether a fetch is running no longer sends Ctrl+C to that process.
- Keeping the sign-in is RealRead's job again. The limit of 3 automatic re-sign-ins a day is replaced by
  10 minutes between attempts (a ceiling of 48 a day stops failing loops). Without a direct link, or when a
  link stops on a page that wants a click, RealRead clicks through as a person would, but never types
  passwords or codes, touches bot checks or accepts consent pages. `realread status` shows when the
  Microsoft sign-in ends at most.
- Papers anyone can open are read by plain HTTP, without Chrome, under the same limits, and must prove to
  be the full text. Chrome starts only when a paper needs it.
- Automatic re-sign-in also covers IOP and SPIE (OpenAthens Connect links) and RSNA. A publisher whose
  sign-in page says it does not know the institution is `no_access` at once, with that reason.
- "Unknown" is no longer reported as "no access". Used-up re-sign-ins (with no sign-in prompt on the page)
  and pages that fail to load are `deferred`. An address that starts a download is read as a PDF.
- A successful re-sign-in is remembered for an hour across batches, so closed articles do not use up the
  day's attempts. "Full access" counts as granted only as a badge on its own line. Reading is repeated
  when a page redirects itself meanwhile.
- Bot-check cool-downs now follow what happened instead of counting every sighting. A check that clears by
  itself costs nothing, an answered one slows the publisher down, and only recurring or unanswered ones
  pause it (at most 4 h). One article counts once. HTTP 429 honours `Retry-After`. Visits passing through a
  check service are logged.
- A normal page that merely mentions a check service (IOP's script addresses, forms with reCAPTCHA) is no
  longer taken for a bot check, and a wait ends as soon as the article text is readable. A waiting tab
  reminds the user every 5 minutes and reports what it shows.
- `realread close` quits the dedicated Chrome, also when Chrome restarts itself to apply an update,
  and refuses while a fetch is using it unless `--force` is given.
- A DOI doi.org does not know, or an id that is neither a DOI nor a URL, is
  `not_found` without opening a page or counting a bot check. Quotas, audit log and prompts name the
  site actually reached instead of `doi.org`, and `linkinghub.elsevier.com` shares ScienceDirect's quota.
- After a bot check or sign-in that ends on another page, the paper is opened again. A page that
  looks clear for a moment while navigating no longer ends the wait.
- An address that is itself a PDF is read, and on sites without a publisher rule the paper's own PDF
  link is used. Open access pages no longer ask for a sign-in; a page with no text and no sign-in
  prompt is `not_extracted` instead of `no_access`.
- A bot check or sign-in pauses only that publisher: others go on in background tabs, so the user's tab
  keeps the focus. If nobody completes it, only that publisher's papers are deferred.
- `job_status` lists queued papers, and notes give cool-down ends and used-up re-sign-in attempts.
  `library_status` dates its problems, and their messages are in the past tense. `cancel_job` needs a
  job id. `fetched_at` is the end of the fetch, and page titles lose the site name.
- No built-in daily cap across all publishers (was 200 / 24 h); the per-publisher caps
  bound the load each site sees. A config file may still set one (`max_total_day`).

First public version.

- Dedicated Chrome profile over CDP: the user's own institutional / publisher sign-in, campus IP,
  VPN or library proxy (prefix or hostname form, auto-detected) is used as is.
- Full text from publisher HTML, with PDF fallback; local Markdown library with search.
- Built-in politeness policy that config can only tighten: per-publisher spacing with jitter,
  burst pauses, hourly / daily / per-issue caps, slow-down after bot checks, exponential
  cool-down on repeated checks or HTTP 429, listing pages refused, audit log.
- Bot checks and sign-in pages pause the batch, notify the user and wait for them. This includes
  a bot check in front of the PDF only (e.g. Wiley PDF-only articles behind Cloudflare): the PDF
  is opened in the tab for the browser or the user to clear, and taken from that navigation
  (CDP Fetch, since Chrome's PDF viewer truncates the logged body) instead of being reported as
  `no_access`. A PDF address that redirects to the abstract means no access in the current
  session and leads to the sign-in check below.
- An ended publisher session is no longer mistaken for missing access. Many publishers say
  nothing when their session has ended (Wiley shows only a personal "Login / Register"), so
  RealRead does not rely on sign-in wording: when a supported publisher shows no full text, it
  renews the session through the user's institution once and reads the article again. An
  article that stays closed after a renewal in the same batch is `no_access` (not licensed),
  without holding the batch or renewing again. Where no renewal is possible, `no_access` says
  so and asks the user to sign in.
- A short body (abstract + long reference list) is no longer stored as `partial` full text
  when the page offers access to the full version or, on Wiley, its data layer says the
  account has no access.
- The article is recognised across publisher redirects (linkinghub -> ScienceDirect), and when
  an institutional sign-in ends on the publisher's home page (ACS) RealRead opens the article again.
- A maintenance / outage page (or HTTP 5xx) defers that publisher's papers ("retry later")
  instead of reporting `no_access`.
- MCP instructions: while a job waits for the user, agents keep polling instead of stopping to ask;
  RealRead has already notified the user.
- Attaching works when the dedicated Chrome is running without a window (macOS).
- Search skips the stored front matter; `chars` counts the stored text, as `read_fulltext` does.
- MCP server (stdio) with background jobs; CLI with init / login / detect-proxy / open / fetch /
  read / search / status / log / doctor.
- Automatic re-sign-in through the user's institution (SAML WAYFless links for IEEE,
  ScienceDirect, Springer, Nature and Atypon sites) when a publisher session expires; falls back to the user
  when the institution asks for a password. The institution is learned from the user's own
  sign-ins (`realread sso`).
- Session-only sign-in cookies survive Chrome restarts (`--restore-last-session`).
- Off-campus access for any institution, without a built-in list: `realread institution <name>`
  (also asked in `init`) looks the institution up in public directories — SeamlessAccess for the
  SAML identity provider, libproxy-db for the library proxy pattern, the OpenAthens redirector
  checked live — and the user picks the method. Generic link templates (`$@` / `$%`), an
  optional network proxy or PAC file for the dedicated Chrome only (`realread access`), and a
  direct-access fallback when the library proxy is not set up for a publisher.
- English and Chinese CLI messages, chosen from the system language.
