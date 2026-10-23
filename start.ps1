$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$workmatePython = if (Test-Path -LiteralPath '.venv\Scripts\python.exe') { Join-Path $PSScriptRoot '.venv\Scripts\python.exe' } else { 'python' }
if (-not (Test-Path -LiteralPath 'frontend\dist\index.html')) {
    throw 'Frontend build is missing. Run setup.ps1 first.'
}
Write-Host 'Workmate: http://127.0.0.1:8765  (Ctrl+C to stop)'
try {
    $workmateHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
    if ($workmateHealth.ok -and $workmateHealth.version -and $null -ne $workmateHealth.llm_configured) {
        Write-Host 'Workmate is already running. Open http://127.0.0.1:8765'
        exit 0
    }
} catch {
    # No running Workmate server; start the local process below.
}
& $workmatePython -m uvicorn backend.app:app --host 127.0.0.1 --port 8765
