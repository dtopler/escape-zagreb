# Start the Escape Room Zagreb availability explorer.
#
#   .\run.ps1              # http://127.0.0.1:8765
#   .\run.ps1 -Port 9000
#   .\run.ps1 -Rescan      # force a fresh 30-day scrape, then serve
param(
    [int]$Port = 8765,
    [int]$Days = 30,
    [switch]$Rescan
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = "C:\Users\dewat\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if (-not (Test-Path $py)) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $py) { throw "Python not found. Install Python 3.11+ or edit `$py in run.ps1." }

$env:PYTHONIOENCODING = "utf-8"
Push-Location $here
try {
    if (-not (Test-Path "data\rooms.json")) {
        Write-Host "Building room catalogue ..." -ForegroundColor Cyan
        & $py catalog.py
    }
    if ($Rescan -or -not (Test-Path "data\availability.json")) {
        Write-Host "Scanning $Days days of availability ..." -ForegroundColor Cyan
        & $py aggregate.py $Days
    }
    Write-Host "`nServing on http://127.0.0.1:$Port/  (Ctrl+C to stop)" -ForegroundColor Green
    & $py server.py --port $Port
}
finally {
    Pop-Location
}
