# Requires Windows PowerShell 5.1 or newer. Invoked by the root .bat launcher.
[CmdletBinding()]
param([switch]$SetupOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot 'runtime'
$tools = Join-Path $runtime 'tools'
$frontend = Join-Path $projectRoot 'frontend'
$frontendUrl = 'http://127.0.0.1:5173'
$backendUrl = 'http://127.0.0.1:8000/health'
$services = @()
$launchLock = $null
$transcribing = $false
$exitCode = 0

function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File failed (exit $LASTEXITCODE). Check the output above and retry."
    }
}

function Save-Download {
    param([string]$Url, [string]$Destination)
    Write-Host "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -TimeoutSec 300
}

function Assert-DownloadHash {
    param([string]$Path, [string]$ExpectedHash)
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $ExpectedHash) {
        Remove-Item -LiteralPath $Path -Force
        throw 'Download checksum failed. Run the launcher again to retry.'
    }
}

function Assert-PortAvailable {
    param([int]$Port)
    $client = New-Object Net.Sockets.TcpClient
    try {
        $pending = $client.ConnectAsync('127.0.0.1', $Port)
        try { $connected = $pending.Wait(500) -and $client.Connected }
        catch { $connected = $false }
        if ($connected) {
            throw "Port $Port is in use. Stop the previous local service, then run this launcher again."
        }
    }
    finally { $client.Dispose() }
}

