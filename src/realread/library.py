"""On-disk library of extracted full texts: <library>/<key>/{fulltext.md, meta.json, source.pdf}."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config

_DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)

# Statuses with stored text.
TEXT_STATUSES = ("ok", "partial")


def normalize_id(raw: str) -> str:
    """Return a bare lowercase DOI when one is present, else the URL unchanged."""
    raw = raw.strip()
    low = raw.lower()
    if low.startswith("http") and "doi.org/" not in low:
        return raw
    m = _DOI_RE.search(raw)
    return m.group(0).rstrip(".,;").lower() if m else raw


def is_doi(item_id: str) -> bool:
    return bool(_DOI_RE.fullmatch(item_id))


def key_for(item_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", item_id)[:180]


@dataclass
class Meta:
    id: str
    # ok | partial | no_access | not_extracted | not_found | needs_user | needs_login | rate_limited | deferred
    # | refused | error
    status: str
    method: str = ""  # html | pdf
    source_url: str = ""
    final_url: str = ""
    title: str = ""
    chars: int = 0
    fetched_at: str = ""
    message: str = ""
    extra: dict = field(default_factory=dict)


def entry_dir(cfg: Config, item_id: str) -> Path:
    return cfg.library / key_for(item_id)


def _header(meta: Meta) -> str:
    lines = [f"title: {meta.title}", f"id: {meta.id}", f"source: {meta.final_url or meta.source_url}",
             f"method: {meta.method}", f"fetched_at: {meta.fetched_at}"]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def save(cfg: Config, meta: Meta, text: str | None = None, pdf: bytes | None = None) -> None:
    d = entry_dir(cfg, meta.id)
    d.mkdir(parents=True, exist_ok=True)
    if text is not None:
        stored = _header(meta) + text
        meta.chars = len(stored)  # same count as read_fulltext's total_chars, so offsets line up
        (d / "fulltext.md").write_text(stored)
    if pdf is not None:
        (d / "source.pdf").write_bytes(pdf)
    (d / "meta.json").write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=1))


def load_meta(cfg: Config, item_id: str) -> Meta | None:
    f = entry_dir(cfg, item_id) / "meta.json"
    return Meta(**json.loads(f.read_text())) if f.exists() else None


def load_text(cfg: Config, item_id: str) -> str | None:
    f = entry_dir(cfg, item_id) / "fulltext.md"
    return f.read_text() if f.exists() else None


def all_meta(cfg: Config) -> list[Meta]:
    if not cfg.library.exists():
        return []
    return [Meta(**json.loads(f.read_text())) for f in sorted(cfg.library.glob("*/meta.json"))]


def search(cfg: Config, pattern: str, ids: list[str] | None = None, context: int = 300, max_hits: int = 50) -> list[dict]:
    """Regex (case-insensitive) search across stored full texts, returning snippets."""
    rx = re.compile(pattern, re.I)
    wanted = {normalize_id(i) for i in ids} if ids else None
    hits: list[dict] = []
    for meta in all_meta(cfg):
        if meta.status not in TEXT_STATUSES or (wanted and meta.id not in wanted):
            continue
        text = load_text(cfg, meta.id) or ""
        # Skip the front matter (title, id, source): hits there are not in the paper's text.
        start = text.find("\n---\n", 4) + 5 if text.startswith("---\n") else 0
        for m in rx.finditer(text, start):
            a, b = max(0, m.start() - context), min(len(text), m.end() + context)
            hits.append({"id": meta.id, "title": meta.title, "offset": m.start(), "snippet": text[a:b]})
            if len(hits) >= max_hits:
                return hits
    return hits
