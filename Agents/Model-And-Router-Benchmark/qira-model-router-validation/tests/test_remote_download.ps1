$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot '..\scripts\remote_evidence.ps1'
$source = Get-Content -LiteralPath $scriptPath -Raw
$start = $source.IndexOf('$LocalPath = $ExecutionContext')
if ($start -lt 0) { throw 'Download implementation not found.' }
$download = [scriptblock]::Create($source.Substring($start))
$remoteBytes = [byte[]]::new(16001)
[Security.Cryptography.RandomNumberGenerator]::Fill($remoteBytes)
$manifestJson = @{size=$remoteBytes.Length; sha256=[Convert]::ToHexString(
    [Security.Cryptography.SHA256]::HashData($remoteBytes)).ToLowerInvariant()} | ConvertTo-Json -Compress
$reads = 0
$failAfter = 2

function Invoke-DownloadRead([string]$Code) {
    if ($Code.Contains("'MANIFEST '")) { return "MANIFEST $manifestJson" }
    if ($script:reads -eq $script:failAfter) { throw 'Simulated read interruption' }
    $script:reads++
    $m = [regex]::Match($Code, 'read_bytes\(\)\[(\d+) :')
    if (-not $m.Success) { throw 'Unexpected read command.' }
    $offset = [int]$m.Groups[1].Value
    $end = [Math]::Min($remoteBytes.Length - 1, $offset + 2899)
    return 'CHUNK ' + [Convert]::ToBase64String([byte[]]$remoteBytes[$offset..$end])
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("qira-transfer-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $testRoot | Out-Null
Push-Location $testRoot
try {
    $LocalPath = '.\evidence.bin'
    $RemoteLiteral = '"/home/azureuser/bench/outputs/evidence.bin"'
    try {
        . $download
        throw 'Expected an interrupted download.'
    } catch {
        if ($_.Exception.Message -ne 'Simulated read interruption') { throw }
    }
    if ((Get-Item '.\evidence.bin.part').Length -ne 5800) { throw 'Verified chunks were not persisted.' }
    if (Test-Path '.\evidence.bin') { throw 'Incomplete download was published.' }
    $failAfter = -1
    $LocalPath = '.\evidence.bin'
    . $download
    if ((Get-FileHash '.\evidence.bin').Hash.ToLowerInvariant() -ne ($manifestJson | ConvertFrom-Json).sha256) {
        throw 'Resumed bytes differ from the source.'
    }
    if (Test-Path '.\evidence.bin.part.json') { throw 'Completed manifest was not cleaned up.' }
    try {
        $LocalPath = '.\evidence.bin'
        . $download
        throw 'Expected existing destination rejection.'
    } catch {
        if ($_.Exception.Message -notlike 'Destination exists:*') { throw }
    }
    [IO.File]::WriteAllBytes((Join-Path $testRoot 'changed.bin.part'), [byte[]](1, 2))
    @{size=1;sha256='wrong'} | ConvertTo-Json | Set-Content '.\changed.bin.part.json'
    try {
        $LocalPath = '.\changed.bin'
        . $download
        throw 'Expected changed remote file rejection.'
    } catch {
        if ($_.Exception.Message -notlike 'Remote file changed*') { throw }
    }
    'PASS: interruption, durable resume, relative path, checksum, existing destination, changed remote.'
} finally {
    Pop-Location
    foreach ($name in @('evidence.bin', 'evidence.bin.part', 'evidence.bin.part.json', 'changed.bin.part', 'changed.bin.part.json')) {
        $file = Join-Path $testRoot $name
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
    }
    Remove-Item -LiteralPath $testRoot
}
