# Contributing

## Ground rules

RealRead reads papers the user is entitled to, one at a time, politely. Contributions that
weaken the policy in `src/realread/policy.py` (looser limits, parallel requests to one publisher,
CAPTCHA solving, fingerprint spoofing, link following, listing/issue crawling, HTTP server
transports) will not be accepted.

## Development

```bash
uv sync
uv run ruff check src tests
uv run pytest            # unit tests, no browser
uv run pytest -m browser # end-to-end flow on synthetic pages, needs a local Google Chrome
```

## Adding or fixing a publisher

1. Open a paper you can access in the RealRead Chrome (`realread open <doi>`).
2. Find the element that holds the article body (not the abstract or references only) and add
   its CSS selector to `BODY_SELECTORS` in `src/realread/sites.py`, keyed by the publisher host.
3. If full text is often only in the PDF, add a `PDF_RULES` entry (article path regex -> PDF path).
4. If the publisher words its sign-in or bot-check pages differently, extend the hint lists in
   `src/realread/detect.py` and add a case to `tests/test_units.py`.
5. Never commit real publisher pages or article text as fixtures; write synthetic HTML instead.

## Looking at a real page

To see what a real sign-in or publisher page offers while developing, `agent-browser` (optional, not a
dependency) can read the dedicated Chrome: `agent-browser --cdp 9333 snapshot -i`. RealRead itself stays on
Playwright: it needs in-process events, per-request test routes and response-stage PDF capture.

Do not write a real institution into source, README or tests. Use a made-up one, as the tests do.

