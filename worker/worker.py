import logging
import os
import socket
import time
from typing import Any

from azure.storage.queue import QueueClient

from shared.order_store import OrderStore, create_order_store
from shared.queueing import create_queue_client, decode_order

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("order-worker")
PROCESSING_SECONDS = int(os.getenv("PROCESSING_SECONDS", "5"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "5"))
VISIBILITY_TIMEOUT = max(30, PROCESSING_SECONDS + 15)
CLAIM_LEASE_SECONDS = max(60, PROCESSING_SECONDS + 30)


def worker_identity() -> str:
    return os.getenv("CONTAINER_APP_REPLICA_NAME") or os.getenv("HOSTNAME") or socket.gethostname()


def delete_message(queue: QueueClient, message: Any) -> None:
    queue.delete_message(message.id, message.pop_receipt)


def process_message(
    message: Any,
    queue: QueueClient,
    store: OrderStore,
    worker_id: str,
    processing_seconds: int = PROCESSING_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
) -> None:
    try:
        order = decode_order(message.content)
    except (ValueError, UnicodeDecodeError):
        logger.exception("Discarding malformed queue message %s", message.id)
        delete_message(queue, message)
        return

    order_id = order["order_id"]
    claim = store.claim(order_id, worker_id, CLAIM_LEASE_SECONDS)
    if claim.disposition in ("completed", "missing"):
        if claim.disposition == "missing":
            logger.error("Queue message references unknown order %s; removing poison message", order_id)
        delete_message(queue, message)
        return
    if claim.disposition == "busy":
        logger.info("Order %s is already claimed by another worker", order_id)
        return

    logger.info(
        "Processing order_id=%s product=%s amount=%s worker=%s",
        order_id,
        order.get("product"),
        order.get("amount"),
        worker_id,
    )
    try:
        time.sleep(processing_seconds)
        store.mark_completed(order_id, worker_id)
    except Exception as error:
        logger.exception("Order %s failed on attempt %s", order_id, claim.attempt)
        if claim.attempt >= max_attempts:
            store.mark_failed(order_id, worker_id, str(error))
            delete_message(queue, message)
        else:
            store.release_for_retry(order_id, worker_id, str(error))
        return

    logger.info("Completed order_id=%s worker=%s", order_id, worker_id)
    delete_message(queue, message)


def run() -> None:
    store = create_order_store()
    store.initialize()
    queue = create_queue_client()
    worker_id = worker_identity()
    logger.info("Worker started worker=%s queue=%s", worker_id, os.getenv("QUEUE_NAME", "orders"))
    try:
        while True:
            try:
                messages = queue.receive_messages(
                    messages_per_page=1,
                    visibility_timeout=VISIBILITY_TIMEOUT,
                )
                message = next(iter(messages), None)
                if message is None:
                    time.sleep(1)
                    continue
                process_message(message, queue, store, worker_id)
            except Exception:
                logger.exception("Worker polling or queue operation failed")
                time.sleep(2)
    finally:
        queue.close()


if __name__ == "__main__":
    run()
