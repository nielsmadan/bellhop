import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bellhop.config import Config, load_config
from bellhop.drain import drain_work, follow_bindings, start_drainer
from bellhop.errors import BellhopError
from bellhop.hls import SessionList, list_sessions
from bellhop.identity import ProcessTable, process_table
from bellhop.models import Binding, Session
from bellhop.paths import state_dir
from bellhop.routes import Route, RouteFinder
from bellhop.store import FILE_NAME, open_store
from bellhop.transcripts import ReaderFinder
from bellhop.wiring import Wiring, wire

type Starter = Callable[[Mapping[str, str]], int | None]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Context:
    env: Mapping[str, str] = field(repr=False)
    now: Callable[[], datetime] = utc_now
    caller_umask: int = 0o022
    starter: Starter = field(default_factory=lambda: start_drainer)
    _config: Config | None = None
    _conn: sqlite3.Connection | None = None
    _sessions: SessionList | None = None
    _bindings: list[Binding] | None = None
    _table: ProcessTable | None = None
    _table_read: bool = False
    _wiring: Wiring | None = None
    _kicked: bool = False
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

    def reachable(self, route: Route, session: Session, *, strict: bool = False) -> bool:
        try:
            return route.reachable(session)
        except BellhopError as error:
            if strict:
                raise
            warning = f"cannot reach {session.harness} sessions: {error}"
            self.warnings += [] if warning in self.warnings else [warning]
            return False

    def forget_bindings(self) -> None:
        self._bindings = None

    @property
    def wiring(self) -> Wiring:
        if self._wiring is None:
            self._wiring = wire(self.env, lambda: self.config)
        return self._wiring

    @property
    def routes(self) -> RouteFinder:
        return self.wiring.routes

    @property
    def readers(self) -> ReaderFinder:
        return self.wiring.readers

    def kick(self, *, again: bool = False) -> None:
        if self._kicked and not again:
            return
        try:
            if not (state_dir(self.env) / FILE_NAME).exists():
                return
            if drain_work(self.conn, self.now()):
                self._kicked = self.starter(self.env) is not None
        except Exception as error:  # noqa: BLE001
            self._kicked = True
            self.warnings.append(f"cannot start the drainer: {error}")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
