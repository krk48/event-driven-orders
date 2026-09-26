import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.app.azure_monitor import get_worker_replicas
from shared.order_store import OrderStore, create_order_store
from shared.queueing import create_queue_client, encode_order

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
store: OrderStore = create_order_store()
queue_client = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global queue_client
    store.initialize()
    queue_client = create_queue_client()
    yield
    if queue_client is not None:
        queue_client.close()


app = FastAPI(
    title="Event-Driven Orders API",
    version="1.0.0",
    description="Queue-backed order submission and autoscaling demo monitoring.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class OrderRequest(BaseModel):
    product: str = Field(min_length=1, max_length=120)
    amount: float = Field(gt=0, le=1_000_000_000)


def get_queue():
    if queue_client is None:
        raise HTTPException(status_code=503, detail="Queue client is not ready")
    return queue_client


def enqueue_order(product: str, amount: float) -> str:
    order_id = str(uuid4())
    store.create_order(order_id, product, amount)
    try:
        get_queue().send_message(encode_order(order_id, product, amount))
    except Exception as error:
        store.mark_enqueue_failed(order_id, str(error))
        logger.exception("Failed to enqueue order %s", order_id)
        raise HTTPException(status_code=503, detail="Could not enqueue order") from error
    return order_id


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/orders", status_code=202)
def create_order(order: OrderRequest) -> dict[str, str]:
    order_id = enqueue_order(order.product, order.amount)
    return {"order_id": order_id, "status": "QUEUED"}


@app.post("/orders/bulk", status_code=202)
def create_bulk_orders(count: int = Query(ge=1, le=100)) -> dict[str, Any]:
    orders = [
        {"order_id": enqueue_order("Demo Product", 1000 + index), "status": "QUEUED"}
        for index in range(count)
    ]
    return {"count": len(orders), "orders": orders}


@app.get("/monitoring")
def monitoring() -> dict[str, Any]:
    try:
        queue_depth = get_queue().get_queue_properties().approximate_message_count or 0
        recent_orders = store.list_recent_orders(20)
        workers = list(
            dict.fromkeys(
                order["worker_id"]
                for order in recent_orders
                if order.get("worker_id")
            )
        )
        return {
            "queue_depth": queue_depth,
            "orders": store.get_statistics(),
            "recent_orders": recent_orders,
            "worker_containers": workers,
        }
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Monitoring data retrieval failed")
        raise HTTPException(status_code=503, detail="Monitoring data is temporarily unavailable") from error


@app.get("/monitoring/replicas")
async def replica_monitoring() -> dict[str, Any]:
    return await get_worker_replicas()
