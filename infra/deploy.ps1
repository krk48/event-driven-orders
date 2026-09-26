param(
    [string]$ResourceGroup = "rg-event-orders",
    [string]$Location = "centralindia",
    [ValidatePattern("^[a-z0-9]{5,10}$")]
    [string]$Suffix = (Get-Random -Minimum 10000 -Maximum 99999).ToString()
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$storageName = "ordstore$Suffix"
$registryName = "ordacr$Suffix"
$environmentName = "cae-orders-$Suffix"
$apiName = "orders-api"
$workerName = "orders-worker"
$frontendName = "orders-dashboard"
$identityName = "orders-acr-pull"

function Invoke-Az {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed: az $($Arguments -join ' ')"
    }
}

function Invoke-AzText {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $result = & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed: az $($Arguments -join ' ')"
    }
    return ($result | Out-String).Trim()
}

Push-Location $projectRoot
try {
    $subscriptionId = Invoke-AzText account show --query id --output tsv
    if (-not $subscriptionId) { throw "Run 'az login' and select a subscription before deployment." }

    Invoke-Az extension add --name containerapp --upgrade --yes
    Invoke-Az group create --name $ResourceGroup --location $Location --output none
    Invoke-Az storage account create --name $storageName --resource-group $ResourceGroup --location $Location --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false --output none
    $storageConnection = Invoke-AzText storage account show-connection-string --name $storageName --resource-group $ResourceGroup --query connectionString --output tsv
    Invoke-Az storage queue create --name orders --connection-string $storageConnection --output none
    Invoke-Az storage table create --name OrderState --connection-string $storageConnection --output none

    Invoke-Az acr create --name $registryName --resource-group $ResourceGroup --location $Location --sku Basic --admin-enabled false --output none
    $registryId = Invoke-AzText acr show --name $registryName --resource-group $ResourceGroup --query id --output tsv
    $loginServer = Invoke-AzText acr show --name $registryName --resource-group $ResourceGroup --query loginServer --output tsv
    $identity = Invoke-AzText identity create --name $identityName --resource-group $ResourceGroup --location $Location --output json | ConvertFrom-Json
    $pullIdentityId = $identity.id
    $pullPrincipalId = $identity.principalId
    Invoke-Az role assignment create --assignee-object-id $pullPrincipalId --assignee-principal-type ServicePrincipal --role AcrPull --scope $registryId --output none

    Invoke-Az acr build --registry $registryName --image event-orders-api:v1 --file api/Dockerfile .
    Invoke-Az acr build --registry $registryName --image event-orders-worker:v1 --file worker/Dockerfile .

    Invoke-Az containerapp env create --name $environmentName --resource-group $ResourceGroup --location $Location --output none
    $apiEnvironment = @(
        "STORAGE_CONNECTION_STRING=secretref:storage-connection",
        "QUEUE_NAME=orders",
        "STATE_BACKEND=azure_table",
        "ORDERS_TABLE_NAME=OrderState",
        "AZURE_SUBSCRIPTION_ID=$subscriptionId",
        "AZURE_RESOURCE_GROUP=$ResourceGroup",
        "WORKER_CONTAINER_APP_NAME=$workerName"
    )
    Invoke-Az containerapp create --name $apiName --resource-group $ResourceGroup --environment $environmentName --image "$loginServer/event-orders-api:v1" --registry-server $loginServer --registry-identity $pullIdentityId --user-assigned $pullIdentityId --target-port 8000 --ingress external --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 3 --secrets "storage-connection=$storageConnection" --env-vars $apiEnvironment --output none
    Invoke-Az containerapp identity assign --name $apiName --resource-group $ResourceGroup --system-assigned --user-assigned $pullIdentityId --output none
    Invoke-Az containerapp create --name $workerName --resource-group $ResourceGroup --environment $environmentName --image "$loginServer/event-orders-worker:v1" --registry-server $loginServer --registry-identity $pullIdentityId --user-assigned $pullIdentityId --cpu 0.5 --memory 1Gi --min-replicas 0 --max-replicas 10 --secrets "storage-connection=$storageConnection" --env-vars "STORAGE_CONNECTION_STRING=secretref:storage-connection" "QUEUE_NAME=orders" "STATE_BACKEND=azure_table" "ORDERS_TABLE_NAME=OrderState" "PROCESSING_SECONDS=5" "MAX_ATTEMPTS=5" --scale-rule-name queue-depth --scale-rule-type azure-queue --scale-rule-metadata "queueName=orders" "queueLength=5" "accountName=$storageName" --scale-rule-auth "connection=storage-connection" --scale-rule-polling-interval 5 --scale-rule-cooldown-period 30 --output none

    $apiPrincipalId = Invoke-AzText containerapp show --name $apiName --resource-group $ResourceGroup --query identity.principalId --output tsv
    $workerId = Invoke-AzText containerapp show --name $workerName --resource-group $ResourceGroup --query id --output tsv
    Invoke-Az role assignment create --assignee-object-id $apiPrincipalId --assignee-principal-type ServicePrincipal --role Reader --scope $workerId --output none

    $apiFqdn = Invoke-AzText containerapp show --name $apiName --resource-group $ResourceGroup --query properties.configuration.ingress.fqdn --output tsv
    Invoke-Az acr build --registry $registryName --image event-orders-dashboard:v1 --file frontend/Dockerfile --build-arg "VITE_API_URL=https://$apiFqdn" .
    Invoke-Az containerapp create --name $frontendName --resource-group $ResourceGroup --environment $environmentName --image "$loginServer/event-orders-dashboard:v1" --registry-server $loginServer --registry-identity $pullIdentityId --user-assigned $pullIdentityId --target-port 80 --ingress external --cpu 0.25 --memory 0.5Gi --min-replicas 1 --max-replicas 2 --output none

    $frontendFqdn = Invoke-AzText containerapp show --name $frontendName --resource-group $ResourceGroup --query properties.configuration.ingress.fqdn --output tsv
    Write-Host ""
    Write-Host "Dashboard: https://$frontendFqdn"
    Write-Host "API docs:  https://$apiFqdn/docs"
    Write-Host "Worker:    $workerName (min 0, max 10; Azure Queue KEDA scaler)"
    Write-Host "Storage connection string was stored as Container Apps secrets and was not written to a file."
}
finally {
    Pop-Location
}
