param(
    [string]$ResourceGroup = "rg-event-orders",
    [string]$Location = "centralindia",
    [string]$SubscriptionId = "5d3b92c0-8e17-4a9a-bf79-93e79dd19297",
    [string]$StorageAccountName,
    [string]$EnvironmentName
)

$ErrorActionPreference = "Stop"
$registryName = "ordacr38588"
$expectedLoginServer = "ordacr38588.azurecr.io"
$apiName = "orders-api"
$workerName = "orders-worker"
$dashboardName = "orders-dashboard"
$identityName = "orders-acr-pull"
$queueName = "orders"
$tableName = "OrderState"
$previousStorageConnectionString = $env:AZURE_STORAGE_CONNECTION_STRING

function Invoke-Az {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed with exit code $LASTEXITCODE. Review the Azure CLI output above."
    }
}

function Invoke-AzText {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    $result = & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed with exit code $LASTEXITCODE. Review the Azure CLI output above."
    }
    return ($result -join "`n").Trim()
}

function Invoke-AzJson {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    $json = Invoke-AzText @Arguments
    if (-not $json) {
        return @()
    }
    return ConvertFrom-Json -InputObject $json
}

function Ensure-RoleAssignment {
    param(
        [string]$PrincipalId,
        [string]$RoleName,
        [string]$Scope
    )

    $assignments = @(Invoke-AzJson role assignment list --assignee-object-id $PrincipalId --scope $Scope --all --output json)
    if (-not ($assignments | Where-Object { $_.roleDefinitionName -eq $RoleName })) {
        Invoke-Az role assignment create --assignee-object-id $PrincipalId --assignee-principal-type ServicePrincipal --role $RoleName --scope $Scope --output none
    }
}

