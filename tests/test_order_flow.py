import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.app import main
from api.app.azure_monitor import get_worker_replicas
from shared.order_store import SQLiteOrderStore
from shared.queueing import encode_order
from worker.worker import process_message


class FakeQueue:
    def __init__(self):
        self.messages = []
        self.deleted = []

    def create_queue(self):
        return None

    def close(self):
        return None

    def send_message(self, content):
        self.messages.append(content)

    def get_queue_properties(self):
        return SimpleNamespace(approximate_message_count=len(self.messages))

    def delete_message(self, message_id, pop_receipt):
        self.deleted.append((message_id, pop_receipt))


def test_api_queues_single_and_bulk_orders(tmp_path, monkeypatch):
    store = SQLiteOrderStore(str(tmp_path / "orders.db"))
    store.initialize()
    queue = FakeQueue()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "create_queue_client", lambda: queue)

    with TestClient(main.app) as client:
        single = client.post("/orders", json={"product": "Laptop", "amount": 55000})
        bulk = client.post("/orders/bulk?count=10")
        monitoring = client.get("/monitoring")
        invalid_bulk = client.post("/orders/bulk?count=101")

    assert single.status_code == 202
    assert single.json()["status"] == "QUEUED"
    assert bulk.status_code == 202
    assert bulk.json()["count"] == 10
    assert invalid_bulk.status_code == 422
    assert len(queue.messages) == 11
    assert monitoring.json()["orders"]["queued"] == 11
    assert monitoring.json()["queue_depth"] == 11


def test_order_claim_prevents_concurrent_processing(tmp_path):
    store = SQLiteOrderStore(str(tmp_path / "orders.db"))
    store.initialize()
    store.create_order("order-1", "Laptop", 55000)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda worker: store.claim("order-1", worker, lease_seconds=60),
                ("worker-a", "worker-b"),
            )
        )

    assert sorted(claim.disposition for claim in claims) == ["busy", "claimed"]


def test_worker_completes_and_deletes_queue_message(tmp_path):
    store = SQLiteOrderStore(str(tmp_path / "orders.db"))
    store.initialize()
    store.create_order("order-2", "Laptop", 55000)
    queue = FakeQueue()
    message = SimpleNamespace(
        content=encode_order("order-2", "Laptop", 55000),
        id="message-1",
        pop_receipt="receipt-1",
    )

    process_message(message, queue, store, "container-test", processing_seconds=0)

    assert store.get_statistics()["completed"] == 1
    assert queue.deleted == [("message-1", "receipt-1")]


def test_worker_does_not_delete_a_message_claimed_by_another_worker(tmp_path):
    store = SQLiteOrderStore(str(tmp_path / "orders.db"))
    store.initialize()
    store.create_order("order-3", "Laptop", 55000)
    store.claim("order-3", "first-worker", lease_seconds=60)
    queue = FakeQueue()
    message = SimpleNamespace(
        content=encode_order("order-3", "Laptop", 55000),
        id="message-2",
        pop_receipt="receipt-2",
    )

    process_message(message, queue, store, "second-worker", processing_seconds=0)

    assert store.get_statistics()["processing"] == 1
    assert queue.deleted == []


def test_worker_deletes_redelivered_message_after_order_is_completed(tmp_path):
    store = SQLiteOrderStore(str(tmp_path / "orders.db"))
    store.initialize()
    store.create_order("order-4", "Laptop", 55000)
    store.claim("order-4", "first-worker", lease_seconds=60)
    store.mark_completed("order-4", "first-worker")
    queue = FakeQueue()
    message = SimpleNamespace(
        content=encode_order("order-4", "Laptop", 55000),
        id="message-3",
        pop_receipt="receipt-3",
    )

    process_message(message, queue, store, "second-worker", processing_seconds=0)

    assert store.get_statistics()["completed"] == 1
    assert queue.deleted == [("message-3", "receipt-3")]


def test_replica_monitoring_is_explicitly_unavailable_without_azure_settings(monkeypatch):
    for key in ("AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP", "WORKER_CONTAINER_APP_NAME"):
        monkeypatch.delenv(key, raising=False)

    result = asyncio.run(get_worker_replicas())

    assert result["available"] is False
    assert result["count"] is None
    assert result["replicas"] == []
