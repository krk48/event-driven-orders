import logging
import os
from typing import Any

import httpx
from azure.identity.aio import ManagedIdentityCredential
from fastapi import HTTPException

logger = logging.getLogger(__name__)
ARM_API_VERSION = "2024-03-01"


async def get_worker_replicas() -> dict[str, Any]:
    subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
    resource_group = os.getenv("AZURE_RESOURCE_GROUP")
    app_name = os.getenv("WORKER_CONTAINER_APP_NAME")
    if not all((subscription_id, resource_group, app_name)):
        return {
            "available": False,
            "count": None,
            "replicas": [],
            "reason": "Azure Container Apps replica monitoring is not configured",
        }

    base_url = (
        "https://management.azure.com/subscriptions/"
        f"{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.App/"
        f"containerApps/{app_name}"
    )
    try:
        async with ManagedIdentityCredential() as credential:
            token = await credential.get_token("https://management.azure.com/.default")
        headers = {"Authorization": f"Bearer {token.token}"}
        async with httpx.AsyncClient(timeout=10) as client:
            revisions_response = await client.get(
                f"{base_url}/revisions",
                params={"api-version": ARM_API_VERSION},
                headers=headers,
            )
            revisions_response.raise_for_status()
            active_revisions = [
                revision
                for revision in revisions_response.json().get("value", [])
                if revision.get("properties", {}).get("active") is True
            ]
            replicas: list[dict[str, str]] = []
            for revision in active_revisions:
                revision_name = revision.get("name")
                if not revision_name:
                    continue
                response = await client.get(
                    f"{base_url}/revisions/{revision_name}/replicas",
                    params={"api-version": ARM_API_VERSION},
                    headers=headers,
                )
                response.raise_for_status()
                for replica in response.json().get("value", []):
                    properties = replica.get("properties", {})
                    replicas.append(
                        {
                            "name": replica.get("name", "unknown"),
                            "revision": revision_name,
                            "state": properties.get("runningState", "Unknown"),
                        }
                    )
        return {"available": True, "count": len(replicas), "replicas": replicas, "reason": None}
    except Exception as error:
        logger.exception("Azure Container Apps replica query failed")
        raise HTTPException(
            status_code=502,
            detail=f"Azure Container Apps replica monitoring failed: {error}",
        ) from error
