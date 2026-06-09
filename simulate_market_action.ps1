param(
    [string]$ScrapeMarket = "",
    [string]$ZipCode = "",
    [switch]$ResetBeforeRun,
    [switch]$RunStorageCheck,
    [switch]$InstallDeps,
    [switch]$InstallPlaywright
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python venv not found at .venv. Create it first."
}

Write-Host "[simulate] repo=$repoRoot"

$marketAliasToName = @{
    "rossi_departamentos" = "Rossi"
    "atacadao_departamentos" = "Atacadão"
    "nagumo_departamentos" = "Nagumo"
    "higas_departamentos" = "Higas"
    "swift_departamentos" = "Swift"
    "sonda_departamentos" = "Sonda Delivery"
    "xsupermercados_departamentos" = "XSupermercados"
    "barbosa_departamentos" = "Barbosa"
    "carrefour_departamentos" = "Carrefour"
    "oba_departamentos" = "Oba Hortifruti"
    "samsclub_departamentos" = "Sam's Club"
    "extra_departamentos" = "Extra"
    "paodeacucar_departamentos" = "Pão de Açúcar"
    "davo_departamentos" = "Davo"
    "giga_departamentos" = "Giga"
}

if ($InstallDeps) {
    Write-Host "[simulate] Installing dependencies from requirements.txt"
    & $pythonExe -m pip install -r requirements.txt
}

if ($InstallPlaywright) {
    Write-Host "[simulate] Installing Playwright Chromium"
    & $pythonExe -m playwright install chromium
}

if (Test-Path ".env") {
    Write-Host "[simulate] Loading .env variables"
    Get-Content ".env" |
        Where-Object { $_ -and -not $_.Trim().StartsWith("#") -and $_.Contains("=") } |
        ForEach-Object {
            $parts = $_.Split("=", 2)
            [Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process")
        }
}

if ($ScrapeMarket) {
    $normalized = $ScrapeMarket.Trim()
    $lookup = $normalized.ToLowerInvariant()
    if ($marketAliasToName.ContainsKey($lookup)) {
        $normalized = $marketAliasToName[$lookup]
    }
    [Environment]::SetEnvironmentVariable("SCRAPE_MARKET", $normalized, "Process")
    [Environment]::SetEnvironmentVariable("MARKET_NAME", $normalized, "Process")
}

if ($ZipCode) {
    [Environment]::SetEnvironmentVariable("SCRAPE_ZIP_CODE", $ZipCode, "Process")
}

# Optional local optimization: avoid CLIP startup warning/overhead in CI-like runs.
if (-not [Environment]::GetEnvironmentVariable("IMAGE_MATCH_ENABLED", "Process")) {
    [Environment]::SetEnvironmentVariable("IMAGE_MATCH_ENABLED", "0", "Process")
}

Write-Host "[simulate] SCRAPE_MARKET=$([Environment]::GetEnvironmentVariable('SCRAPE_MARKET','Process'))"
Write-Host "[simulate] MARKET_NAME=$([Environment]::GetEnvironmentVariable('MARKET_NAME','Process'))"
Write-Host "[simulate] SCRAPE_ZIP_CODE=$([Environment]::GetEnvironmentVariable('SCRAPE_ZIP_CODE','Process'))"
Write-Host "[simulate] IMAGE_MATCH_ENABLED=$([Environment]::GetEnvironmentVariable('IMAGE_MATCH_ENABLED','Process'))"

if ($ResetBeforeRun) {
    Write-Host "[simulate] Resetting Postgres tables"
    & $pythonExe reset_data.py postgres --yes
}

Write-Host "[simulate] Running main.py"
& $pythonExe main.py
$mainExit = $LASTEXITCODE

if ($mainExit -ne 0) {
    Write-Host "[simulate] main.py failed with exit code $mainExit"
    exit $mainExit
}

if ($RunStorageCheck) {
    Write-Host "[simulate] Running storage controller (--force)"
    & $pythonExe -m db.storage_controller --force
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Write-Host "[simulate] Completed successfully"