function Wait-Service {
    param([string]$Url, [Diagnostics.Process]$Process, [switch]$Backend)
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) { throw "A service exited before it was ready: $Url" }
        $response = $null
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        }
        catch { Start-Sleep -Milliseconds 500; continue }
        if ($Backend) {
            $health = $response.Content | ConvertFrom-Json
            if ($health.status -ne 'ok' -or $health.api_contract -ne 'market-workbench-v2') {
                throw 'The backend health response is incompatible with this project.'
            }
        }
        return
    }
    throw "Service startup timed out: $Url. See the service logs."
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'This launcher requires Windows 10/11 (64-bit).' }
    Set-Location -LiteralPath $projectRoot
    foreach ($file in @('pyproject.toml', 'uv.lock', '.python-version', 'frontend/package.json', 'frontend/package-lock.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $file))) {
            throw "Missing $file. Extract or clone the complete project before running the launcher."
        }
    }
    New-Item -ItemType Directory -Force -Path $tools | Out-Null
    try {
        $launchLock = [IO.File]::Open((Join-Path $runtime 'start-windows.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    }
    catch { throw 'Another Windows launcher is running. Use its window to stop it first.' }
    $logDir = Join-Path $runtime (Join-Path 'logs' ('windows-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')))
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    Start-Transcript -Path (Join-Path $logDir 'setup.log') | Out-Null
    $transcribing = $true
    Write-Host "Project: $projectRoot"
    Write-Host "Logs: $logDir"
    if (-not $SetupOnly) {
        Assert-PortAvailable 8000
        Assert-PortAvailable 5173
    }

    # Keep the toolchain private to this project; no admin or permanent PATH edits.
    $uvDir = Join-Path $tools 'uv'
    $uv = Join-Path $uvDir 'uv.exe'
    if (-not (Test-Path -LiteralPath $uv)) {
        $installer = Join-Path $tools 'install-uv.ps1'
        Save-Download 'https://astral.sh/uv/install.ps1' $installer
        $env:UV_UNMANAGED_INSTALL = $uvDir
        try {
            $windowsPowerShell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
            Invoke-Checked $windowsPowerShell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installer)
        }
        finally { Remove-Item Env:UV_UNMANAGED_INSTALL -ErrorAction SilentlyContinue }
        if (-not (Test-Path -LiteralPath $uv)) { throw 'uv installation did not produce uv.exe.' }
    }

    $architecture = $env:PROCESSOR_ARCHITEW6432
    if (-not $architecture) { $architecture = $env:PROCESSOR_ARCHITECTURE }
    switch ($architecture) {
        'AMD64' { $nodeArch = 'x64' }
        'ARM64' { $nodeArch = 'arm64' }
        default { throw "Unsupported Windows architecture: $architecture. Use 64-bit Windows." }
    }
    $package = Get-Content -LiteralPath (Join-Path $frontend 'package.json') -Raw | ConvertFrom-Json
    $nodeVersion = $package.devDependencies.node
    if ($nodeVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'package.json must pin an exact Node.js version.' }
    $nodeName = "node-v$nodeVersion-win-$nodeArch"
    $nodeDir = Join-Path $tools $nodeName
    $node = Join-Path $nodeDir 'node.exe'
    $npmCli = Join-Path $nodeDir 'node_modules/npm/bin/npm-cli.js'
    if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath $npmCli)) {
        $releaseUrl = "https://nodejs.org/dist/v$nodeVersion"
        $archive = Join-Path $tools "$nodeName.zip"
        $checksums = Join-Path $tools "$nodeName-SHASUMS256.txt"
        Save-Download "$releaseUrl/SHASUMS256.txt" $checksums
        $pattern = '^([0-9a-fA-F]{64})\s+' + [regex]::Escape("$nodeName.zip") + '$'
        $expectedHash = $null
        foreach ($line in (Get-Content -LiteralPath $checksums)) {
            if ($line -match $pattern) { $expectedHash = $Matches[1]; break }
        }
        if (-not $expectedHash) { throw "No official SHA-256 checksum found for $nodeName.zip." }
        Save-Download "$releaseUrl/$nodeName.zip" $archive
        Assert-DownloadHash $archive $expectedHash
        Expand-Archive -LiteralPath $archive -DestinationPath $tools -Force
        Remove-Item -LiteralPath $archive -Force
    }
    $env:PATH = "$nodeDir;$uvDir;" + $env:PATH
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $tools 'python'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $runtime 'venv-windows'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONUNBUFFERED = '1'
    # Do not let an unrelated activated environment change this project's interpreter.
    Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
    $pythonVersion = (Get-Content -LiteralPath (Join-Path $projectRoot '.python-version') -Raw).Trim()
    # Some locked native Python wheels only target x64; Windows 11 ARM uses emulation.
    $pythonRequest = "cpython-$pythonVersion-windows-x86_64-none"
    Write-Host '[1/3] Preparing Python and locked backend dependencies...'
    Invoke-Checked $uv @('sync', '--locked', '--managed-python', '--python', $pythonRequest)

    # A failed npm ci leaves no stamp, so the next launch retries installation.
    $lockHash = (Get-FileHash -LiteralPath (Join-Path $frontend 'package-lock.json') -Algorithm SHA256).Hash
    $packageHash = (Get-FileHash -LiteralPath (Join-Path $frontend 'package.json') -Algorithm SHA256).Hash
    $dependencyKey = "${lockHash}:${packageHash}:${nodeVersion}:${nodeArch}"
    $stamp = Join-Path $frontend 'node_modules/.windows-dependencies'
    $installedKey = if (Test-Path -LiteralPath $stamp) { (Get-Content -LiteralPath $stamp -Raw).Trim() } else { '' }
    Write-Host '[2/3] Preparing locked frontend dependencies...'
    if ($installedKey -ne $dependencyKey -or -not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules/vite/bin/vite.js'))) {
        Push-Location -LiteralPath $frontend
        try { Invoke-Checked $node @($npmCli, 'ci', '--include=dev', '--no-audit', '--no-fund') }
        finally { Pop-Location }
        Set-Content -LiteralPath $stamp -Value $dependencyKey -Encoding ASCII
    }
    if ($SetupOnly) { Write-Host 'Environment is ready.' }
    else {
        Assert-PortAvailable 8000
        Assert-PortAvailable 5173
        Write-Host '[3/3] Starting local services...'
        $python = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts/python.exe'
        $backendOptions = @{
            FilePath = $python
            ArgumentList = @('-m', 'uvicorn', 'backend.api.main:app', '--host', '127.0.0.1', '--port', '8000')
            WorkingDirectory = $projectRoot
            NoNewWindow = $true
            PassThru = $true
            RedirectStandardOutput = Join-Path $logDir 'backend.log'
            RedirectStandardError = Join-Path $logDir 'backend-error.log'
        }
        $backend = Start-Process @backendOptions
        $services += $backend
        Wait-Service $backendUrl $backend -Backend
        # Launch Vite directly: no npm.cmd quoting or extra cmd.exe process tree.
        $webOptions = @{
            FilePath = $node
            ArgumentList = @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', '5173', '--strictPort')
            WorkingDirectory = $frontend
            NoNewWindow = $true
            PassThru = $true
            RedirectStandardOutput = Join-Path $logDir 'frontend.log'
            RedirectStandardError = Join-Path $logDir 'frontend-error.log'
        }
        $web = Start-Process @webOptions
        $services += $web
        Wait-Service $frontendUrl $web
        # Verify the frontend proxy reaches the expected backend before opening it.
        Wait-Service "$frontendUrl/health" $web -Backend
        try { Start-Process $frontendUrl }
        catch { Write-Host "Open your browser manually: $frontendUrl" }
        Write-Host "Ready: $frontendUrl"
        Write-Host 'Keep this window open. Press Enter to stop both services (or Ctrl+C).'
        while ($true) {
            foreach ($service in $services) {
                $service.Refresh()
                if ($service.HasExited) { throw "A local service stopped (exit $($service.ExitCode)). See $logDir" }
            }
            if ([Console]::KeyAvailable -and [Console]::ReadKey($true).Key -eq 'Enter') { break }
            Start-Sleep -Milliseconds 500
        }
    }
}
catch {
    $exitCode = 1
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Check your network/proxy and the logs above, then double-click the launcher to retry.'
    if (Get-Variable logDir -ErrorAction SilentlyContinue) {
        Get-ChildItem -LiteralPath $logDir -Filter '*-error.log' | ForEach-Object {
            Write-Host $_.Name
            Get-Content -LiteralPath $_.FullName -Tail 15
        }
    }
}
finally {
    foreach ($service in $services) {
        $service.Refresh()
        if (-not $service.HasExited) {
            try { & "$env:SystemRoot\System32\taskkill.exe" /PID $service.Id /T /F 2>&1 | Out-Null }
            catch { Write-Host "Could not stop process $($service.Id): $($_.Exception.Message)" }
        }
        $service.Dispose()
    }
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($null -ne $launchLock) { $launchLock.Dispose() }
}
exit $exitCode
