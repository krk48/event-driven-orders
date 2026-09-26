import json
import os
from typing import Any

from azure.core.exceptions import ResourceExistsError
from azure.storage.queue import QueueClient

from shared.settings import required_setting


def create_queue_client() -> QueueClient:
    client = QueueClient.from_connection_string(
        required_setting("STORAGE_CONNECTION_STRING"),
        os.getenv("QUEUE_NAME", "orders"),
    )
    try:
        client.create_queue()
    except ResourceExistsError:
        pass
    return client


def encode_order(order_id: str, product: str, amount: float) -> str:
    return json.dumps(
        {"order_id": order_id, "product": product, "amount": amount},
        separators=(",", ":"),
    )


def decode_order(content: Any) -> dict[str, Any]:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    if not isinstance(content, str):
        raise ValueError("Queue message content must be text")
    value = json.loads(content)
    if not isinstance(value, dict) or not isinstance(value.get("order_id"), str):
        raise ValueError("Queue message must contain a string order_id")
    return value
