from contextlib import contextmanager
import json
import sqlite3

from .codec import canonical
from .errors import DomainError


class Ledger:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signatures (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id), receipt TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_orders (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    source_sha256 TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_items (
                    order_id TEXT NOT NULL REFERENCES repair_orders(id),
                    item_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    note TEXT,
                    PRIMARY KEY (order_id, item_id)
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, ident, source, snapshot):
        with self.connect() as connection:
            inserted = connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (ident, canonical(source), canonical(snapshot)),
            )
            row = connection.execute("SELECT snapshot FROM runs WHERE id = ?", (ident,)).fetchone()
            return json.loads(row[0]), inserted.rowcount == 0

    def read(self, ident):
        with self.connect() as connection:
            row = connection.execute("SELECT source, snapshot FROM runs WHERE id = ?", (ident,)).fetchone()
            if row is None:
                raise DomainError("RUN_NOT_FOUND", "找不到此运行", 404)
            sign = connection.execute("SELECT receipt FROM signatures WHERE run_id = ?", (ident,)).fetchone()
            snapshot = json.loads(row[1])
            return json.loads(row[0]), {**snapshot, "signature": json.loads(sign[0]) if sign else None}

    def recent(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT snapshot FROM runs ORDER BY rowid DESC LIMIT 30").fetchall()
            return [json.loads(row[0])["manifest"] for row in rows]

    def sign(self, ident, receipt):
        data = canonical(receipt)
        with self.connect() as connection:
            inserted = connection.execute(
                "INSERT INTO signatures VALUES (?, ?) ON CONFLICT(run_id) DO NOTHING", (ident, data))
            saved = connection.execute("SELECT receipt FROM signatures WHERE run_id = ?", (ident,)).fetchone()[0]
            if saved != data:
                raise DomainError("ALREADY_SIGNED", "此运行已由他人签收", 409)
            return {**json.loads(saved), "reused": inserted.rowcount == 0}

    def save_repair_order(self, order, run_id, created_at):
        """幂等落库：同一工单重跑只插入缺失条目，已确认的状态与备注保持不变。"""
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO repair_orders VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (order["id"], run_id, order["source_sha256"], canonical(order), created_at))
            for item in order["items"]:
                connection.execute(
                    "INSERT INTO repair_items VALUES (?, ?, ?, ?) ON CONFLICT(order_id, item_id) DO NOTHING",
                    (order["id"], item["id"], "OPEN", None))
            saved = connection.execute(
                "SELECT payload FROM repair_orders WHERE id = ?", (order["id"],)).fetchone()
            states = {row[0]: (row[1], row[2]) for row in connection.execute(
                "SELECT item_id, state, note FROM repair_items WHERE order_id = ?", (order["id"],))}
            return json.loads(saved[0]), states

    def read_repair_order(self, order_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM repair_orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                raise DomainError("ORDER_NOT_FOUND", "找不到此修复工单", 404)
            states = {r[0]: (r[1], r[2]) for r in connection.execute(
                "SELECT item_id, state, note FROM repair_items WHERE order_id = ?", (order_id,))}
            return json.loads(row[0]), states

    def list_repair_orders(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM repair_orders ORDER BY rowid DESC LIMIT 30").fetchall()
            return [json.loads(row[0]) for row in rows]

    def update_repair_item(self, order_id, item_id, state, note=None):
        """状态机推进；已终结的条目拒绝改写，保证已确认来源不被覆盖。"""
        allowed = {"OPEN": ("CONFIRMED",), "CONFIRMED": ("REPAIRED",),
                   "REPAIRED": ("VERIFIED",), "VERIFIED": ()}
        with self.connect() as connection:
            current = connection.execute(
                "SELECT state FROM repair_items WHERE order_id = ? AND item_id = ?",
                (order_id, item_id)).fetchone()
            if current is None:
                raise DomainError("ITEM_NOT_FOUND", "找不到此工单条目", 404)
            if state not in allowed[current[0]]:
                raise DomainError("INVALID_TRANSITION", f"条目不能由 {current[0]} 变为 {state}", 409)
            connection.execute(
                "UPDATE repair_items SET state = ?, note = ? WHERE order_id = ? AND item_id = ?",
                (state, note, order_id, item_id))
            return state
