import json
import sqlite3
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path("storage/agent_state.db")


class ScheduleDB:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS calendar_meta (
                    user_id TEXT PRIMARY KEY,
                    current_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS committed_actions (
                    idempotency_key TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    committed_at TEXT NOT NULL
                );
                """
            )

    def ensure_version(self, user_id: str, initial_version: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT current_version FROM calendar_meta WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row:
                return row["current_version"]
            conn.execute(
                "INSERT INTO calendar_meta (user_id, current_version) VALUES (?, ?)",
                (user_id, initial_version),
            )
            return initial_version

    def get_current_version(self, user_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT current_version FROM calendar_meta WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row["current_version"] if row else None

    def bump_version(self, user_id: str, new_version: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO calendar_meta (user_id, current_version) VALUES (?, ?)",
                (user_id, new_version),
            )

    def record_approval(self, approval_id: str, user_id: str, expected_version: str, status: str = "APPROVED") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO approvals (approval_id, user_id, status, expected_version) VALUES (?, ?, ?, ?)",
                (approval_id, user_id, status, expected_version),
            )

    def get_approval(self, approval_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_committed_by_key(self, idempotency_key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM committed_actions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            return dict(row) if row else None

    def save_committed_action(
        self, idempotency_key: str, event_id: str, user_id: str, approval_id: str, payload: dict, committed_at: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO committed_actions
                (idempotency_key, event_id, user_id, approval_id, payload_json, committed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (idempotency_key, event_id, user_id, approval_id, json.dumps(payload, ensure_ascii=False), committed_at),
            )
