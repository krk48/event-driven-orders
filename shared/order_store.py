import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode

Disposition = Literal["claimed", "completed", "busy", "missing"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


@dataclass(frozen=True)
class ClaimResult:
    disposition: Disposition
    attempt: int = 0


class OrderStore(Protocol):
    def initialize(self) -> None: ...

    def create_order(self, order_id: str, product: str, amount: float) -> None: ...

    def claim(self, order_id: str, worker_id: str, lease_seconds: int) -> ClaimResult: ...

    def mark_completed(self, order_id: str, worker_id: str) -> None: ...

    def release_for_retry(self, order_id: str, worker_id: str, error: str) -> None: ...

    def mark_failed(self, order_id: str, worker_id: str, error: str) -> None: ...

    def mark_enqueue_failed(self, order_id: str, error: str) -> None: ...

    def get_statistics(self) -> dict[str, int]: ...

    def list_recent_orders(self, limit: int = 10) -> list[dict[str, Any]]: ...


class SQLiteOrderStore:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def initialize(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.database_path)), exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    product TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    worker_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claim_until TEXT,
                    error TEXT
                )
                """
            )

    def create_order(self, order_id: str, product: str, amount: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO orders
                   (order_id, product, amount, status, created_at)
                   VALUES (?, ?, ?, 'QUEUED', ?)""",
                (order_id, product, amount, timestamp()),
            )

    def claim(self, order_id: str, worker_id: str, lease_seconds: int) -> ClaimResult:
        now = utc_now()
        lease_until = timestamp(now + timedelta(seconds=lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, attempts, claim_until FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                return ClaimResult("missing")
            if row["status"] == "COMPLETED":
                return ClaimResult("completed", row["attempts"])
            if row["status"] == "PROCESSING" and row["claim_until"] and row["claim_until"] > timestamp(now):
                return ClaimResult("busy", row["attempts"])
            if row["status"] not in ("QUEUED", "FAILED", "PROCESSING"):
                return ClaimResult("busy", row["attempts"])
            cursor = connection.execute(
                """UPDATE orders
                   SET status = 'PROCESSING', worker_id = ?, attempts = attempts + 1,
                       claim_until = ?, error = NULL
                   WHERE order_id = ?""",
                (worker_id, lease_until, order_id),
            )
            return ClaimResult("claimed", row["attempts"] + 1) if cursor.rowcount else ClaimResult("busy")

    def mark_completed(self, order_id: str, worker_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE orders SET status = 'COMPLETED', completed_at = ?,
                   claim_until = NULL, error = NULL
                   WHERE order_id = ? AND status = 'PROCESSING' AND worker_id = ?""",
                (timestamp(), order_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Could not mark order {order_id} completed; processing claim was lost")

    def release_for_retry(self, order_id: str, worker_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE orders SET status = 'QUEUED', claim_until = NULL, error = ?
                   WHERE order_id = ? AND status = 'PROCESSING' AND worker_id = ?""",
                (error[:1000], order_id, worker_id),
            )

    def mark_failed(self, order_id: str, worker_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE orders SET status = 'FAILED', completed_at = ?, claim_until = NULL, error = ?
                   WHERE order_id = ? AND status = 'PROCESSING' AND worker_id = ?""",
                (timestamp(), error[:1000], order_id, worker_id),
            )

    def mark_enqueue_failed(self, order_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE orders SET status = 'FAILED', completed_at = ?, error = ?
                   WHERE order_id = ? AND status = 'QUEUED'""",
                (timestamp(), error[:1000], order_id),
            )

    def get_statistics(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM orders GROUP BY status"
            ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        return {
            "total": sum(counts.values()),
            "queued": counts.get("QUEUED", 0),
            "processing": counts.get("PROCESSING", 0),
            "completed": counts.get("COMPLETED", 0),
            "failed": counts.get("FAILED", 0),
        }

    def list_recent_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


class AzureTableOrderStore:
    def __init__(self, connection_string: str, table_name: str):
        self.connection_string = connection_string
        self.table_name = table_name
        self._table = None

    def initialize(self) -> None:
        service = TableServiceClient.from_connection_string(self.connection_string)
        service.create_table_if_not_exists(self.table_name)
        self._table = service.get_table_client(self.table_name)

    @property
    def table(self):
        if self._table is None:
            raise RuntimeError("Azure Table order store has not been initialized")
        return self._table

    @staticmethod
    def _entity_data(entity: Any) -> dict[str, Any]:
        return {
            "order_id": entity["RowKey"],
            "product": entity["product"],
            "amount": entity["amount"],
            "status": entity["status"],
            "created_at": entity["created_at"],
            "completed_at": entity.get("completed_at"),
            "worker_id": entity.get("worker_id"),
            "attempts": entity.get("attempts", 0),
            "error": entity.get("error"),
        }

    def create_order(self, order_id: str, product: str, amount: float) -> None:
        self.table.create_entity(
            {
                "PartitionKey": "orders",
                "RowKey": order_id,
                "product": product,
                "amount": amount,
                "status": "QUEUED",
                "created_at": timestamp(),
                "attempts": 0,
            }
        )

    def _mutate(self, order_id: str, change: Any) -> dict[str, Any] | None:
        for attempt in range(8):
            try:
                entity = self.table.get_entity("orders", order_id)
            except ResourceNotFoundError:
                return None
            outcome = change(entity)
            if outcome is None:
                return self._entity_data(entity)
            etag = entity.metadata.get("etag")
            try:
                self.table.update_entity(
                    entity,
                    mode=UpdateMode.REPLACE,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
                return self._entity_data(entity)
            except ResourceModifiedError:
                if attempt == 7:
                    raise RuntimeError(f"Order {order_id} changed too frequently to update safely")
                time.sleep(0.01 * (attempt + 1))
        raise RuntimeError(f"Could not update order {order_id}")

    def claim(self, order_id: str, worker_id: str, lease_seconds: int) -> ClaimResult:
        result: ClaimResult | None = None

        def change(entity: Any) -> bool | None:
            nonlocal result
            now = utc_now()
            status = entity["status"]
            attempts = entity.get("attempts", 0)
            if status == "COMPLETED":
                result = ClaimResult("completed", attempts)
                return None
            claim_until = entity.get("claim_until")
            if status == "PROCESSING" and claim_until and claim_until > timestamp(now):
                result = ClaimResult("busy", attempts)
                return None
            if status not in ("QUEUED", "FAILED", "PROCESSING"):
                result = ClaimResult("busy", attempts)
                return None
            entity["status"] = "PROCESSING"
            entity["worker_id"] = worker_id
            entity["attempts"] = attempts + 1
            entity["claim_until"] = timestamp(now + timedelta(seconds=lease_seconds))
            entity.pop("error", None)
            result = ClaimResult("claimed", attempts + 1)
            return True

        try:
            self._mutate(order_id, change)
        except RuntimeError as error:
            if "changed too frequently" in str(error):
                return ClaimResult("busy")
            raise
        return result or ClaimResult("missing")

    def mark_completed(self, order_id: str, worker_id: str) -> None:
        def change(entity: Any) -> bool | None:
            if entity["status"] != "PROCESSING" or entity.get("worker_id") != worker_id:
                raise RuntimeError(f"Could not mark order {order_id} completed; processing claim was lost")
            entity["status"] = "COMPLETED"
            entity["completed_at"] = timestamp()
            entity.pop("claim_until", None)
            entity.pop("error", None)
            return True

        if self._mutate(order_id, change) is None:
            raise RuntimeError(f"Order {order_id} does not exist")

    def release_for_retry(self, order_id: str, worker_id: str, error: str) -> None:
        def change(entity: Any) -> bool | None:
            if entity["status"] == "PROCESSING" and entity.get("worker_id") == worker_id:
                entity["status"] = "QUEUED"
                entity.pop("claim_until", None)
                entity["error"] = error[:1000]
                return True
            return None

        self._mutate(order_id, change)

    def mark_failed(self, order_id: str, worker_id: str, error: str) -> None:
        def change(entity: Any) -> bool | None:
            if entity["status"] == "PROCESSING" and entity.get("worker_id") == worker_id:
                entity["status"] = "FAILED"
                entity["completed_at"] = timestamp()
                entity.pop("claim_until", None)
                entity["error"] = error[:1000]
                return True
            return None

        self._mutate(order_id, change)

    def mark_enqueue_failed(self, order_id: str, error: str) -> None:
        def change(entity: Any) -> bool | None:
            if entity["status"] == "QUEUED":
                entity["status"] = "FAILED"
                entity["completed_at"] = timestamp()
                entity["error"] = error[:1000]
                return True
            return None

        self._mutate(order_id, change)

    def _list_entities(self) -> list[Any]:
        return list(self.table.query_entities("PartitionKey eq 'orders'"))

    def get_statistics(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entity in self._list_entities():
            status = entity["status"]
            counts[status] = counts.get(status, 0) + 1
        return {
            "total": sum(counts.values()),
            "queued": counts.get("QUEUED", 0),
            "processing": counts.get("PROCESSING", 0),
            "completed": counts.get("COMPLETED", 0),
            "failed": counts.get("FAILED", 0),
        }

    def list_recent_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        entities = sorted(self._list_entities(), key=lambda entity: entity["created_at"], reverse=True)
        return [self._entity_data(entity) for entity in entities[:limit]]


def create_order_store() -> OrderStore:
    backend = os.getenv("STATE_BACKEND", "sqlite").lower()
    if backend == "sqlite":
        return SQLiteOrderStore(os.getenv("DATABASE_PATH", "/data/orders.db"))
    if backend == "azure_table":
        connection_string = os.getenv("STORAGE_CONNECTION_STRING")
        if not connection_string:
            raise RuntimeError("STORAGE_CONNECTION_STRING is required for Azure Table Storage")
        return AzureTableOrderStore(
            connection_string,
            os.getenv("ORDERS_TABLE_NAME", "OrderState"),
        )
    raise RuntimeError(f"Unsupported STATE_BACKEND: {backend}")
