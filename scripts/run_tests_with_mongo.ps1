# Starts a local Mongo container, runs pytest, then tears it down.
# Usage: Open PowerShell in repo root and run:
#   .\scripts\run_tests_with_mongo.ps1

$ErrorActionPreference = 'Stop'
Write-Host "Bringing up test MongoDB container..."
docker-compose -f docker-compose.test.yml up -d

# Wait a bit for Mongo to be ready (healthcheck retries in compose also help)
Write-Host "Waiting for MongoDB to become available..."
Start-Sleep -Seconds 6

# Export MONGODB_URL for the test run
$env:MONGODB_URL = "mongodb://localhost:27017"

# Run pytest
try {
    & env\Scripts\python.exe -m pytest -q
    $exitCode = $LASTEXITCODE
} catch {
    Write-Error "pytest failed: $_"
    $exitCode = 1
} finally {
    Write-Host "Tearing down test containers..."
    docker-compose -f docker-compose.test.yml down
}

exit $exitCode
