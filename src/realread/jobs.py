"""Background fetch jobs for the MCP server, so agents can queue hundreds of papers
without holding a tool call open, and see when the batch is waiting for the user."""

from __future__ import annotations

import asyncio
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .browser import lazy_context
from .config import Config
from .fetcher import Control, run_batch
from .library import Meta, normalize_id
from .policy import host_key


@dataclass
class Job:
    id: str
    ids: list[str]
    refresh: bool = False
    # running | waiting_for_user | finished | stopped | cancelled | error
    state: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    results: list[Meta] = field(default_factory=list)
    waiting: dict | None = None
    blocked: dict[str, dict] = field(default_factory=dict)  # by host: what waits for the user
    notes: list[str] = field(default_factory=list)
    control: Control = field(default_factory=Control)
    task: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def on_event(self, event: str, **data) -> None:
        if event == "wait":
            self.waiting = {"host": data["host"], "reason": data["reason"], "until": time.time() + data["secs"]}
        elif event == "blocked":
            self.state = "waiting_for_user"
            self.blocked[data["host"]] = {"kind": data["kind"], "host": data["host"], "url": data["url"],
                                          "since": time.time()}
        elif event == "still_blocked":
            if b := self.blocked.get(data["host"]):
                b.update(url=data["url"], title=data["title"])
        elif event == "unblocked":
            self.blocked.pop(data["host"], None)
            if not self.blocked:
                self.state = "running"
        elif event == "result":
            self.waiting = None
            self.results.append(data["meta"])
            # A fetch that stopped waiting without the user (timed out) sends no "unblocked".
            if data["meta"].status in ("needs_user", "needs_login"):
                self.blocked = {h: b for h, b in self.blocked.items() if not _same_site(h, data["meta"])}
                if not self.blocked and self.state == "waiting_for_user":
                    self.state = "running"
        elif event == "quota":
            self.notes.append(f"daily limit reached for {data['scope']}")
        elif event == "cooldown":
            until = datetime.fromtimestamp(data["until"]).strftime("%Y-%m-%d %H:%M")
            self.notes.append(f"{data['host']} is cooling down until {until}; its items were deferred")
        elif event == "auto_sign_in_skipped":
            when = datetime.fromtimestamp(data["resets_at"]).strftime("%H:%M")
            self.notes.append(f"{data['host']}: an automatic re-sign-in was tried too recently (next one after {when}); "
                              "the user is asked if the page requires sign-in, otherwise the article is deferred")
        elif event == "auto_sign_in_done":
            self.notes.append(f"{data['host']}: automatic re-sign-in {'succeeded' if data['ok'] else 'needs the user'}")
        elif event == "proxy_skipped":
            self.notes.append(f"{data['host']}: the library proxy is not set up for it; opened directly")
        elif event == "stopped":
            self.notes.append(f"stopped: {data['why']}")
        self.changed.set()

    def summary(self, max_results: int = 50) -> dict:
        counts: dict[str, int] = {}
        for m in self.results:
            counts[m.status] = counts.get(m.status, 0) + 1
        out = {
            "job_id": self.id,
            "state": self.state,
            "progress": f"{len(self.results)}/{len(self.ids)}",
            "counts": counts,
            "notes": self.notes[-5:],
            "results": [{"id": m.id, "status": m.status, "title": m.title, "chars": m.chars, "message": m.message}
                        for m in self.results[-max_results:]],
        }
        done = {m.id for m in self.results}
        if queued := [i for i in dict.fromkeys(normalize_id(x) for x in self.ids if x.strip()) if i not in done]:
            out["queued"] = {"count": len(queued), "next": queued[:20]}
        if self.waiting and self.state == "running":
            out["next_request_in_s"] = max(0, int(self.waiting["until"] - time.time()))
            out["waiting_reason"] = self.waiting["reason"]
        if self.blocked:
            shown = []
            for b in self.blocked.values():
                what = "a bot check (not a sign-in)" if b["kind"] == "bot" else "a sign-in page"
                at = urllib.parse.urlparse(b["url"]).hostname or b["host"]
                state = f"{int(time.time() - b.get('since', time.time()))} s ago"
                if b.get("title"):
                    state += f", tab now titled {b['title'][:80]!r}"
                shown.append(f"{b['host'] if at == b['host'] else b['host'] + ' (now at ' + at + ')'} "
                             f"shows {what} (since {state})")
            out["action_required"] = (
                "; ".join(shown) + ". RealRead has already notified the user and brought the tab to the front; the "
                "user completes it there and RealRead reminds them every few minutes. Other publishers go on "
                "meanwhile. Keep polling job_status (it resumes on its own) and tell the user plainly what is "
                "waiting for them. Never try to bypass it."
            )
        return out


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.active: Job | None = None

    def start(self, cfg: Config, ids: list[str], refresh: bool = False) -> Job:
        if self.active and self.active.state in ("running", "waiting_for_user"):
            raise RuntimeError(f"job {self.active.id} is still running and may belong to another agent; submit again "
                               "when it has ended (check job_status). Only one job runs at a time.")
        job = Job(id=uuid.uuid4().hex[:8], ids=ids, refresh=refresh)
        self.jobs[job.id] = job
        self.active = job
        job.task = asyncio.create_task(self._run(cfg, job))
        return job

    async def _run(self, cfg: Config, job: Job) -> None:
        try:
            async with lazy_context(cfg) as ctx:
                await run_batch(cfg, ctx, job.ids, refresh=job.refresh, emit=job.on_event, control=job.control)
            if job.control.cancelled:
                job.state = "cancelled"
            elif any("stopped" in n for n in job.notes):
                job.state = "stopped"
            else:
                job.state = "finished"
        except Exception as e:
            job.state = "error"
            job.notes.append(f"{type(e).__name__}: {e}")
        finally:
            job.finished_at = time.time()
            job.changed.set()

    def get(self, job_id: str | None) -> Job | None:
        return self.jobs.get(job_id) if job_id else self.active

    async def wait(self, job: Job, seconds: float) -> None:
        """Return when the job ends, starts or stops waiting for the user, or `seconds` pass."""
        end = time.monotonic() + seconds
        initial = job.state
        while job.state == initial and initial in ("running", "waiting_for_user") and (left := end - time.monotonic()) > 0:
            job.changed.clear()
            try:
                await asyncio.wait_for(job.changed.wait(), timeout=left)
            except TimeoutError:
                break


def _same_site(host: str, meta: Meta) -> bool:
    return host_key(host) == host_key(urllib.parse.urlparse(meta.final_url or meta.source_url).hostname or "")


def job_dict(job: Job) -> dict:
    d = asdict(job)
    for k in ("control", "task", "changed", "results"):
        d.pop(k, None)
    return d
