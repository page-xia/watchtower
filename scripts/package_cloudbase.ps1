param(
    [string]$EnvId = "server-d2g7x597t019f5cb0",
    [string]$ServerName = "watchtower"
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$stamp = Get-Date -Format "yyyyMMddHHmmss"
$packagePath = Join-Path $env:TEMP "codex-cloudbase-watchtower-$stamp"

New-Item -ItemType Directory -Force -Path $packagePath | Out-Null

Copy-Item -LiteralPath (Join-Path $root "app") -Destination (Join-Path $packagePath "app") -Recurse
Get-ChildItem -LiteralPath (Join-Path $packagePath "app") -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force

New-Item -ItemType Directory -Force -Path (Join-Path $packagePath "web") | Out-Null
Copy-Item -LiteralPath (Join-Path $root "web\dist") -Destination (Join-Path $packagePath "web\dist") -Recurse

New-Item -ItemType Directory -Force -Path (Join-Path $packagePath "data") | Out-Null
Copy-Item -LiteralPath (Join-Path $root "data\themes.yaml") -Destination (Join-Path $packagePath "data\themes.yaml")
Copy-Item -LiteralPath (Join-Path $root "data\trading_rules.yaml") -Destination (Join-Path $packagePath "data\trading_rules.yaml")

Copy-Item -LiteralPath (Join-Path $root "pyproject.toml") -Destination (Join-Path $packagePath "pyproject.toml")
Copy-Item -LiteralPath (Join-Path $root "Dockerfile") -Destination (Join-Path $packagePath "Dockerfile")
Copy-Item -LiteralPath (Join-Path $root ".dockerignore") -Destination (Join-Path $packagePath ".dockerignore")

$cloudbaseConfig = @{
    envId = $EnvId
    cloudrun = @{
        name = $ServerName
    }
} | ConvertTo-Json -Depth 3
# PS 5.1 "Set-Content -Encoding utf8" emits a BOM; newer @cloudbase/cli
# rejects cloudbaserc.json with a BOM, so write UTF-8 without BOM explicitly.
[System.IO.File]::WriteAllText(
    (Join-Path $packagePath "cloudbaserc.json"),
    $cloudbaseConfig,
    (New-Object System.Text.UTF8Encoding($false))
)

$blockedPatterns = @("watchlist", "positions", "runtime", "ts2db_config")
$blockedFiles = Get-ChildItem -LiteralPath $packagePath -Recurse -File |
    Where-Object {
        $path = $_.FullName.ToLowerInvariant()
        foreach ($pattern in $blockedPatterns) {
            if ($path.Contains($pattern)) {
                return $true
            }
        }
        return $false
    }

if ($blockedFiles) {
    $blockedFiles | ForEach-Object { Write-Error "Private file included in CloudBase package: $($_.FullName)" }
    exit 1
}

Write-Output $packagePath
