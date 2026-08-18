"""Cross-run dedupe state.

Plain JSON rather than SQLite: this file is committed back to the repo by the CI
workflow so state survives between GitHub Actions runs, and a binary SQLite file
conflicts badly in git while Actions' cache can be evicted without warning.

Size matters here. The workflow commits this file every five minutes, so storing a
full record for all ~21k tracked postings (6.6 MB) would balloon the repository.
Postings are therefore split in two: ``seen`` holds only ``key -> date`` for the vast
majority that never matched, and ``matches`` adds a score and notified flag for the
handful that cleared the bar. Keys are written sorted, one per line, so each commit
diffs as a few inserted lines rather than a rewritten blob.

Neither half stores anything identifying - no company, title, URL or location. Keys
are opaque hashes. The file is committed on every run, so readable detail here would
amount to publishing a log of the job search itself.

Bootstrap matters too. On a cold start every posting looks new, which would fire
several hundred alerts at once; ``mark_all_seen`` records the existing world silently.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Job

log = logging.getLogger(__name__)

VERSION = 2


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_seen(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class State:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.seen: dict[str, str] = {}
        self.matches: dict[str, dict[str, Any]] = {}
        self.etags: dict[str, str] = {}
        self.meta: dict[str, Any] = {}

    # ---------------------------------------------------------------- load/save

    def load(self) -> "State":
        if not self.path.exists():
            log.info("No state file at %s - treating this as a cold start", self.path)
            return self
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt state file must not wedge the poller; worst case we re-alert.
            log.warning("Could not read state (%s); starting empty", exc)
            return self

        self.etags = data.get("etags") or {}
        self.meta = data.get("meta") or {}
        self.seen = data.get("seen") or {}
        self.matches = data.get("matches") or {}

        # v1 kept every posting as a full record under "jobs".
        for key, rec in (data.get("jobs") or {}).items():
            if not isinstance(rec, dict):
                continue
            if rec.get("notified") or rec.get("score") is not None:
                self.matches.setdefault(key, rec)
            else:
                self.seen.setdefault(key, (rec.get("first_seen") or _today())[:10])
        return self

    def save(self) -> None:
        payload = {
            "version": VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "meta": self.meta,
            "etags": self.etags,
            "matches": self.matches,
            "seen": self.seen,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so an interrupted run can't leave truncated JSON behind.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=0, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------- access

    def is_new(self, job: Job) -> bool:
        return job.key not in self.seen and job.key not in self.matches

    def was_notified(self, job: Job) -> bool:
        return bool(self.matches.get(job.key, {}).get("notified"))

    def mark_seen(self, job: Job, score: float | None = None, notified: bool = False) -> None:
        """Record a posting. Scored or alerted ones also keep a score and notified flag."""
        if score is None and not notified:
            self.seen.setdefault(job.key, _today())
            return

        record = self.matches.get(job.key)
        if record is None:
            # Deliberately nothing identifying: no company, title, URL or location.
            # This file is committed to the repo on every run, so a readable log of
            # which roles were surfaced would be a running record of the job search.
            # The key is already an opaque hash, and Discord holds the readable copy.
            record = {"first_seen": datetime.now(timezone.utc).isoformat()}
            self.matches[job.key] = record
            self.seen.pop(job.key, None)
        if score is not None:
            record["score"] = score
        if notified:
            record["notified"] = True
            record["notified_at"] = datetime.now(timezone.utc).isoformat()

    def mark_all_seen(self, jobs: Iterable[Job]) -> int:
        count = 0
        for job in jobs:
            if self.is_new(job):
                self.mark_seen(job)
                count += 1
        return count

    def prune(self, days: int = 90) -> int:
        """Drop entries older than `days` so the committed file stays small."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        stale_seen = [k for k, v in self.seen.items()
                      if (dt := _parse_seen(v)) is not None and dt < cutoff]
        for key in stale_seen:
            del self.seen[key]

        stale_matches = [k for k, rec in self.matches.items()
                         if (dt := _parse_seen(rec.get("first_seen"))) is not None
                         and dt < cutoff]
        for key in stale_matches:
            del self.matches[key]

        return len(stale_seen) + len(stale_matches)

    def __len__(self) -> int:
        return len(self.seen) + len(self.matches)
