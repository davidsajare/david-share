<#
.SYNOPSIS
    Run commands on, or move files to/from, the benchmark VM through Azure Run Command
    (ARM control plane) - no inbound SSH port is ever opened.

.DESCRIPTION
    Uses the Azure CLI session token of the current user. Files are moved in
    base64 chunks inside Run Command output, so this is only meant for small
    scripts and result files; the SHA256 of every transfer is verified.

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
$manifestOutput = Invoke-Python "from pathlib import Path; import json,hashlib; b=Path($remoteLiteral).read_bytes(); print('MANIFEST '+json.dumps({'size':len(b),'sha256':hashlib.sha256(b).hexdigest()}))"
$match = [regex]::Match($manifestOutput, 'MANIFEST (\{[^\r\n]+\})')
if (-not $match.Success) { throw $manifestOutput }
$manifest = $match.Groups[1].Value | ConvertFrom-Json
$buffer = [IO.MemoryStream]::new()
try {
    for ($offset = 0; $offset -lt $manifest.size; $offset += 2700) {
        $message = Invoke-Python "from pathlib import Path; import base64; b=Path($remoteLiteral).read_bytes()[$offset : $offset+2700]; print('CHUNK '+base64.b64encode(b).decode())"
        $chunk = [regex]::Match($message, 'CHUNK ([A-Za-z0-9+/=]+)')
        if (-not $chunk.Success) { throw "Missing chunk at $offset : $message" }
        $bytes = [Convert]::FromBase64String($chunk.Groups[1].Value)
        $expected = [Math]::Min(2700, $manifest.size - $offset)
        if ($bytes.Length -ne $expected) { throw "Truncated chunk at $offset" }
        $buffer.Write($bytes, 0, $bytes.Length)
        Write-Output "Recovered $([Math]::Min($offset+2700,$manifest.size))/$($manifest.size) bytes"
    }
    $bytes = $buffer.ToArray()
    $hash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
    if ($hash -ne $manifest.sha256) { throw 'Download checksum mismatch.' }
    if (Test-Path -LiteralPath $LocalPath) { throw "Destination exists: $LocalPath" }
    [IO.File]::WriteAllBytes($LocalPath, $bytes)
    Write-Output "Downloaded $LocalPath; SHA256 $hash verified."
} finally {
    $buffer.Dispose()
}