try {
    try {
        $currentSubscriptionId = Invoke-AzText account show --query id --output tsv
    }
    catch {
        throw "Azure CLI login could not be verified. Run 'az login' and try again."
    }
    if (-not $currentSubscriptionId) {
        throw "Azure CLI is not logged in. Run 'az login' and try again."
    }

    Invoke-Az account set --subscription $SubscriptionId

    Invoke-Az extension add --name containerapp --upgrade --yes

    $resourceGroupExists = Invoke-AzText group exists --name $ResourceGroup
    if ($resourceGroupExists -ne "true") {
        Invoke-Az group create --name $ResourceGroup --location $Location --output none
    }

    $storageAccounts = @(Invoke-AzJson storage account list --resource-group $ResourceGroup --output json)
    if ($StorageAccountName) {
        $storageName = $StorageAccountName
        $matchingStorage = @($storageAccounts | Where-Object { $_.name -eq $storageName })
        if (-not $matchingStorage) {
            Invoke-Az storage account create --name $storageName --resource-group $ResourceGroup --location $Location --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false --output none
        }
    }
    else {
        $matchingStorage = @($storageAccounts | Where-Object { $_.name -like "ordstore*" })
        if ($matchingStorage.Count -gt 1) {
            throw "Multiple ordstore* storage accounts exist in '$ResourceGroup'. Re-run with -StorageAccountName to select the intended account."
        }
        if ($matchingStorage.Count -eq 1) {
            $storageName = $matchingStorage[0].name
        }
        else {
            $storageName = "ordstore$(Get-Random -Minimum 10000 -Maximum 99999)"
            Invoke-Az storage account create --name $storageName --resource-group $ResourceGroup --location $Location --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false --output none
        }
    }

    $storageConnectionString = Invoke-AzText storage account show-connection-string --name $storageName --resource-group $ResourceGroup --query connectionString --output tsv
    if (-not $storageConnectionString) {
        throw "Could not retrieve a connection string for storage account '$storageName'."
    }

    $env:AZURE_STORAGE_CONNECTION_STRING = $storageConnectionString
    Invoke-Az storage queue create --name $queueName --output none
    Invoke-Az storage table create --name $tableName --output none

    $registry = Invoke-AzJson acr show --name $registryName --resource-group $ResourceGroup --output json
    $loginServer = $registry.loginServer
    if ($loginServer -ne $expectedLoginServer) {
        throw "ACR '$registryName' was not found at the expected login server '$expectedLoginServer'."
    }
    $registryId = $registry.id

    $identities = @(Invoke-AzJson identity list --resource-group $ResourceGroup --output json)
    $pullIdentity = $identities | Where-Object { $_.name -eq $identityName } | Select-Object -First 1
    if (-not $pullIdentity) {
        $pullIdentity = Invoke-AzJson identity create --name $identityName --resource-group $ResourceGroup --location $Location --output json
    }
    $pullIdentityId = $pullIdentity.id
    $pullPrincipalId = $pullIdentity.principalId
    if (-not $pullIdentityId -or -not $pullPrincipalId) {
        throw "Managed identity '$identityName' does not have a resource ID and principal ID."
    }
    Ensure-RoleAssignment -PrincipalId $pullPrincipalId -RoleName "AcrPull" -Scope $registryId

    if ($EnvironmentName) {
        $selectedEnvironmentName = $EnvironmentName
    }
    else {
        $environments = @(Invoke-AzJson containerapp env list --resource-group $ResourceGroup --output json)
        $matchingEnvironments = @($environments | Where-Object { $_.name -like "cae-orders*" })
        if ($matchingEnvironments.Count -gt 1) {
            throw "Multiple cae-orders* Container Apps environments exist in '$ResourceGroup'. Re-run with -EnvironmentName to select the intended environment."
        }
        if ($matchingEnvironments.Count -eq 1) {
            $selectedEnvironmentName = $matchingEnvironments[0].name
        }
        else {
            $selectedEnvironmentName = "cae-orders"
        }
    }

    $environments = @(Invoke-AzJson containerapp env list --resource-group $ResourceGroup --output json)
    $existingEnvironment = $environments | Where-Object { $_.name -eq $selectedEnvironmentName } | Select-Object -First 1
    if (-not $existingEnvironment) {
        Invoke-Az containerapp env create --name $selectedEnvironmentName --resource-group $ResourceGroup --location $Location --output none
    }

    $containerApps = @(Invoke-AzJson containerapp list --resource-group $ResourceGroup --output json)
    $apiImage = "ordacr38588.azurecr.io/event-orders-api:latest"
    $workerImage = "ordacr38588.azurecr.io/event-orders-worker:latest"
    $dashboardImage = "ordacr38588.azurecr.io/event-orders-dashboard:latest"
    $apiEnvironment = @(
        "STORAGE_CONNECTION_STRING=secretref:storage-connection",
        "QUEUE_NAME=$queueName",
        "STATE_BACKEND=azure_table",
        "ORDERS_TABLE_NAME=$tableName",
        "AZURE_SUBSCRIPTION_ID=$SubscriptionId",
        "AZURE_RESOURCE_GROUP=$ResourceGroup",
        "WORKER_CONTAINER_APP_NAME=$workerName"
    )
    $workerEnvironment = @(
        "STORAGE_CONNECTION_STRING=secretref:storage-connection",
        "QUEUE_NAME=$queueName",
        "STATE_BACKEND=azure_table",
        "ORDERS_TABLE_NAME=$tableName",
        "PROCESSING_SECONDS=5",
        "MAX_ATTEMPTS=5"
    )

    $existingApi = $containerApps | Where-Object { $_.name -eq $apiName } | Select-Object -First 1
    if ($existingApi) {
        Invoke-Az containerapp identity assign --name $apiName --resource-group $ResourceGroup --system-assigned --user-assigned $pullIdentityId --output none
        Invoke-Az containerapp secret set --name $apiName --resource-group $ResourceGroup --secrets "storage-connection=$storageConnectionString" --output none
        Invoke-Az containerapp update --name $apiName --resource-group $ResourceGroup --image $apiImage --replace-env-vars @apiEnvironment --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 3 --output none
        Invoke-Az containerapp ingress enable --name $apiName --resource-group $ResourceGroup --type external --target-port 8000 --output none
    }
    else {
        Invoke-Az containerapp create --name $apiName --resource-group $ResourceGroup --environment $selectedEnvironmentName --image $apiImage --registry-server $loginServer --registry-identity $pullIdentityId --user-assigned $pullIdentityId --system-assigned --target-port 8000 --ingress external --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 3 --secrets "storage-connection=$storageConnectionString" --env-vars @apiEnvironment --output none
    }

    $existingWorker = $containerApps | Where-Object { $_.name -eq $workerName } | Select-Object -First 1
    if ($existingWorker) {
        Invoke-Az containerapp identity assign --name $workerName --resource-group $ResourceGroup --user-assigned $pullIdentityId --output none
        Invoke-Az containerapp secret set --name $workerName --resource-group $ResourceGroup --secrets "storage-connection=$storageConnectionString" --output none
        Invoke-Az containerapp update --name $workerName --resource-group $ResourceGroup --image $workerImage --replace-env-vars @workerEnvironment --cpu 0.5 --memory 1Gi --min-replicas 0 --max-replicas 10 --scale-rule-name queue-depth --scale-rule-type azure-queue --scale-rule-metadata "queueName=$queueName" "queueLength=5" "accountName=$storageName" --scale-rule-auth "connection=storage-connection" --scale-rule-polling-interval 5 --scale-rule-cooldown-period 30 --output none
    }
    else {
        Invoke-Az containerapp create --name $workerName --resource-group $ResourceGroup --environment $selectedEnvironmentName --image $workerImage --registry-server $loginServer --registry-identity $pullIdentityId --user-assigned $pullIdentityId --cpu 0.5 --memory 1Gi --min-replicas 0 --max-replicas 10 --secrets "storage-connection=$storageConnectionString" --env-vars @workerEnvironment --scale-rule-name queue-depth --scale-rule-type azure-queue --scale-rule-metadata "queueName=$queueName" "queueLength=5" "accountName=$storageName" --scale-rule-auth "connection=storage-connection" --scale-rule-polling-interval 5 --scale-rule-cooldown-period 30 --output none
    }

    $existingDashboard = $containerApps | Where-Object { $_.name -eq $dashboardName } | Select-Object -First 1
    if ($existingDashboard) {
        Invoke-Az containerapp identity assign --name $dashboardName --resource-group $ResourceGroup --user-assigned $pullIdentityId --output none
        Invoke-Az containerapp update --name $dashboardName --resource-group $ResourceGroup --image $dashboardImage --cpu 0.25 --memory 0.5Gi --min-replicas 1 --max-replicas 2 --output none
        Invoke-Az containerapp ingress enable --name $dashboardName --resource-group $ResourceGroup --type external --target-port 80 --output none
    }
    else {
        Invoke-Az containerapp create --name $dashboardName --resource-group $ResourceGroup --environment $selectedEnvironmentName --image $dashboardImage --registry-server $loginServer --registry-identity $pullIdentityId --user-assigned $pullIdentityId --target-port 80 --ingress external --cpu 0.25 --memory 0.5Gi --min-replicas 1 --max-replicas 2 --output none
    }

    $apiPrincipalId = Invoke-AzText containerapp show --name $apiName --resource-group $ResourceGroup --query identity.principalId --output tsv
    $workerResourceId = Invoke-AzText containerapp show --name $workerName --resource-group $ResourceGroup --query id --output tsv
    if (-not $apiPrincipalId -or -not $workerResourceId) {
        throw "Could not resolve the API managed identity or worker Container App resource ID."
    }
    Ensure-RoleAssignment -PrincipalId $apiPrincipalId -RoleName "Reader" -Scope $workerResourceId

    $apiFqdn = Invoke-AzText containerapp show --name $apiName --resource-group $ResourceGroup --query properties.configuration.ingress.fqdn --output tsv
    $dashboardFqdn = Invoke-AzText containerapp show --name $dashboardName --resource-group $ResourceGroup --query properties.configuration.ingress.fqdn --output tsv
    if (-not $apiFqdn -or -not $dashboardFqdn) {
        throw "Could not resolve the API and dashboard Container App hostnames."
    }

    Write-Host ""
    Write-Host "Dashboard URL: https://$dashboardFqdn"
    Write-Host "API URL:       https://$apiFqdn"
    Write-Host "API docs URL:  https://$apiFqdn/docs"
    Write-Host "Worker:        $workerName"
    Write-Host "ACR:           $registryName"
    Write-Host "Resource group: $ResourceGroup"
}
finally {
    if ($null -eq $previousStorageConnectionString) {
        Remove-Item Env:AZURE_STORAGE_CONNECTION_STRING -ErrorAction SilentlyContinue
    }
    else {
        $env:AZURE_STORAGE_CONNECTION_STRING = $previousStorageConnectionString
    }
}
