import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bellhop.config import Config, load_config
from bellhop.drain import drain_work, follow_bindings, start_drainer
from bellhop.hls import SessionList, list_sessions
from bellhop.identity import ProcessTable, process_table
from bellhop.models import Binding
from bellhop.paths import state_dir
from bellhop.routes import RouteFinder, route_finder
from bellhop.store import open_store
from bellhop.transcripts import ReaderFinder, reader_finder

type Starter = Callable[[Mapping[str, str]], bool]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Context:
    env: Mapping[str, str]
    now: Callable[[], datetime] = utc_now
    starter: Starter = field(default_factory=lambda: start_drainer)
    _config: Config | None = None
    _conn: sqlite3.Connection | None = None
    _sessions: SessionList | None = None
    _bindings: list[Binding] | None = None
    _table: ProcessTable | None = None
    _table_read: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config(self.env)
        return self._config

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = open_store(state_dir(self.env))
        return self._conn

    @property
    def sessions(self) -> SessionList:
        if self._sessions is None:
            self._sessions = list_sessions(self.env)
            self.warnings += self._sessions.warnings
        return self._sessions

    @property
    def bindings(self) -> list[Binding]:
        if self._bindings is None:
            self._bindings = follow_bindings(
                self.conn, self.sessions.sessions, self.table, self.now()
            )
        return self._bindings

    @property
    def table(self) -> ProcessTable | None:
        if not self._table_read:
            self._table = process_table()
            self._table_read = True
        return self._table

    def forget_bindings(self) -> None:
        self._bindings = None

    @property
    def routes(self) -> RouteFinder:
        return route_finder(self.env)

    @property
    def readers(self) -> ReaderFinder:
        return reader_finder(self.env)

    def kick(self) -> None:
        if self._conn is not None and drain_work(self._conn, self.now()):
            self.starter(self.env)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
