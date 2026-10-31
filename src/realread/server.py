"""MCP server (stdio only: RealRead is a personal, local tool, not a shared service)."""

from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from . import config, library
from .jobs import JobManager
from .policy import Guard, effective_limits

mcp = MCPServer(
    "realread",
    instructions=(
        "RealRead reads full texts of papers the user can already access: subscription papers through the "
        "user's own logged-in Chrome window, papers open to anyone by plain HTTP without the browser, all "
        "under built-in politeness limits (one request at a time per publisher, hourly and daily caps). A "
        "paper you can already read in full with another tool does not have to go through RealRead; use "
        "RealRead when you need the full text checked and stored, or for anything behind a subscription. "
        "Workflow: fetch_fulltext(ids) starts a background job; "
        "poll job_status until it is finished; then read_fulltext / search_fulltext. "
        "If a job reports state 'waiting_for_user' or 'action_required', RealRead has already notified "
        "the user with a desktop notification and the tab in front; the user completes the bot check or "
        "sign-in there, and other publishers go on meanwhile. Keep polling job_status with a long wait_s rather "
        "than stopping to ask; the job "
        "resumes by itself; if nobody responds within the wait limit, that publisher's papers are deferred and "
        "the job goes on with the others. "
        "Never try to bypass checks or limits, and do not split work to get around them. "
        "If the daily limit is reached, continue the review with what is already stored."
    ),
)
jobs = JobManager()


def _cfg() -> config.Config:
    return config.load()


@mcp.tool()
async def fetch_fulltext(
    ids: Annotated[list[str], Field(description="DOIs or article URLs.", min_length=1, max_length=300)],
    refresh: Annotated[bool, Field(description="Refetch items already stored.")] = False,
    wait_s: Annotated[int, Field(ge=0, le=600, description="Seconds to wait for progress before returning.")] = 60,
) -> dict:
    """Start a background job that fetches and stores full texts politely, then report its status.

    Already stored items return immediately. Fetching is rate-limited per publisher, so large
    batches take hours; poll job_status. Only one job runs at a time.
    """
    cfg = _cfg()
    try:
        job = jobs.start(cfg, ids, refresh)
    except RuntimeError as e:
        return {"error": str(e)}
    await jobs.wait(job, wait_s)
    return job.summary()


@mcp.tool()
async def job_status(
    job_id: Annotated[str | None, Field(description="Job id; default: the current/last job.")] = None,
    wait_s: Annotated[int, Field(ge=0, le=600, description="Wait up to this long for the job to progress.")] = 0,
    max_results: Annotated[int, Field(ge=1, le=300)] = 50,
) -> dict:
    """Progress of a fetch job, including whether it is waiting for the user."""
    job = jobs.get(job_id)
    if not job:
        return {"error": "no such job"}
    if wait_s:
        await jobs.wait(job, wait_s)
    return job.summary(max_results)


@mcp.tool()
def cancel_job(job_id: Annotated[str, Field(description="The id returned when you started the job.")]) -> dict:
    """Stop your own running fetch job after the current article. Other agents' jobs are not yours to cancel."""
    job = jobs.get(job_id)
    if not job:
        return {"error": "no such job"}
    job.control.cancelled = True
    return {"job_id": job.id, "state": "cancelling"}


@mcp.tool()
def read_fulltext(
    id: Annotated[str, Field(description="DOI or URL used when fetching.")],
    offset: Annotated[int, Field(ge=0)] = 0,
    max_chars: Annotated[int, Field(ge=1000, le=200_000)] = 40_000,
) -> dict:
    """Read a stored full text in chunks. `next_offset` is null at the end."""
    cfg = _cfg()
    item_id = library.normalize_id(id)
    text = library.load_text(cfg, item_id)
    meta = library.load_meta(cfg, item_id)
    if text is None:
        return {"id": item_id, "status": meta.status if meta else "not_fetched",
                "message": meta.message if meta else "call fetch_fulltext first"}
    end = offset + max_chars
    return {
        "id": item_id,
        "status": meta.status if meta else "",
        "message": meta.message if meta else "",
        "title": meta.title if meta else "",
        "total_chars": len(text),
        "text": text[offset:end],
        "next_offset": end if end < len(text) else None,
    }


@mcp.tool()
def search_fulltext(
    pattern: Annotated[str, Field(description="Case-insensitive regular expression.")],
    ids: Annotated[list[str] | None, Field(description="Limit to these DOIs/URLs.")] = None,
    context_chars: Annotated[int, Field(ge=50, le=2000)] = 300,
    max_hits: Annotated[int, Field(ge=1, le=200)] = 50,
) -> list[dict]:
    """Search all stored full texts and return matching snippets with their offsets."""
    return library.search(_cfg(), pattern, ids, context_chars, max_hits)


@mcp.tool()
def library_status() -> dict:
    """Stored items by status, problems and why, and today's remaining quota."""
    cfg = _cfg()
    metas = library.all_meta(cfg)
    counts: dict[str, int] = {}
    for m in metas:
        counts[m.status] = counts.get(m.status, 0) + 1
    return {
        "counts": counts,
        "ok": [{"id": m.id, "title": m.title, "chars": m.chars} for m in metas if m.status == "ok"],
        "problems": [{"id": m.id, "status": m.status, "recorded_at": m.fetched_at, "message": m.message}
                     for m in metas if m.status != "ok"],
        "problems_note": "Results of earlier fetches, not live state. job_status shows what is running now.",
        "quota": Guard(cfg, effective_limits(cfg)[0]).usage(),
    }
