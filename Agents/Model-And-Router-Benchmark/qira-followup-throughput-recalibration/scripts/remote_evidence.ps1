<#
.SYNOPSIS
    Run commands on, or move files to/from, the benchmark VM through Azure Run Command
    (ARM control plane) - no inbound SSH port is ever opened.

.DESCRIPTION
    Uses the Azure CLI session token of the current user. Files are moved in
    base64 chunks inside Run Command output, so this is only meant for small
    scripts and result files; the SHA256 of every transfer is verified.
    Downloads retain .part and .part.json for restart after transient failures.
    Requires PowerShell 7.2+ and Azure CLI.

.EXAMPLE
    .\remote_evidence.ps1 -SubscriptionId YOUR-SUBSCRIPTION-ID -ResourceGroup YOUR-RESOURCE-GROUP `
        -VmName qira-bench-vm -Mode Download `
        -RemotePath /home/azureuser/bench/outputs/evidence_20260909_120534.json.xz `
        -LocalPath .\outputs\evidence_20260909_120534.json.xz
#>
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$ResourceGroup,
    [string]$VmName = 'qira-bench-vm',
    [string]$RemoteRoot = '/home/azureuser/bench/',
    [ValidateSet('Run', 'Upload', 'Download')]
    [string]$Mode = 'Run',
    [string]$Script,
    [string]$LocalPath,
    [string]$RemotePath
)
$ErrorActionPreference = 'Stop'
$vmUri = "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Compute/virtualMachines/$VmName/runCommand?api-version=2024-07-01"
$token = az account get-access-token --subscription $SubscriptionId --resource https://management.azure.com/ --query accessToken -o tsv
if ($LASTEXITCODE -ne 0) { throw 'Cannot obtain Azure management token.' }
$headers = @{ Authorization = "Bearer $token" }

function Invoke-Guest([string]$Code) {
    $body = @{ commandId = 'RunShellScript'; script = @($Code) } | ConvertTo-Json -Depth 5 -Compress
    $response = Invoke-WebRequest -Method Post -Uri $vmUri -Headers $headers -ContentType 'application/json' -Body $body
    $pollUri = [string]($response.Headers['Azure-AsyncOperation'] | Select-Object -First 1)
    if (-not $pollUri) { throw 'Azure did not return an operation URL.' }
    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 3
        $operation = Invoke-RestMethod -Uri $pollUri -Headers $headers
        if ($operation.status -eq 'Failed' -or $operation.status -eq 'Canceled') {
            throw ($operation | ConvertTo-Json -Depth 12)
        }
        if ((Get-Date) -gt $deadline) { throw 'Run Command timed out; inspect the operation before retrying.' }
    } while ($operation.status -ne 'Succeeded')
    $values = $operation.properties.output.value
    if (-not $values) {
        $location = [string]($response.Headers['Location'] | Select-Object -First 1)
        if ($location) {
            $result = Invoke-RestMethod -Uri $location -Headers $headers
            $values = $result.value
        }
    }
    if (-not $values) { throw 'Run Command completed but returned no output.' }
    return (($values | ForEach-Object { $_.message }) -join "`n")
}

function Invoke-Python([string]$Code) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    return Invoke-Guest "echo '$encoded' | base64 -d | /usr/bin/python3"
}

