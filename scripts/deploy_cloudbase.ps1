#Requires -Version 5.1
<#
.SYNOPSIS
Build, package, deploy and smoke-test the watchtower CloudBase CloudRun service.

.EXAMPLE
.\scripts\deploy_cloudbase.ps1

.EXAMPLE
.\scripts\deploy_cloudbase.ps1 -DryRun -SkipTests

.EXAMPLE
.\scripts\deploy_cloudbase.ps1 -SkipTests -SkipBuild
#>

[CmdletBinding()]
param(
    [string]$EnvId = "server-d2g7x597t019f5cb0",
    [string]$ServerName = "watchtower",
    [string]$ProductionUrl = "https://watch.omnisource.xin",
    [int]$Port = 8788,
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$SkipVerify,
    [switch]$SkipLogin,
    [switch]$DryRun,
    [int]$SmokeRetries = 8,
    [int]$SmokeRetrySeconds = 20,
    [string]$Python = "",
    [string]$Remark = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Resolve-CommandPath {
    param(
        [Parameter(Mandatory = $true)][string[]]$Name,
        [Parameter(Mandatory = $true)][string]$InstallHint
    )

    foreach ($candidate in $Name) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) {
            $path = $command.Source
            if (-not $path -and $command.PSObject.Properties["Path"]) {
                $path = $command.Path
            }
            if ($path) {
                return $path
            }
            return $candidate
        }
    }

    throw "Missing command '$($Name -join "/")'. $InstallHint"
}

function Resolve-PythonPath {
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python)) {
            throw "Python path does not exist: $Python"
        }
        return (Resolve-Path -LiteralPath $Python).Path
    }

    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }

    return Resolve-CommandPath -Name @("python.exe", "python") -InstallHint "Create .venv first, or pass -Python <path>."
}

