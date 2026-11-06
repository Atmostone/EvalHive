"""A bounded SQLite WAL journal, committed before any network delivery."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from evalhive_harness import encode_line, validate


class SpoolFull(ValueError):
    pass


class EventSpool:
    def __init__(self, path: str | Path, *, max_pending_bytes: int = 64 * 1024 * 1024):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self.max_pending_bytes = max_pending_bytes
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL, size INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, response TEXT
            );
            CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO counters VALUES ('next_seq', 0);
            INSERT OR IGNORE INTO counters VALUES ('acked_through', -1);
        """)
        self.db.commit()

    @property
    def next_seq(self) -> int:
        return self.db.execute("SELECT value FROM counters WHERE name = 'next_seq'").fetchone()[0]

    @property
    def pending_bytes(self) -> int:
        return self.db.execute("SELECT COALESCE(SUM(size), 0) FROM events").fetchone()[0]

    @property
    def pending_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def append(self, event: dict) -> None:
        validate("event", event)
        data = encode_line(event)
        if event["seq"] != self.next_seq:
            raise ValueError("Spool events must be contiguous")
        # Keep one bounded record available for a terminal failure when full.
        reserve = 1024 * 1024 if event["event"] != "run.finished" else 0
        if self.pending_bytes + len(data) + reserve > self.max_pending_bytes:
            raise SpoolFull("Unacknowledged event journal reached its size limit")
        with self.db:
            self.db.execute(
                "INSERT INTO events (seq, event_id, body, size) VALUES (?, ?, ?, ?)",
                (event["seq"], event["event_id"], data.decode(), len(data)),
            )
            self.db.execute(
                "UPDATE counters SET value = ? WHERE name = 'next_seq'", (event["seq"] + 1,)
            )

    def batch(
        self, *, max_records: int = 128, max_bytes: int = 4 * 1024 * 1024 - 1024
    ) -> list[dict]:
        # JSON arrays add commas; leave space for the envelope and separators.
        result, size = [], 0
        for body, record_size in self.db.execute(
            "SELECT body, size FROM events ORDER BY seq LIMIT ?", (max_records,)
        ):
            if size + record_size + 1 > max_bytes:
                break
            result.append(json.loads(body))
            size += record_size + 1
        return result

    def acknowledge(self, through: int) -> None:
        if type(through) is not int or through < -1 or through >= self.next_seq:
            raise ValueError("Server acknowledgement is outside the local journal")
        with self.db:
            self.db.execute("DELETE FROM events WHERE seq <= ?", (through,))
            self.db.execute(
                "UPDATE counters SET value = MAX(value, ?) WHERE name = 'acked_through'", (through,)
            )

    def command(self, command_id: str) -> tuple[str, dict | None] | None:
        row = self.db.execute(
            "SELECT state, response FROM commands WHERE id = ?", (command_id,)
        ).fetchone()
        return None if row is None else (row[0], json.loads(row[1]) if row[1] is not None else None)

    def claim_run(self) -> None:
        if self.db.execute("SELECT 1 FROM counters WHERE name = 'claimed'").fetchone():
            raise ValueError("An attempt journal cannot start another connector execution")
        with self.db:
            self.db.execute("INSERT INTO counters (name, value) VALUES ('claimed', 1)")

    def command_started(self, command_id: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO commands (id, state) VALUES (?, 'executing')", (command_id,)
            )

    def command_finished(self, command_id: str, response: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE commands SET state = 'complete', response = ? WHERE id = ?",
                (json.dumps(response, ensure_ascii=False, allow_nan=False), command_id),
            )

    def completed_commands(self) -> list[tuple[str, dict]]:
        return [
            (row[0], json.loads(row[1]))
            for row in self.db.execute("SELECT id, response FROM commands WHERE state = 'complete'")
        ]

    def command_acknowledged(self, command_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM commands WHERE id = ?", (command_id,))

    def close(self) -> None:
        self.db.close()
        # WAL/SHM are adjacent to the private file; permissions also follow the
        # private directory, rather than exposing journal data as artifacts.

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
