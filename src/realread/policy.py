"""Built-in politeness policy: what keeps RealRead a reading tool and not a crawler.

The limits below are the loosest RealRead will ever run with. A config file can make
them stricter, never looser. All state is shared across processes (file lock) and
persisted, so restarting does not reset quotas or cool-downs.

Mechanisms:
  * one request at a time per publisher, >= MIN_INTERVAL_S apart (plus random jitter)
  * a longer pause after every BURST_EVERY articles from the same publisher
  * rolling hourly / daily caps per publisher (no built-in cap across all publishers; a config
    file may set one)
  * a daily cap per journal issue (no downloading whole issues)
  * a bot check the user answers: that publisher is slowed down. Checks recurring soon after
    being answered, or left unanswered, or HTTP 429, put it into a growing cool-down.
  * an append-only audit log of every article opened
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path

from filelock import FileLock

from .config import Config


@dataclass(frozen=True)
class Limits:
    min_interval_s: float = 20.0
    per_host_hour: int = 20
    per_host_day: int = 80
    total_day: int = 0  # 0: no cap across all publishers
    per_issue_day: int = 10


HARD_LIMITS = Limits()

JITTER = (1.0, 1.5)  # multiplier on the interval
BURST_EVERY = 8  # articles from one publisher before a longer pause
BURST_PAUSE_S = (120.0, 300.0)
SLOWDOWN_FACTOR = 2.0  # interval multiplier after a bot check ...
SLOWDOWN_WINDOW_S = 2 * 3600  # ... for this long
ANSWERED_PER_HOUR = 3  # checks a user answers within an hour before the site gets a pause
COOLDOWN_BASE_S = 1800.0
UNANSWERED_COOLDOWN_MAX_S = 4 * 3600.0  # a check is a question to one visitor; waiting a day adds nothing
COOLDOWN_MAX_S = 24 * 3600.0  # HTTP 429: the site said it is overloaded by us
# Re-signing in is the click the user would make: it is limited by spacing, not rationed. The daily
# ceiling only stops a sign-in that keeps failing from running all day.
AUTO_SIGN_IN_SPACING_S = 600.0
AUTO_SIGN_INS_PER_DAY = 48
RENEWAL_FRESH_S = 3600.0  # after a renewal the session is fresh: a closed article means no licence
HOUR, DAY = 3600.0, 86400.0

_CONFIG_FIELDS = {
    "min_interval_s": "min_interval_s",
    "per_host_hour": "max_per_host_hour",
    "per_host_day": "max_per_host_day",
    "total_day": "max_total_day",
    "per_issue_day": "max_per_issue_day",
}


def effective_limits(cfg: Config) -> tuple[Limits, list[str]]:
    """Combine the config with HARD_LIMITS, keeping whichever is stricter."""
    values = asdict(HARD_LIMITS)
    ignored: list[str] = []
    for f in fields(Limits):
        wanted = getattr(cfg, _CONFIG_FIELDS[f.name])
        if not wanted:
            continue
        hard = values[f.name]
        if f.name == "min_interval_s":
            stricter = wanted >= hard
        else:
            stricter = not hard or wanted <= hard  # 0 in HARD_LIMITS: no built-in cap
        if stricter:
            values[f.name] = wanted
        else:
            ignored.append(f"{_CONFIG_FIELDS[f.name]}={wanted} (built-in limit {hard})")
    return Limits(**values), ignored


# Redirect hosts that belong to one publisher's site, so its quota is not split in two.
_SAME_PUBLISHER = {"linkinghub.elsevier.com": "www.sciencedirect.com"}


def host_key(host: str) -> str:
    """Group a publisher's hosts (www./pdf. variants; EZproxy-rewritten hosts stay distinct)."""
    host = _SAME_PUBLISHER.get(host.lower(), host)
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2] in {"ac", "co", "com", "edu", "org", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class QuotaExhausted(Exception):
    def __init__(self, scope: str, resets_at: float):
        super().__init__(f"daily limit reached for {scope}")
        self.scope, self.resets_at = scope, resets_at


@dataclass
class Plan:
    ready_at: float
    reason: str  # interval | burst_pause | hourly_cap | cooldown | slowdown | ""


class Guard:
    def __init__(self, cfg: Config, limits: Limits | None = None, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.limits = limits or effective_limits(cfg)[0]
        self.clock = clock
        cfg.library.mkdir(parents=True, exist_ok=True)
        self._state_file = cfg.library / ".realread-state.json"
        self._lock = FileLock(str(cfg.library / ".realread-state.lock"))
        self._audit = cfg.library / "audit.jsonl"

    # -- state helpers -------------------------------------------------------------
    def _load(self) -> dict:
        if self._state_file.exists():
            return json.loads(self._state_file.read_text())
        return {"hosts": {}, "total": [], "issues": {}}

    def _save(self, state: dict) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self._state_file)

    def _host(self, state: dict, host: str) -> dict:
        h = state["hosts"].setdefault(host_key(host), {})
        for k, v in (("times", []), ("next_at", 0.0), ("streak", 0), ("cooldown_until", 0.0),
                     ("slow_until", 0.0), ("strikes", []), ("blocks", 0), ("autos", []), ("answered", []), ("limited", []),
                     ("renewed_at", 0.0)):
            h.setdefault(k, v)
        return h

    def _prune(self, state: dict, now: float) -> None:
        state["total"] = [t for t in state["total"] if now - t < DAY]
        for h in state["hosts"].values():
            h["times"] = [t for t in h.get("times", []) if now - t < DAY]
            h["strikes"] = [t for t in h.get("strikes", []) if now - t < DAY]
            h["autos"] = [t for t in h.get("autos", []) if now - t < DAY]
            h["answered"] = [t for t in h.get("answered", []) if now - t < DAY]
            h["limited"] = [t for t in h.get("limited", []) if now - t < DAY]
        today = date.fromtimestamp(now).isoformat()
        state["issues"] = {today: state.get("issues", {}).get(today, {})}

    # -- public API ----------------------------------------------------------------
    def plan(self, host: str) -> Plan:
        """When may the next article from `host` be opened? Raises QuotaExhausted."""
        with self._lock:
            state = self._load()
            now = self.clock()
            self._prune(state, now)
            h = self._host(state, host)
            lim = self.limits
            if lim.total_day and len(state["total"]) >= lim.total_day:
                raise QuotaExhausted("all publishers", min(state["total"]) + DAY)
            if len(h["times"]) >= lim.per_host_day:
                raise QuotaExhausted(host_key(host), min(h["times"]) + DAY)
            candidates = [(h["next_at"], h.get("next_reason", "interval")), (h["cooldown_until"], "cooldown")]
            last_hour = [t for t in h["times"] if now - t < HOUR]
            if len(last_hour) >= lim.per_host_hour:
                candidates.append((min(last_hour) + HOUR, "hourly_cap"))
            ready_at, reason = max(candidates)
            return Plan(ready_at, reason if ready_at > now else "")

    def record_request(self, host: str) -> None:
        with self._lock:
            state = self._load()
            now = self.clock()
            self._prune(state, now)
            h = self._host(state, host)
            h["times"].append(now)
            state["total"].append(now)
            interval = self.limits.min_interval_s * (SLOWDOWN_FACTOR if now < h["slow_until"] else 1.0)
            h["next_at"] = now + interval * random.uniform(*JITTER)
            h["next_reason"] = "slowdown" if now < h["slow_until"] else "interval"
            h["streak"] += 1
            if h["streak"] >= BURST_EVERY:
                h["streak"] = 0
                h["next_at"] += random.uniform(*BURST_PAUSE_S)
                h["next_reason"] = "burst_pause"
            self._save(state)

    def record_block(self, host: str, kind: str, retry_after_s: float = 0.0) -> float:
        """Register what a site asked for; returns the cool-down end (0 if none).

        'answered': the user answered a bot check. 'unanswered': nobody did. 'rate_limited': HTTP 429,
        whose Retry-After is honoured when it is longer than RealRead's own back-off.
        """
        with self._lock:
            state = self._load()
            now = self.clock()
            self._prune(state, now)
            h = self._host(state, host)
            h["slow_until"] = now + SLOWDOWN_WINDOW_S
            h["blocks"] += 1
            if kind == "answered":
                h["answered"].append(now)
                if len([t for t in h["answered"] if now - t < HOUR]) >= ANSWERED_PER_HOUR:
                    h["cooldown_until"] = max(h["cooldown_until"], now + COOLDOWN_BASE_S)
            elif kind == "unanswered":
                h["strikes"].append(now)
                if len(h["strikes"]) >= 2:  # the first one may just mean the user is away
                    wait = COOLDOWN_BASE_S * 2 ** (len(h["strikes"]) - 2)
                    h["cooldown_until"] = max(h["cooldown_until"], now + min(wait, UNANSWERED_COOLDOWN_MAX_S))
            else:
                h["limited"].append(now)
                wait = max(COOLDOWN_BASE_S * 2 ** (len(h["limited"]) - 1), retry_after_s)
                h["cooldown_until"] = max(h["cooldown_until"], now + min(wait, COOLDOWN_MAX_S))
            h["streak"] = 0
            self._save(state)
            return h["cooldown_until"] if h["cooldown_until"] > now else 0.0

    def auto_sign_in_allowed(self, host: str) -> bool:
        with self._lock:
            state = self._load()
            now = self.clock()
            self._prune(state, now)
            autos = self._host(state, host)["autos"]
            return len(autos) < AUTO_SIGN_INS_PER_DAY and (not autos or now - max(autos) >= AUTO_SIGN_IN_SPACING_S)

    def auto_sign_in_resets_at(self, host: str) -> float:
        """When the next automatic re-sign-in is possible."""
        with self._lock:
            state = self._load()
            now = self.clock()
            self._prune(state, now)
            autos = self._host(state, host)["autos"]
            if not autos:
                return now
            return min(autos) + DAY if len(autos) >= AUTO_SIGN_INS_PER_DAY else max(autos) + AUTO_SIGN_IN_SPACING_S

    def record_auto_sign_in(self, host: str) -> None:
        with self._lock:
            state = self._load()
            now = self.clock()
            self._prune(state, now)
            self._host(state, host)["autos"].append(now)
            self._save(state)

    def record_renewal(self, host: str) -> None:
        with self._lock:
            state = self._load()
            self._host(state, host)["renewed_at"] = self.clock()
            self._save(state)

    def renewed_recently(self, host: str) -> bool:
        """Spares the daily attempts: batches often follow each other, each with a closed article."""
        with self._lock:
            return self.clock() - self._host(self._load(), host)["renewed_at"] < RENEWAL_FRESH_S

    def issue_allowed(self, issue: str) -> bool:
        with self._lock:
            state = self._load()
            self._prune(state, self.clock())
            today = next(iter(state["issues"]))
            return state["issues"][today].get(issue, 0) < self.limits.per_issue_day

    def record_issue(self, issue: str) -> None:
        with self._lock:
            state = self._load()
            self._prune(state, self.clock())
            day = state["issues"][next(iter(state["issues"]))]
            day[issue] = day.get(issue, 0) + 1
            self._save(state)

    def usage(self) -> dict:
        with self._lock:
            state = self._load()
        now = self.clock()
        self._prune(state, now)
        lim = self.limits
        hosts = {}
        for key, h in state["hosts"].items():
            hosts[key] = {
                "last_hour": len([t for t in h.get("times", []) if now - t < HOUR]),
                "last_24h": len(h.get("times", [])),
                "cooldown_s": max(0, int(h.get("cooldown_until", 0) - now)),
                "slowed_s": max(0, int(h.get("slow_until", 0) - now)),
                "bot_checks_24h": len(h.get("strikes", [])) + len(h.get("answered", [])),
            }
        return {
            "limits": asdict(lim),
            "total_last_24h": len(state["total"]),
            "remaining_today": max(0, lim.total_day - len(state["total"])) if lim.total_day else None,
            "hosts": hosts,
        }

    def audit(self, **entry) -> None:
        entry = {"ts": round(self.clock(), 1), **entry}
        with self._lock, open(self._audit, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def audit_tail(self, n: int = 50) -> list[dict]:
        if not self._audit.exists():
            return []
        lines = self._audit.read_text().splitlines()[-n:]
        return [json.loads(x) for x in lines]


def state_path(cfg: Config) -> Path:
    return cfg.library / ".realread-state.json"
