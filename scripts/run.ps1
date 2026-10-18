
# Usage: .\scripts\run.ps1 ingest | serve | extract | mcp

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("ingest", "serve", "extract", "mcp")]
    [string]$cmd
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "Virtual environment not found. Run: python -m venv .venv"
    exit 1
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"

Push-Location $ProjectRoot
try {
    switch ($cmd) {
        "extract" {
            & $Python -m busfactor.extract --clone
        }
        "ingest" {
            & $Python -m busfactor.ingest
        }
        "serve" {
            & $Python -m uvicorn busfactor.server:app --host 127.0.0.1 --port 8000
        }
        "mcp" {
            & $Python -m busfactor.mcp_server
        }
    }
}
finally {
    Pop-Location
}
