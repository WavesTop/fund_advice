# Standalone launcher checks; no Pester or project dependencies required.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$path = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts/start_windows.ps1'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
# Load helpers without running downloads, installing packages or starting the app.
foreach ($definition in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Invoke-Expression $definition.Extent.Text
}

function Assert-Throws {
    param([scriptblock]$Action, [string]$Message)
    try { & $Action }
    catch {
        if ($_.Exception.Message -notlike "*$Message*") { throw }
        return
    }
    throw "Expected an error containing: $Message"
}

$tempDir = Join-Path ([IO.Path]::GetTempPath()) ('launcher-check-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tempDir | Out-Null
$current = [Diagnostics.Process]::GetCurrentProcess()
$shell = $current.MainModule.FileName
$listener = $null
$child = $null
try {
    Invoke-Checked $shell @('-NoProfile', '-Command', 'exit 0')
    Assert-Throws { Invoke-Checked $shell @('-NoProfile', '-Command', 'exit 7') } 'exit 7'
    Write-Host 'PASS: native command success and failure'

    $download = Join-Path $tempDir 'download.zip'
    [IO.File]::WriteAllText($download, 'verified contents')
    $hash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash
    Assert-DownloadHash $download $hash
    [IO.File]::WriteAllText($download, 'corrupt contents')
    Assert-Throws { Assert-DownloadHash $download $hash } 'checksum failed'
    if (Test-Path -LiteralPath $download) { throw 'Corrupt download was kept.' }
    Write-Host 'PASS: checksum validation rejects and removes corrupt downloads'

    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    Assert-Throws { Assert-PortAvailable $port } 'is in use'
    $listener.Stop()
    Assert-PortAvailable $port
    Write-Host 'PASS: occupied ports are rejected; free ports are accepted'

    function Invoke-WebRequest { return @{ Content = '{"status":"ok","api_contract":"market-workbench-v2"}' } }
    Wait-Service 'http://test.invalid/health' $current -Backend
    function Invoke-WebRequest { return @{ Content = '{"status":"ok","api_contract":"old"}' } }
    Assert-Throws { Wait-Service 'http://test.invalid/health' $current -Backend } 'incompatible'
    Write-Host 'PASS: backend health contract validation'

    $script:attempts = 0
    function Invoke-WebRequest {
        $script:attempts++
        if ($script:attempts -eq 1) { throw 'Not ready yet' }
        return @{ Content = '{"status":"ok","api_contract":"market-workbench-v2"}' }
    }
    Wait-Service 'http://test.invalid/health' $current -Backend
    if ($script:attempts -ne 2) { throw 'Health check did not retry.' }
    Write-Host 'PASS: transient startup errors are retried'

    $child = Start-Process -FilePath $shell -ArgumentList @('-NoProfile', '-Command', 'exit 0') -PassThru -Wait
    Assert-Throws { Wait-Service 'http://test.invalid/health' $child -Backend } 'exited before'
    Write-Host 'PASS: early service exit aborts startup'
    Write-Host 'All Windows launcher checks passed.'
}
finally {
    if ($null -ne $listener) { $listener.Stop() }
    if ($null -ne $child) { $child.Dispose() }
    $current.Dispose()
    Remove-Item -LiteralPath $tempDir -Recurse -Force
}