function Invoke-CommandChecked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = $Root
    )

    $display = "$FilePath $($Arguments -join ' ')"
    Write-Host $display
    Push-Location -LiteralPath $WorkingDirectory
    try {
        # Newer @cloudbase/cli still shows an interactive canary prompt under
        # --force; piping an empty line selects the default "No" (full release).
        '' | & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code $LASTEXITCODE`: $display"
        }
    }
    finally {
        Pop-Location
    }
}

function Get-PropertyValue {
    param(
        [object]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $Object) {
        return $null
    }
    if ($Object -is [System.Collections.IDictionary] -and $Object.Contains($Name)) {
        return $Object[$Name]
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) {
        return $property.Value
    }
    return $null
}

function Convert-ToArray {
    param([object]$Value)
    # PowerShell 管道会展开单元素数组；用 , 前缀阻止展开，
    # 保证调用方拿到的始终是数组（夜间/开盘前指数分钟线只有 1 个点时
    # 单元素 JSON 数组会被 Invoke-RestMethod 标量化，否则 $points.Count
    # 在 StrictMode 下抛“找不到属性 Count”）。
    if ($null -eq $Value) {
        return , @()
    }
    if ($Value -is [array]) {
        return , $Value
    }
    return , @($Value)
}

function Get-ChinaNow {
    try {
        $tz = [System.TimeZoneInfo]::FindSystemTimeZoneById("China Standard Time")
        return [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz)
    }
    catch {
        return Get-Date
    }
}

function Convert-TimeTextToMinutes {
    param([object]$Value)
    $text = ""
    if ($null -ne $Value) {
        $text = [string]$Value
    }
    if ($text -notmatch "^(\d{2}):(\d{2})") {
        return $null
    }
    return ([int]$Matches[1] * 60) + [int]$Matches[2]
}

function Get-ExpectedMarketTailMinutes {
    $now = Get-ChinaNow
    $minutes = ($now.Hour * 60) + $now.Minute
    $open = 9 * 60 + 30
    $middayClose = 11 * 60 + 30
    $afternoonOpen = 13 * 60
    $close = 15 * 60

    if ($minutes -lt ($open + 15)) {
        return $null
    }
    if ($minutes -le $middayClose) {
        return [Math]::Max($open, $minutes - 10)
    }
    if ($minutes -lt $afternoonOpen) {
        return $middayClose - 5
    }
    if ($minutes -le ($close + 10)) {
        return [Math]::Max($afternoonOpen, [Math]::Min($close, $minutes - 10))
    }
    return $close - 5
}

function Join-Url {
    param(
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)][string]$Path
    )
    return "$($Base.TrimEnd('/'))/$($Path.TrimStart('/'))"
}

function Invoke-JsonGet {
    param([Parameter(Mandatory = $true)][string]$Path)
    $url = Join-Url -Base $ProductionUrl -Path $Path
    Write-Host "GET $url"
    return Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 30 -Headers @{ "Cache-Control" = "no-cache" }
}

function Assert-CleanPackage {
    param([Parameter(Mandatory = $true)][string]$PackagePath)

    $blockedPatterns = @("watchlist", "positions", "runtime", "ts2db_config", ".env")
    $blockedFiles = Get-ChildItem -LiteralPath $PackagePath -Recurse -File | Where-Object {
        $path = $_.FullName.ToLowerInvariant()
        foreach ($pattern in $blockedPatterns) {
            if ($path.Contains($pattern)) {
                return $true
            }
        }
        return $false
    }

    if ($blockedFiles) {
        $blockedFiles | ForEach-Object { Write-Error "Private file included in package: $($_.FullName)" }
        throw "CloudBase package contains private files."
    }
}

function Show-SmokeSummary {
    param(
        [object]$Health,
        [object]$Dashboard,
        [object]$IndexMinutes,
        [object[]]$Sectors
    )

    $sourceStatus = Get-PropertyValue -Object $Dashboard -Name "source_status"
    $boardSource = Get-PropertyValue -Object $Dashboard -Name "board_source"
    if (-not $boardSource) {
        $boardSource = Get-PropertyValue -Object $sourceStatus -Name "board_source"
    }
    $officialReady = Get-PropertyValue -Object $sourceStatus -Name "official_board_member_ready"
    $memberCount = Get-PropertyValue -Object $sourceStatus -Name "board_member_cached_count"
    $activeSource = Get-PropertyValue -Object $sourceStatus -Name "active_source"
    $tradeDate = Get-PropertyValue -Object $sourceStatus -Name "trade_date"
    $expectedTailMinutes = Get-ExpectedMarketTailMinutes

    Write-Step "Smoke summary"
    Write-Host ("health: {0}" -f ($(if ($Health) { "ok" } else { "empty" })))
    Write-Host ("active_source: {0}" -f $activeSource)
    Write-Host ("trade_date: {0}" -f $tradeDate)
    Write-Host ("board_source: {0}" -f $boardSource)
    Write-Host ("official_board_member_ready: {0}" -f $officialReady)
    Write-Host ("board_member_cached_count: {0}" -f $memberCount)
    Write-Host ("sector_rank_count: {0}" -f (Convert-ToArray $Sectors).Count)

    $indexSeries = Convert-ToArray (Get-PropertyValue -Object $IndexMinutes -Name "indices")
    $indexSeriesWithPoints = 0
    $lateIndexStarts = @()
    $staleIndexTails = @()
    foreach ($series in $indexSeries) {
        $code = Get-PropertyValue -Object $series -Name "code"
        $name = Get-PropertyValue -Object $series -Name "name"
        $points = Convert-ToArray (Get-PropertyValue -Object $series -Name "points")
        if ($points.Count -eq 0) {
            Write-Host ("index {0} {1}: 0 points" -f $code, $name)
            continue
        }
        # 夜间/开盘前指数分钟线只有 15:00 收盘快照一个点，属于正常状态，
        # 不参与“开盘点位过晚/尾部过期”检查，否则夜间部署永远无法通过冒烟。
        if ($points.Count -eq 1 -and ([string](Get-PropertyValue -Object $points[0] -Name "time")) -eq "15:00") {
            Write-Host ("index {0} {1}: after-hours close snapshot, freshness checks skipped" -f $code, $name)
            $indexSeriesWithPoints += 1
            continue
        }
        $firstTime = Get-PropertyValue -Object $points[0] -Name "time"
        $lastTime = Get-PropertyValue -Object $points[$points.Count - 1] -Name "time"
        Write-Host ("index {0} {1}: {2} points, {3} -> {4}" -f $code, $name, $points.Count, $firstTime, $lastTime)
        $indexSeriesWithPoints += 1
        if ($firstTime -and ([string]$firstTime).CompareTo("09:35") -gt 0) {
            $lateIndexStarts += ("{0} {1} starts at {2}" -f $code, $name, $firstTime)
        }
        $lastMinutes = Convert-TimeTextToMinutes $lastTime
        if ($null -ne $expectedTailMinutes -and $null -ne $lastMinutes -and $lastMinutes -lt $expectedTailMinutes) {
            $staleIndexTails += ("{0} {1} ends at {2}" -f $code, $name, $lastTime)
        }
    }

    $sectorFlowSeries = Convert-ToArray (Get-PropertyValue -Object $Dashboard -Name "sector_flow")
    $sectorFlowWithPoints = 0
    $lateSectorFlowStarts = @()
    $staleSectorFlowTails = @()
    Write-Host ("sector_flow_count: {0}" -f $sectorFlowSeries.Count)
    foreach ($series in ($sectorFlowSeries | Select-Object -First 3)) {
        $name = Get-PropertyValue -Object $series -Name "name"
        $points = Convert-ToArray (Get-PropertyValue -Object $series -Name "points")
        if ($points.Count -eq 0) {
            Write-Host ("sector_flow {0}: 0 points" -f $name)
            continue
        }
        $firstTime = Get-PropertyValue -Object $points[0] -Name "time"
        $lastTime = Get-PropertyValue -Object $points[$points.Count - 1] -Name "time"
        Write-Host ("sector_flow {0}: {1} points, {2} -> {3}" -f $name, $points.Count, $firstTime, $lastTime)
        $sectorFlowWithPoints += 1
        if ($firstTime -and ([string]$firstTime).CompareTo("09:35") -gt 0) {
            $lateSectorFlowStarts += ("{0} starts at {1}" -f $name, $firstTime)
        }
        $lastMinutes = Convert-TimeTextToMinutes $lastTime
        if ($null -ne $expectedTailMinutes -and $null -ne $lastMinutes -and $lastMinutes -lt $expectedTailMinutes) {
            $staleSectorFlowTails += ("{0} ends at {1}" -f $name, $lastTime)
        }
    }

    if ($activeSource -eq "local_trajectory_bootstrap") {
        throw "Smoke check failed: service is still serving bootstrap data; wait for live refresh."
    }
    if (-not $boardSource) {
        throw "Smoke check failed: board_source is empty."
    }
    if ($indexSeries.Count -eq 0) {
        throw "Smoke check failed: /api/indices/minutes returned no indices."
    }
    if ($indexSeriesWithPoints -eq 0) {
        throw "Smoke check failed: /api/indices/minutes returned indices with no minute points."
    }
    if ($lateIndexStarts.Count -gt 0) {
        throw "Smoke check failed: index minute data starts too late. $($lateIndexStarts -join '; ')"
    }
    if ($staleIndexTails.Count -gt 0) {
        throw "Smoke check failed: index minute data is stale. $($staleIndexTails -join '; ')"
    }
    if ((Convert-ToArray $Sectors).Count -eq 0) {
        throw "Smoke check failed: /api/sectors/rank returned no sectors."
    }
    if ($sectorFlowSeries.Count -gt 0 -and $sectorFlowWithPoints -eq 0) {
        throw "Smoke check failed: dashboard sector_flow has no minute points."
    }
    if ($lateSectorFlowStarts.Count -gt 0) {
        throw "Smoke check failed: sector flow data starts too late. $($lateSectorFlowStarts -join '; ')"
    }
    if ($staleSectorFlowTails.Count -gt 0) {
        throw "Smoke check failed: sector flow data is stale. $($staleSectorFlowTails -join '; ')"
    }
}

function Invoke-ProductionSmokeChecks {
    $attempts = [Math]::Max(1, $SmokeRetries)
    $sleepSeconds = [Math]::Max(1, $SmokeRetrySeconds)
    $lastError = $null

    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        Write-Step "Production smoke checks ($attempt/$attempts)"
        try {
            $cacheBuster = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
            $health = Invoke-JsonGet -Path "/api/health?_$cacheBuster"
            $dashboard = Invoke-JsonGet -Path "/api/dashboard?view=terminal&board_level=3&page_size=20&watchlist_codes=&_$cacheBuster"
            $indexMinutes = Invoke-JsonGet -Path "/api/indices/minutes?_$cacheBuster"
            $sectors = Convert-ToArray (Invoke-JsonGet -Path "/api/sectors/rank?board_level=3&watchlist_codes=&_$cacheBuster")
            Show-SmokeSummary -Health $health -Dashboard $dashboard -IndexMinutes $indexMinutes -Sectors $sectors
            return
        }
        catch {
            $lastError = $_
            Write-Warning $_.Exception.Message
            if ($attempt -lt $attempts) {
                Write-Host "Waiting $sleepSeconds seconds before retry..."
                Start-Sleep -Seconds $sleepSeconds
            }
        }
    }

    throw "Production smoke checks failed after $attempts attempts. Last error: $($lastError.Exception.Message)"
}

Write-Step "Target"
Write-Host "envId: $EnvId"
Write-Host "service: $ServerName"
Write-Host "port: $Port"
Write-Host "url: $ProductionUrl"
Write-Host "dryRun: $DryRun"
Write-Host "Note: secrets stay in CloudRun environment variables. This script does not upload ts2db_config.yaml or local watchlist/positions/runtime data."

if (-not $DryRun) {
    Write-Step "Check CloudBase CLI"
    $tcbPath = Resolve-CommandPath -Name @("tcb.cmd", "tcb") -InstallHint "Install it with: npm i -g @cloudbase/cli"
    Invoke-CommandChecked -FilePath $tcbPath -Arguments @("--version") -WorkingDirectory $Root
    Invoke-CommandChecked -FilePath $tcbPath -Arguments @("cloudrun", "deploy", "--help") -WorkingDirectory $Root
}

if (-not $SkipTests) {
    Write-Step "Run backend regression tests"
    $pythonPath = Resolve-PythonPath
    $testFiles = @(
        "tests\test_message_store.py",
        "tests\test_cloud_persistence_service.py",
        "tests\test_terminal_board.py",
        "tests\test_intraday_storage.py"
    )
    $pytestArgs = @("-m", "pytest") + $testFiles + @("-q")
    try {
        Invoke-CommandChecked -FilePath $pythonPath -Arguments $pytestArgs -WorkingDirectory $Root
    }
    catch {
        throw "Backend regression tests did not finish; deployment was not started. Use -SkipTests only if tests have already passed. Details: $($_.Exception.Message)"
    }
}
else {
    Write-Step "Skip tests"
}

if (-not $SkipBuild) {
    Write-Step "Build frontend"
    $npmPath = Resolve-CommandPath -Name @("npm.cmd", "npm") -InstallHint "Install Node.js LTS and run npm install in web/."
    Invoke-CommandChecked -FilePath $npmPath -Arguments @("run", "build") -WorkingDirectory (Join-Path $Root "web")
}
else {
    Write-Step "Skip frontend build"
    $distPath = Join-Path $Root "web\dist"
    if (-not (Test-Path -LiteralPath $distPath)) {
        throw "web/dist does not exist. Remove -SkipBuild or build the frontend first."
    }
}

Write-Step "Create clean CloudBase package"
$packageScript = Join-Path $Root "scripts\package_cloudbase.ps1"
$packageOutput = & $packageScript -EnvId $EnvId -ServerName $ServerName
if (-not $?) {
    throw "Packaging failed."
}
$packagePath = ($packageOutput | Select-Object -Last 1).ToString().Trim()
if (-not (Test-Path -LiteralPath $packagePath)) {
    throw "Package path does not exist: $packagePath"
}
Assert-CleanPackage -PackagePath $packagePath
Write-Host "package: $packagePath"

$deployArgs = @("cloudrun", "deploy", "--service-name", $ServerName, "--env-id", $EnvId, "--port", [string]$Port, "--force")
if ($Remark) {
    $deployArgs += @("--remark", $Remark)
}

if ($DryRun) {
    Write-Step "Dry run complete"
    Write-Host "Would run from package directory:"
    Write-Host ("tcb {0}" -f ($deployArgs -join " "))
    Write-Host "No CloudBase resources were changed."
    exit 0
}

if (-not $SkipLogin) {
    Write-Step "CloudBase login"
    Invoke-CommandChecked -FilePath $tcbPath -Arguments @("login") -WorkingDirectory $Root
}
else {
    Write-Step "Skip CloudBase login"
}

Write-Step "Select CloudBase environment"
Invoke-CommandChecked -FilePath $tcbPath -Arguments @("env", "use", $EnvId) -WorkingDirectory $Root

Write-Step "Deploy CloudRun service"
Invoke-CommandChecked -FilePath $tcbPath -Arguments $deployArgs -WorkingDirectory $packagePath

Write-Step "Verify CloudRun service list"
Invoke-CommandChecked -FilePath $tcbPath -Arguments @("cloudrun", "list", "--service-name", $ServerName, "--env-id", $EnvId) -WorkingDirectory $Root

if ($SkipVerify) {
    Write-Step "Skip production smoke checks"
    exit 0
}

Invoke-ProductionSmokeChecks

Write-Step "Done"
