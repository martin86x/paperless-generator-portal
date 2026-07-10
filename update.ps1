# update.ps1 — holt den neuesten Generator-Build ins Portal und baut das Image neu.
# Ausfuehren im Ordner Generator-Portal/ (lokaler Dev-Rechner).
#
# Der klassische Generator wird NICHT veraendert — hier wird nur das fertige
# dist/index.html als Input-Artefakt uebernommen.

$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "..\Generator-Build\dist\index.html"
$dst = Join-Path $PSScriptRoot "site\index.html"

if (-not (Test-Path $src)) {
    Write-Error "Nicht gefunden: $src  (erst 'python build.py' im Generator-Build ausfuehren)"
    exit 1
}

Copy-Item $src $dst -Force
Write-Host "index.html aktualisiert ($src -> site/index.html)" -ForegroundColor Green

# Optionaler lokaler Rebuild, wenn Docker vorhanden ist
if (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Host "Baue Image neu..." -ForegroundColor Cyan
    docker compose up -d --build
} else {
    Write-Host "Docker nicht gefunden — HTML kopiert, Rebuild uebersprungen." -ForegroundColor Yellow
}
