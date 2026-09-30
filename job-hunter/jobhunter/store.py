"""SQLite persistence so we never apply to the same job twice."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jobhunter.models import Job, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    apply_url    TEXT,
    location     TEXT,
    remote       INTEGER,
    description  TEXT,
    salary_min   INTEGER,
    salary_max   INTEGER,
    posted_at    TEXT,
    ats          TEXT,
    score        REAL DEFAULT 0,
    reasons      TEXT DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'new',
    note         TEXT DEFAULT '',
    first_seen   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""

# Statuses that mean "we already acted on this, leave it alone on re-fetch".
FINAL = {Status.APPLIED, Status.SKIPPED}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if "emailed_at" not in cols:                     # added after the first release
            self.db.execute("ALTER TABLE jobs ADD COLUMN emailed_at TEXT")
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    def upsert(self, job: Job) -> bool:
        """Insert a new job. Returns True if it was new. Existing jobs keep their status."""
        cur = self.db.execute("SELECT 1 FROM jobs WHERE key = ?", (job.key,))
        if cur.fetchone():
            return False
        now = _now()
        self.db.execute(
            """INSERT INTO jobs (key, source, external_id, company, title, url, apply_url,
                   location, remote, description, salary_min, salary_max, posted_at, ats,
                   score, reasons, status, first_seen, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job.key, job.source, job.external_id, job.company, job.title, job.url,
                job.apply_url, job.location, int(job.remote), job.description,
                job.salary_min, job.salary_max, job.posted_at, job.ats, job.score,
                json.dumps(job.reasons), job.status.value, now, now,
            ),
        )
        self.db.commit()
        return True

    def set_score(self, key: str, score: float, reasons: list[str], status: Status) -> None:
        self.db.execute(
            "UPDATE jobs SET score=?, reasons=?, status=?, updated_at=? WHERE key=?",
            (score, json.dumps(reasons), status.value, _now(), key),
        )
        self.db.commit()

    def set_status(self, key: str, status: Status, note: str = "") -> None:
        self.db.execute(
            "UPDATE jobs SET status=?, note=?, updated_at=? WHERE key=?",
            (status.value, note, _now(), key),
        )
        self.db.commit()

    def get(self, key: str) -> Job | None:
        row = self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        return self._row_to_job(row) if row else None

    def by_status(self, *statuses: Status, limit: int | None = None) -> list[Job]:
        marks = ",".join("?" for _ in statuses)
        sql = f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY score DESC, first_seen DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.db.execute(sql, [s.value for s in statuses]).fetchall()
        return [self._row_to_job(r) for r in rows]

    def note(self, key: str) -> str:
        row = self.db.execute("SELECT note FROM jobs WHERE key=?", (key,)).fetchone()
        return row["note"] if row else ""

    def not_emailed(self, *statuses: Status) -> list[Job]:
        marks = ",".join("?" for _ in statuses)
        rows = self.db.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) AND emailed_at IS NULL "
            "ORDER BY score DESC, first_seen DESC", [st.value for st in statuses]).fetchall()
        return [self._row_to_job(r) for r in rows]

    def mark_emailed(self, keys: list[str]) -> None:
        now = _now()
        self.db.executemany("UPDATE jobs SET emailed_at=? WHERE key=?", [(now, k) for k in keys])
        self.db.commit()

    def changed_since(self, since: str, *statuses: Status) -> list[Job]:
        marks = ",".join("?" for _ in statuses)
        rows = self.db.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) AND updated_at >= ? ORDER BY updated_at DESC",
            [st.value for st in statuses] + [since]).fetchall()
        return [self._row_to_job(r) for r in rows]

    def seen_since(self, since: str) -> int:
        return self.db.execute("SELECT COUNT(*) n FROM jobs WHERE first_seen >= ?", (since,)).fetchone()["n"]

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def applied_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.db.execute(
            "SELECT COUNT(*) n FROM jobs WHERE status=? AND updated_at >= ?",
            (Status.APPLIED.value, today),
        ).fetchone()
        return row["n"]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            source=row["source"], external_id=row["external_id"], company=row["company"],
            title=row["title"], url=row["url"], apply_url=row["apply_url"] or "",
            location=row["location"] or "", remote=bool(row["remote"]),
            description=row["description"] or "", salary_min=row["salary_min"],
            salary_max=row["salary_max"], posted_at=row["posted_at"] or "",
            ats=row["ats"] or "", score=row["score"] or 0,
            reasons=json.loads(row["reasons"] or "[]"), status=Status(row["status"]),
        )