if ($Mode -eq 'Run') {
    Invoke-Guest $Script
    return
}
if (-not $RemotePath.StartsWith($RemoteRoot, [StringComparison]::Ordinal)) {
    throw "Remote path must be inside the benchmark directory ($RemoteRoot)."
}
$remoteLiteral = $RemotePath | ConvertTo-Json -Compress
if ($Mode -eq 'Upload') {
    $bytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $LocalPath))
    $encoded = [Convert]::ToBase64String($bytes)
    $hash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
    $code = "from pathlib import Path; import base64,hashlib; p=Path($remoteLiteral); b=base64.b64decode('$encoded'); assert hashlib.sha256(b).hexdigest()=='$hash'; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b); print('UPLOAD_OK $hash')"
    $message = Invoke-Python $code
    if (-not $message.Contains("UPLOAD_OK $hash")) { throw $message }
    Write-Output "Uploaded $([IO.Path]::GetFileName($LocalPath)); SHA256 verified."
    return
}
function Invoke-DownloadRead([string]$Code) {
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        try { return Invoke-Python $Code }
        catch {
            $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
            $transportFailure = $false
            for ($cause = $_.Exception; $null -ne $cause; $cause = $cause.InnerException) {
                if ($cause -is [System.Net.Http.HttpRequestException] -or
                    $cause -is [System.IO.IOException] -or $cause -is [System.Net.WebException]) {
                    $transportFailure = $true
                }
            }
            if ($attempt -eq 6 -or ($status -notin @(401, 408, 409, 429, 500, 502, 503, 504) -and
                -not ($status -eq 0 -and $transportFailure))) { throw }
            Write-Warning "Read-only transfer attempt $attempt failed (HTTP $status); retrying."
            if ($status -eq 401) {
                $fresh = az account get-access-token --subscription $SubscriptionId --resource https://management.azure.com/ --query accessToken -o tsv
                if ($LASTEXITCODE -ne 0) { throw 'Cannot refresh Azure management token.' }
                $headers.Authorization = "Bearer $fresh"
            }
            Start-Sleep -Seconds ([Math]::Min(30, 5 * $attempt))
        }
    }
}

$LocalPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LocalPath)
if (Test-Path -LiteralPath $LocalPath) { throw "Destination exists: $LocalPath" }
$manifestOutput = Invoke-DownloadRead "from pathlib import Path; import json,hashlib; b=Path($remoteLiteral).read_bytes(); print('MANIFEST '+json.dumps({'size':len(b),'sha256':hashlib.sha256(b).hexdigest()}))"
$match = [regex]::Match($manifestOutput, 'MANIFEST (\{[^\r\n]+\})')
if (-not $match.Success) { throw $manifestOutput }
$manifest = $match.Groups[1].Value | ConvertFrom-Json
$partial = "$LocalPath.part"
$manifestPath = "$LocalPath.part.json"
if (Test-Path -LiteralPath $partial) {
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'Partial transfer lacks its manifest.' }
    $previous = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($previous.size -ne $manifest.size -or $previous.sha256 -ne $manifest.sha256) {
        throw 'Remote file changed since the partial download; partial evidence was retained.'
    }
} else {
    $manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding utf8
}
$buffer = [IO.File]::Open($partial, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Write, [IO.FileShare]::Read)
try {
    if ($buffer.Length -gt $manifest.size) { throw 'Partial download exceeds remote file size.' }
    $offset = $buffer.Length
    $buffer.Position = $offset
    while ($offset -lt $manifest.size) {
        $message = Invoke-DownloadRead "from pathlib import Path; import base64; b=Path($remoteLiteral).read_bytes()[$offset : $offset+2900]; print('CHUNK '+base64.b64encode(b).decode())"
        $chunk = [regex]::Match($message, 'CHUNK ([A-Za-z0-9+/=]+)')
        if (-not $chunk.Success) { throw "Missing chunk at $offset : $message" }
        $bytes = [Convert]::FromBase64String($chunk.Groups[1].Value)
        $expected = [Math]::Min(2900, $manifest.size - $offset)
        if ($bytes.Length -ne $expected) { throw "Truncated chunk at $offset" }
        $buffer.Write($bytes, 0, $bytes.Length)
        $buffer.Flush($true)
        $offset += $bytes.Length
        Write-Output "Recovered $offset/$($manifest.size) bytes"
    }
    $buffer.Dispose()
    $hash = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $manifest.sha256) { throw 'Download checksum mismatch.' }
    Move-Item -LiteralPath $partial -Destination $LocalPath
    Remove-Item -LiteralPath $manifestPath
    Write-Output "Downloaded $LocalPath; SHA256 $hash verified."
} finally {
    $buffer.Dispose()
}
