"""SQLite access. Fail closed if the DB is unusable."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.storage import connect, init_db, utcnow


class DatabaseError(Exception):
    pass


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self.cx = connect(self.path)
            init_db(self.cx)
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc
        self._tx_depth = 0

    @contextmanager
    def transaction(self):
        """Commit several writes together. A failure rolls the whole batch back."""
        self._tx_depth += 1
        try:
            yield
            if self._tx_depth == 1:
                self.cx.commit()
        except Exception:
            if self._tx_depth == 1:
                try:
                    self.cx.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            self._tx_depth -= 1

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        try:
            cur = self.cx.execute(sql, params)
            if self._tx_depth == 0:
                self.cx.commit()
            return cur
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        try:
            self.cx.executemany(sql, seq)
            if self._tx_depth == 0:
                self.cx.commit()
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        try:
            return list(self.cx.execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        try:
            return self.cx.execute(sql, params).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def close(self) -> None:
        self.cx.close()
