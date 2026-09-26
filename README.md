# Event-Driven Order Processing

A hackathon prototype for the path **FastAPI → Azure Storage Queue → KEDA → Azure Container Apps workers → scale to zero**. The dashboard generates load and displays queue depth, order lifecycle statistics, recent worker/container identities, and Azure-reported worker replicas.

## Architecture

```text
React/Vite dashboard
        │ HTTPS / JSON
        ▼
FastAPI order API ─── Azure Storage Queue (orders)
        │                         │
        │ order state             │ queue depth
        ▼                         ▼
SQLite locally / Azure Table   KEDA scaler
                                  │ 0..10 replicas
                                  ▼
                         Python Container Apps worker
                         claim → wait 5s → complete → delete
```

The local Compose environment runs Azurite for the Queue and shares a SQLite volume between the API and worker. Azure uses Azure Table Storage for shared order state; Queue and Table use the same Azure Storage account. Azure Container Apps replica information is fetched by the API through the Azure Resource Manager API using its system-assigned managed identity. No Azure credential is exposed to the browser. In local mode the replica value is explicitly unavailable, not simulated.

Order processing is at-least-once at the queue transport level. Azure Queue invisibility prevents normal concurrent delivery, while an atomic SQLite transaction / Azure Table ETag claim prevents two replicas from concurrently claiming the same order. The worker completes the order before deleting its message; if deletion fails, a later delivery detects the completed order and removes the leftover message. A crashed claim becomes available after its lease expires.

## Run locally with Docker (recommended)

Requirements: Docker Desktop with Compose.

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open:

- Dashboard: http://localhost:8080
- API health: http://localhost:8000/health
- Interactive API docs: http://localhost:8000/docs

The first start builds three containers. Compose starts Azurite, API, worker, and frontend. The API and worker share the `order-data` volume. The checked-in `.env.example` uses Azurite's public development-only test key and the Compose-internal `azurite` host; never use it for Azure credentials.

To stop the services, press Ctrl+C or run `docker compose down`. Add `-v` only when you intentionally want to remove the local queue and order data volumes.

## Run services individually

Requirements: Python 3.11+ (3.12 recommended), Node.js 20+, npm, and Docker Desktop.

1. Start just the queue emulator:

   ```powershell
   docker compose up -d azurite
   ```

2. In a PowerShell API terminal, from the project root:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements-test.txt
   New-Item -ItemType Directory -Force .data | Out-Null
   $env:STORAGE_CONNECTION_STRING = "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFeqCnf2w==;QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;"
   $env:STATE_BACKEND = "sqlite"
   $env:DATABASE_PATH = "$((Get-Location).Path)\.data\orders.db"
   uvicorn api.app.main:app --reload
   ```

3. In a second PowerShell terminal from the project root, activate the same venv and run:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   $env:STORAGE_CONNECTION_STRING = "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFeqCnf2w==;QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;"
   $env:STATE_BACKEND = "sqlite"
   $env:DATABASE_PATH = "$((Get-Location).Path)\.data\orders.db"
   python -m worker.worker
   ```

4. In a third terminal:

   ```powershell
   cd frontend
   npm install
   npm run dev
   ```

   Open http://localhost:5173. The Vite development default points to the API at `http://localhost:8000`.

