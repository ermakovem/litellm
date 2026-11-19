from contextlib import closing, contextmanager
from dataclasses import dataclass
from math import inf, isfinite, nextafter
from pathlib import Path
import sqlite3
from typing import Final, Iterator


@dataclass(frozen=True, slots=True)
class Snapshot:
    used_percent: float
    resets_at: int
    observed_at: int


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    source: str
    window: str
    share_percent: float
    allocated_points: float
    remaining_percent: float
    provider_remaining_percent: float
    resets_at: int
    observed_at: int
    unallocated_points: float


_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS subscription_share (
    source TEXT NOT NULL, user_id TEXT NOT NULL, percent REAL NOT NULL,
    PRIMARY KEY (source, user_id)
);
CREATE TABLE IF NOT EXISTS subscription_window (
    source TEXT NOT NULL, window TEXT NOT NULL, resets_at INTEGER NOT NULL,
    observed_at INTEGER NOT NULL, used_percent REAL NOT NULL,
    through INTEGER NOT NULL, unallocated REAL NOT NULL,
    PRIMARY KEY (source, window)
);
CREATE TABLE IF NOT EXISTS subscription_charge (
    source TEXT NOT NULL, window TEXT NOT NULL, user_id TEXT NOT NULL, points REAL NOT NULL,
    PRIMARY KEY (source, window, user_id)
);
CREATE TABLE IF NOT EXISTS subscription_request (
    request_id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT NOT NULL,
    recorded_at INTEGER NOT NULL, weight REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS subscription_request_period
    ON subscription_request(source, recorded_at);
"""


class QuotaLedger:
    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def set_share(self, source: str, user_id: str, percent: float) -> None:
        if not isfinite(percent) or not 0 <= percent <= 100:
            raise ValueError("A share must be between 0 and 100 percent")
        with self._write() as db:
            assigned: Final = db.execute(
                "SELECT COALESCE(SUM(percent), 0) FROM subscription_share WHERE source=? AND user_id<>?",
                (source, user_id),
            ).fetchone()[0]
            if assigned + percent > 100:
                raise ValueError("Assigned shares may not exceed 100 percent")
            db.execute(
                "INSERT INTO subscription_share VALUES (?, ?, ?) "
                "ON CONFLICT(source, user_id) DO UPDATE SET percent=excluded.percent",
                (source, user_id, percent),
            )

    def record(self, request_id: str, source: str, user_id: str, recorded_at: float, weight: float) -> None:
        if not isfinite(weight) or weight <= 0:
            raise ValueError("A request weight must be finite and positive")
        with self._write() as db:
            through: Final = db.execute(
                "SELECT MAX(through) FROM subscription_window WHERE source=?",
                (source,),
            ).fetchone()[0]
            eligible_at: Final = max(recorded_at, nextafter(through, inf)) if through is not None else recorded_at
            db.execute(
                "INSERT OR IGNORE INTO subscription_request VALUES (?, ?, ?, ?, ?)",
                (request_id, source, user_id, eligible_at, weight),
            )

    def observe(self, source: str, window: str, snapshot: Snapshot) -> None:
        if not 0 <= snapshot.used_percent <= 100 or snapshot.resets_at <= snapshot.observed_at:
            raise ValueError("Invalid quota snapshot")
        with self._write() as db:
            previous: Final = db.execute(
                "SELECT resets_at, observed_at, used_percent, through, unallocated "
                "FROM subscription_window WHERE source=? AND window=?",
                (source, window),
            ).fetchone()
            if previous is None:
                db.execute(
                    "INSERT INTO subscription_window VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (source, window, snapshot.resets_at, snapshot.observed_at,
                     snapshot.used_percent, snapshot.observed_at),
                )
                return

            resets_at, observed_at, used_percent, through, unallocated = previous
            if snapshot.observed_at <= observed_at:
                return
            if snapshot.resets_at != resets_at:
                if snapshot.observed_at < resets_at:
                    raise ValueError("The previous quota window is still active")
                db.execute("DELETE FROM subscription_charge WHERE source=? AND window=?", (source, window))
                self._settle(db, source, window, resets_at, snapshot, snapshot.used_percent, 0)
                return
            if snapshot.used_percent < used_percent:
                return
            self._settle(db, source, window, through, snapshot, snapshot.used_percent - used_percent, unallocated)

    @staticmethod
    def _settle(
        db: sqlite3.Connection, source: str, window: str, through: int,
        snapshot: Snapshot, delta: float, unallocated: float,
    ) -> None:
        weights: Final = db.execute(
            "SELECT user_id, SUM(weight) FROM subscription_request "
            "WHERE source=? AND recorded_at>? AND recorded_at<=? GROUP BY user_id",
            (source, through, snapshot.observed_at),
        ).fetchall() if delta else ()
        total: Final = sum(weight for _, weight in weights)
        db.executemany(
            "INSERT INTO subscription_charge VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source, window, user_id) DO UPDATE SET points=points+excluded.points",
            ((source, window, user_id, delta * (weight / total)) for user_id, weight in weights),
        )
        db.execute(
            "UPDATE subscription_window SET resets_at=?, observed_at=?, used_percent=?, through=?, unallocated=? "
            "WHERE source=? AND window=?",
            (snapshot.resets_at, snapshot.observed_at, snapshot.used_percent,
             snapshot.observed_at if delta else through, unallocated + (0 if total else delta), source, window),
        )

    def shares(self) -> list[tuple[str, str, float]]:
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT source, user_id, percent FROM subscription_share ORDER BY source, user_id").fetchall()

    def usage(self, user_id: str) -> list[QuotaUsage]:
        with closing(sqlite3.connect(self.path)) as db:
            rows: Final = db.execute(
                "SELECT s.source, w.window, s.percent, COALESCE(c.points, 0), w.used_percent, "
                "w.resets_at, w.observed_at, w.unallocated "
                "FROM subscription_share s JOIN subscription_window w USING (source) "
                "LEFT JOIN subscription_charge c ON c.source=w.source AND c.window=w.window AND c.user_id=s.user_id "
                "WHERE s.user_id=? ORDER BY s.source, w.window",
                (user_id,),
            ).fetchall()
        return [
            QuotaUsage(source, window, share, points, max(0.0, 100 * (1 - points / share)) if share else 0,
                       100 - used, reset, observed, unallocated)
            for source, window, share, points, used, reset, observed, unallocated in rows
        ]
