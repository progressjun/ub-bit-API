"""SQLite 상태 저장소.

자동매매는 프로세스가 죽는 것을 전제로 설계해야 한다. 포지션/주문/체결은
메모리가 아니라 디스크에 남기고, 재기동 시 거래소 잔고와 대조한다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    market        TEXT PRIMARY KEY,
    entry_price   REAL NOT NULL,
    qty           REAL NOT NULL,
    entry_krw     REAL NOT NULL,
    stop_price    REAL NOT NULL,
    target_net    REAL NOT NULL,
    peak_price    REAL NOT NULL,
    bars_held     INTEGER NOT NULL DEFAULT 0,
    armed         INTEGER NOT NULL DEFAULT 0,
    opened_at     TEXT NOT NULL,
    meta          TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market        TEXT NOT NULL,
    opened_at     TEXT NOT NULL,
    closed_at     TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL NOT NULL,
    qty           REAL NOT NULL,
    entry_krw     REAL NOT NULL,
    exit_krw      REAL NOT NULL,
    fee_krw       REAL NOT NULL,
    net_pnl       REAL NOT NULL,
    net_return    REAL NOT NULL,
    exit_kind     TEXT NOT NULL,
    reason        TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    market        TEXT NOT NULL,
    side          TEXT NOT NULL,
    mode          TEXT NOT NULL,
    request       TEXT,
    response      TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts            TEXT PRIMARY KEY,
    total_krw     REAL NOT NULL,
    cash_krw      REAL NOT NULL,
    exposure_krw  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
"""


@dataclass
class Position:
    market: str
    entry_price: float
    qty: float
    entry_krw: float
    stop_price: float
    target_net: float
    peak_price: float
    bars_held: int = 0
    armed: bool = False
    opened_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    meta: dict[str, Any] = field(default_factory=dict)


class Store:
    def __init__(self, path: str = "data/ubbit.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------- positions
    def upsert_position(self, pos: Position) -> None:
        self.conn.execute(
            """INSERT INTO positions
               (market, entry_price, qty, entry_krw, stop_price, target_net, peak_price,
                bars_held, armed, opened_at, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(market) DO UPDATE SET
                 entry_price=excluded.entry_price, qty=excluded.qty, entry_krw=excluded.entry_krw,
                 stop_price=excluded.stop_price, target_net=excluded.target_net,
                 peak_price=excluded.peak_price, bars_held=excluded.bars_held,
                 armed=excluded.armed, meta=excluded.meta""",
            (
                pos.market, pos.entry_price, pos.qty, pos.entry_krw, pos.stop_price,
                pos.target_net, pos.peak_price, pos.bars_held, int(pos.armed),
                pos.opened_at, json.dumps(pos.meta, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def get_position(self, market: str) -> Position | None:
        row = self.conn.execute("SELECT * FROM positions WHERE market=?", (market,)).fetchone()
        return _row_to_position(row) if row else None

    def all_positions(self) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return [_row_to_position(r) for r in rows]

    def delete_position(self, market: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE market=?", (market,))
        self.conn.commit()

    # ---------------------------------------------------------------- trades
    def record_trade(self, **kwargs: Any) -> None:
        cols = ",".join(kwargs)
        marks = ",".join("?" * len(kwargs))
        self.conn.execute(f"INSERT INTO trades ({cols}) VALUES ({marks})", tuple(kwargs.values()))
        self.conn.commit()

    def recent_trades(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def performance(self) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT COUNT(*) n,
                      COALESCE(SUM(net_pnl),0) pnl,
                      COALESCE(SUM(fee_krw),0) fees,
                      COALESCE(SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END),0) wins,
                      COALESCE(SUM(CASE WHEN net_pnl>0 THEN net_pnl ELSE 0 END),0) gross_win,
                      COALESCE(SUM(CASE WHEN net_pnl<0 THEN -net_pnl ELSE 0 END),0) gross_loss
               FROM trades"""
        ).fetchone()
        n = row["n"] or 0
        return {
            "trades": n,
            "net_pnl": row["pnl"],
            "fees_paid": row["fees"],
            "win_rate": (row["wins"] / n) if n else 0.0,
            "profit_factor": (row["gross_win"] / row["gross_loss"]) if row["gross_loss"] else None,
            "fee_to_pnl": (row["fees"] / abs(row["pnl"])) if row["pnl"] else None,
        }

    # ---------------------------------------------------------------- orders
    def record_order(self, market: str, side: str, mode: str, request: Any, response: Any) -> None:
        self.conn.execute(
            "INSERT INTO orders (ts, market, side, mode, request, response) VALUES (?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"), market, side, mode,
                json.dumps(request, ensure_ascii=False, default=str),
                json.dumps(response, ensure_ascii=False, default=str),
            ),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- equity
    def record_equity(self, total: float, cash: float, exposure: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity (ts,total_krw,cash_krw,exposure_krw) VALUES (?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), total, cash, exposure),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        market=row["market"], entry_price=row["entry_price"], qty=row["qty"],
        entry_krw=row["entry_krw"], stop_price=row["stop_price"], target_net=row["target_net"],
        peak_price=row["peak_price"], bars_held=row["bars_held"], armed=bool(row["armed"]),
        opened_at=row["opened_at"], meta=json.loads(row["meta"] or "{}"),
    )
