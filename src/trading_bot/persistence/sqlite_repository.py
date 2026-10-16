"""One transaction per command; monetary columns are strings, never SQLite REAL."""

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, closing, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from trading_bot.portfolio.models import Account, Currency, Position

TABLES = {
    "orders",
    "executions",
    "fx_exchanges",
    "cash_ledger",
    "valuations",
    "decisions",
    "api_usage",
    "benchmark_prices",
    "benchmark_investments",
    "snapshots",
    "snapshot_revisions",
    "dashboard",
}


class Session(Protocol):
    def account(self) -> Account: ...
    def initialized(self) -> bool: ...
    def save_account(self, account: Account) -> None: ...
    def get(self, table: str, key: str) -> str | None: ...
    def records(self, table: str) -> list[str]: ...
    def put(self, table: str, key: str, value: BaseModel) -> None: ...


class PortfolioRepository(Protocol):
    def transaction(self) -> AbstractContextManager[Session]: ...


class SQLiteSession:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def initialized(self) -> bool:
        return (
            self.connection.execute("SELECT 1 FROM accounts WHERE id='paper'").fetchone()
            is not None
        )

    def account(self) -> Account:
        row = self.connection.execute("SELECT payload FROM accounts WHERE id='paper'").fetchone()
        if row is None:
            raise ValueError("paper account is not initialized; run paper init")
        account = Account.model_validate_json(row[0])
        balances: dict[Currency, Decimal] = {}
        for currency, amount in self.connection.execute("SELECT currency, amount FROM balances"):
            if currency not in ("JPY", "USD"):
                raise ValueError("invalid stored currency")
            balances[currency] = Decimal(amount)
        positions = {
            symbol: Position.model_validate_json(payload)
            for symbol, payload in self.connection.execute("SELECT symbol, payload FROM positions")
        }
        return Account.model_validate(
            {**account.model_dump(), "balances": balances, "positions": positions}
        )

    def save_account(self, account: Account) -> None:
        self.connection.execute(
            "INSERT INTO accounts(id,payload) VALUES('paper',?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (account.model_dump_json(exclude={"balances", "positions"}),),
        )
        self.connection.execute("DELETE FROM balances")
        self.connection.executemany(
            "INSERT INTO balances VALUES(?,?)", [(c, str(v)) for c, v in account.balances.items()]
        )
        self.connection.execute("DELETE FROM positions")
        self.connection.executemany(
            "INSERT INTO positions VALUES(?,?)",
            [(s, p.model_dump_json()) for s, p in account.positions.items()],
        )

    @staticmethod
    def table(table: str) -> str:
        if table not in TABLES:
            raise ValueError("unknown repository table")
        return table

    def get(self, table: str, key: str) -> str | None:
        row = self.connection.execute(
            f"SELECT payload FROM {self.table(table)} WHERE id=?",
            (key,),
        ).fetchone()
        return row[0] if row else None

    def records(self, table: str) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                f"SELECT payload FROM {self.table(table)} ORDER BY rowid",
            )
        ]

    def put(self, table: str, key: str, value: BaseModel) -> None:
        # Orders / AI workflow records change state; executions and cash events are append-only.
        suffix = (
            " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload"
            if table in {"orders", "decisions", "api_usage", "snapshots", "dashboard"}
            else ""
        )
        self.connection.execute(
            f"INSERT INTO {self.table(table)}(id,payload) VALUES(?,?){suffix}",
            (key, value.model_dump_json()),
        )


class SQLiteRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS balances("
                "currency TEXT PRIMARY KEY,amount TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS positions("
                "symbol TEXT PRIMARY KEY,payload TEXT NOT NULL)"
            )
            for table in sorted(TABLES):
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
                )
            connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield SQLiteSession(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