To stop Azurite, use `docker compose stop azurite`.

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/orders` | Queue one `{ "product": "Laptop", "amount": 55000 }` order |
| `POST` | `/orders/bulk?count=100` | Generate 1–100 demo orders |
| `GET` | `/monitoring` | Approximate queue depth, order totals/status counts, recent orders, and seen worker IDs |
| `GET` | `/monitoring/replicas` | Azure Container Apps active-revision replicas, or an explicit unavailable response |

Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/orders `
  -ContentType "application/json" -Body '{"product":"Laptop","amount":55000}'
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/orders/bulk?count=50"
Invoke-RestMethod -Uri http://localhost:8000/monitoring
```

The single-order response is `202 Accepted` with `order_id` and `status: "QUEUED"`. Bulk count is capped at 100. Order state transitions through `QUEUED`, `PROCESSING`, and `COMPLETED` or `FAILED`. The worker retries processing failures up to five deliveries by default; after the final attempt, it marks the order failed and removes the poison message.

## Tests

Install test dependencies (included in the individual-service setup), then run from the project root:

```powershell
python -m pytest
```

The tests use a temporary SQLite database and queue fakes; they do not require an Azure account or a running Azurite instance. For the UI production bundle, run:

```powershell
cd frontend
npm install
npm run build
```

## Deploy to Azure

Requirements: Azure CLI, Docker Desktop, an Azure subscription with permission to create the resources/role assignments, and a signed-in Azure CLI session. The script creates a resource group, Storage Account with `orders` queue and `OrderState` table, ACR, Container Apps Environment, API, worker, and dashboard.

```powershell
az login
az account set --subscription "<subscription-id>"
.\infra\deploy.ps1 -ResourceGroup "rg-event-orders" -Location "eastus"
```

`-Suffix` defaults to a random five-digit value. Supply your own globally unique suffix if names already exist:

```powershell
.\infra\deploy.ps1 -ResourceGroup "rg-event-orders" -Location "eastus" -Suffix "73142"
```

The script builds/pushes images through ACR Tasks, assigns a user-managed identity with `AcrPull` for image access, stores the Storage connection string as Container Apps secrets for API/worker and the scaler, and assigns the API's system-managed identity read access to the worker Container App for replica monitoring. Review and adjust resource names, region, CPU/memory, max replicas, polling, and role scope before using outside a demo subscription. The script does not delete Azure resources.

### KEDA scaling configuration

The worker Container App has an `azure-queue` scaling rule with:

- Queue: `orders`
- Target queue length: 5 messages per replica
- Minimum replicas: 0
- Maximum replicas: 10
- Polling interval: 5 seconds
- Scale-down cooldown: 30 seconds
- Queue connection string referenced from the Container Apps secret `storage-connection`

At zero visible queue depth, KEDA can scale the worker to zero. As the backlog crosses the queue-length target, KEDA requests replicas up to the configured maximum. Once the queue empties and the cooldown passes, the app returns to zero. Provisioning and platform polling add cold-start delay; zero does not mean a new order is processed immediately. The API remains at one replica for demo availability.

## Cold-start and scale-out demo

1. Open the dashboard and confirm queue depth is `0` and Azure-reported active workers is `0`. In the Azure Portal or CLI, verify the worker's replica count is also zero.
2. Submit one order from the dashboard or `POST /orders`. Record the submission time and watch when its worker ID/log entry appears. The difference demonstrates queue polling plus Container Apps cold start.
3. Select **Generate 50 Orders** or **Generate 100 Orders**. Observe queue depth increase, then KEDA request more replicas. The dashboard refreshes every four seconds; Azure replica telemetry is queried alongside monitoring.
4. View worker logs with `az containerapp logs show -g <resource-group> -n orders-worker --follow`; each processing/completion line includes the replica/hostname.
5. Let the queue drain. Watch order counts move from queued/processing to completed and active replicas return to zero after the cooldown.
6. Repeat a single order from zero to contrast the cold start with the batch scale-out.

## Troubleshooting

- **Docker API cannot connect to Azurite:** Keep `azurite` as the endpoint host for Compose; use `127.0.0.1` only for processes running directly on Windows. Confirm `docker compose ps` shows Azurite healthy and port `10001` available.
- **API/worker fail to start:** Check `docker compose logs api worker azurite`. Ensure the Storage connection string, queue name, and SQLite shared volume are identical for API and worker.
- **Orders remain queued locally:** Confirm the worker is running and points at the same Azurite queue and SQLite database. Check worker logs for queue/auth or database lock errors.
- **Replica count is unavailable locally:** This is expected; local mode does not fabricate Azure replica telemetry. In Azure, verify the API has a system-assigned identity, the `Reader` role assignment on the worker Container App, and the correct subscription/resource-group/app environment variables.
- **Azure worker does not scale:** Check the queue name, Storage secret reference, `accountName`, scaler auth, visible queue depth, and `minReplicas`/`maxReplicas` in the worker revision. The Azure Storage connection string must point at the account containing `orders`.
- **Azure images fail to pull:** Confirm the user-assigned identity has `AcrPull` on the ACR and is configured as the app's registry identity. Allow time for role assignment propagation, then restart the revision if needed.
- **Table/queue authorization errors:** Confirm API and worker Container Apps secrets reference the Storage Account connection string and the `OrderState` table/`orders` queue exist. Never paste a real connection string into source, tickets, or chat.
- **PowerShell blocks virtual environment activation:** Use `.\.venv\Scripts\python.exe -m pip ...` and `.\.venv\Scripts\uvicorn.exe ...`, or follow your organization's script execution policy.
