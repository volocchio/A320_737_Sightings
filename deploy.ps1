param(
    [string]$m = "Update A320/737 Sightings",
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"
$base    = "https://a320737sightings.voloaltro.tech"
$envFile = Join-Path $PSScriptRoot ".env"

$secret = (Get-Content $envFile | Where-Object { $_ -match "^DEPLOY_SECRET=" }) -replace "^DEPLOY_SECRET=", ""
if (-not $secret) { Write-Error "DEPLOY_SECRET not found in .env"; exit 1 }

git add -A
git diff --cached --quiet 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    git commit -m $m
} else {
    Write-Host "No staged changes - skipping commit." -ForegroundColor Yellow
}

if (-not $NoPush) { git push }

$expected = (git rev-parse --short HEAD).Trim()
Write-Host ""
Write-Host "Deploying $base  commit=$expected ..." -ForegroundColor Cyan

$resp = Invoke-RestMethod -Uri "$base/webhook/deploy" `
    -Method POST `
    -Headers @{ "X-Deploy-Secret" = $secret } `
    -TimeoutSec 15
Write-Host "  webhook: $($resp.status)" -ForegroundColor Gray

Write-Host "  polling" -NoNewline -ForegroundColor Gray
$deadline = (Get-Date).AddSeconds(90)
$confirmed = $false

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    Write-Host "." -NoNewline -ForegroundColor Gray
    try {
        $v = Invoke-RestMethod -Uri "$base/_version" -TimeoutSec 5
        if ($v.commit -eq $expected) { $confirmed = $true; break }
    } catch { }
}

Write-Host ""
if ($confirmed) {
    Write-Host ""
    Write-Host "  DEPLOYED  commit=$expected" -ForegroundColor Green
    Write-Host "  started=$($v.started_at)" -ForegroundColor Green
    Write-Host "  $base/insights" -ForegroundColor DarkCyan
} else {
    Write-Host ""
    Write-Host "  TIMEOUT - container did not restart within 90s" -ForegroundColor Red
    Write-Host "  Run on VPS:  docker restart a320737_sightings" -ForegroundColor Yellow
    exit 1
}