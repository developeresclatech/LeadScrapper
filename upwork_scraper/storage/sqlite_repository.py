"""SQLite-backed lead checkpoints and upload state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from ..pipeline.processor import ProcessedLead
from ..pipeline.deduplicator import RunDeduplicator
from ..models import JobLead
from ..analyzer import LeadAnalysis


class SQLiteLeadRepository:
    """Persist qualified leads as they pass through the pipeline."""

    def __init__(self, path: str, run_id: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._lock = Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                platform_worker TEXT NOT NULL,
                keyword TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                lead_json TEXT NOT NULL,
                analysis_json TEXT NOT NULL,
                uploaded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, dedup_key)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                run_number INTEGER NOT NULL,
                run_id TEXT NOT NULL UNIQUE,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(run_date, run_number)
            )
            """
        )
        columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(daily_runs)"
            ).fetchall()
        }
        if "completed_at" not in columns:
            self._connection.execute(
                "ALTER TABLE daily_runs ADD COLUMN completed_at TEXT"
            )
        self._connection.commit()

        # Only new writes enter the outbox: historical rows may have been
        # accepted under older recency/qualification rules.
        self._connection.execute("""CREATE TABLE IF NOT EXISTS upload_outbox (
            lead_id INTEGER PRIMARY KEY REFERENCES processed_leads(id))""")
        self._connection.execute("""CREATE TABLE IF NOT EXISTS search_checkpoints (
            identity TEXT PRIMARY KEY, keyword TEXT NOT NULL,
            lead_json TEXT NOT NULL, checked_json TEXT)""")
        self._connection.commit()

    def checkpoint_search(self, keyword: str, leads: list[JobLead]) -> None:
        with self._lock:
            for lead in leads:
                self._connection.execute("""INSERT INTO search_checkpoints
                    (identity, keyword, lead_json) VALUES (?, ?, ?)
                    ON CONFLICT(identity) DO UPDATE SET
                        keyword=excluded.keyword, lead_json=excluded.lead_json""",
                    (RunDeduplicator.key(lead), keyword, lead.model_dump_json()))
            self._connection.commit()

    def completed_details(self, lead: JobLead) -> JobLead | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT checked_json FROM search_checkpoints WHERE identity=?",
                (RunDeduplicator.key(lead),),
            ).fetchone()
        return JobLead.model_validate_json(row[0]) if row and row[0] else None

    def pending_uploads(self):
        with self._lock:
            rows = self._connection.execute("""SELECT p.id, p.lead_json, p.analysis_json
                FROM processed_leads p JOIN upload_outbox o ON o.lead_id=p.id
                WHERE p.uploaded=0 ORDER BY p.id""").fetchall()
        return [(ProcessedLead(JobLead.model_validate_json(lead), LeadAnalysis(**json.loads(analysis))),
                 f"outbox:{identifier}") for identifier, lead, analysis in rows]

    def latest_completed_at(self) -> str | None:
        """Return the most recent successful run completion timestamp."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT completed_at
                FROM daily_runs
                WHERE completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row[0]) if row else None

    def claim_daily_run(self, run_date: str, started_at: str) -> int:
        """Atomically reserve and return this local day's run number."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """
                SELECT COALESCE(MAX(run_number), 0)
                FROM daily_runs
                WHERE run_date = ?
                """,
                (run_date,),
            ).fetchone()
            run_number = int(row[0]) + 1
            self._connection.execute(
                """
                INSERT INTO daily_runs (
                    run_date, run_number, run_id, started_at
                ) VALUES (?, ?, ?, ?)
                """,
                (run_date, run_number, self.run_id, started_at),
            )
            self._connection.commit()
            return run_number

    def mark_run_completed(self, completed_at: str | None = None) -> None:
        """Mark this run complete so future runs can calculate a catch-up gap."""
        timestamp = completed_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """
                UPDATE daily_runs
                SET completed_at = ?
                WHERE run_id = ?
                """,
                (timestamp, self.run_id),
            )
            self._connection.commit()

    def save(
        self,
        platform_worker: str,
        keyword: str,
        item: ProcessedLead,
        dedup_key: str,
    ) -> str:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO processed_leads (
                    run_id, platform_worker, keyword, dedup_key,
                    lead_json, analysis_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.run_id,
                    platform_worker,
                    keyword,
                    dedup_key,
                    json.dumps(item.lead.model_dump(), default=str),
                    json.dumps(asdict(item.analysis), default=str),
                ),
            )
            row_id = self._connection.execute(
                "SELECT id FROM processed_leads WHERE run_id=? AND dedup_key=?",
                (self.run_id, dedup_key),
            ).fetchone()[0]
            self._connection.execute("INSERT OR IGNORE INTO upload_outbox VALUES (?)", (row_id,))
            if item.lead.attachment_status in {"Yes", "No"}:
                self._connection.execute(
                    "UPDATE search_checkpoints SET checked_json=? WHERE identity=?",
                    (item.lead.model_dump_json(), RunDeduplicator.key(item.lead)),
                )
            self._connection.commit()
            return f"outbox:{row_id}"

    def mark_uploaded(self, dedup_keys: list[str]) -> None:
        if not dedup_keys:
            return
        placeholders = ",".join("?" for _ in dedup_keys)
        with self._lock:
            for key in dedup_keys:
                if key.startswith("outbox:"):
                    self._connection.execute("UPDATE processed_leads SET uploaded=1 WHERE id=?",
                                             (int(key.split(":", 1)[1]),))
            self._connection.execute(
                f"""
                UPDATE processed_leads
                SET uploaded = 1
                WHERE run_id = ? AND dedup_key IN ({placeholders})
                """,
                [self.run_id, *dedup_keys],
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()
