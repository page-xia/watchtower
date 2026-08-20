#requires -Version 5.1
<#
.SYNOPSIS
    一键部署 watchtower 前后端到阿里云轻量服务器生产环境（omnisource.xin）。

.DESCRIPTION
    流程：回归测试（可跳过）→ 构建 web/dist → 白名单打包 → 上传服务器
    → 服务器端 docker build → 初始化用户隔离表 → 重建容器 → HTTPS 双客户端验收。

    服务器侧约定（见 README「生产部署」与 AGENTS.md）：
      - 部署目录 /root/watchtower（watchtower.env 与 data/ 卷只在服务器上，不进包）
      - 容器名 watchtower，镜像 watchtower:local，映射 127.0.0.1:8788:8788
      - 宿主机 nginx 终结 TLS，外部统一走 https://omnisource.xin

.EXAMPLE
    .\scripts\deploy_aliyun.ps1
    .\scripts\deploy_aliyun.ps1 -SkipTests
    .\scripts\deploy_aliyun.ps1 -SkipTests -SkipBuild    # 只改了后端 Python 时
#>
param(
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$SkipVerify,
    [string]$Server = "root@47.116.20.229",
    [string]$RemoteDir = "/root/watchtower",
    [string]$Image = "watchtower:local",
    [string]$Container = "watchtower",
    [string]$HealthBase = "https://omnisource.xin"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$python = Join-Path $Root ".venv\Scripts\python.exe"

function Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# ---------- 1. 回归测试 ----------
if (-not $SkipTests) {
    Step "运行后端回归测试（可用 -SkipTests 跳过）"
    if (-not (Test-Path $python)) { throw "未找到 .venv，先按 README「快速开始」初始化环境" }
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "测试未通过，已中止部署" }
}

# ---------- 2. 前端构建 ----------
if (-not $SkipBuild) {
    Step "构建前端 web/dist"
    Push-Location (Join-Path $Root "web")
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
    } finally {
        Pop-Location
    }
}
if (-not (Test-Path (Join-Path $Root "web\dist\index.html"))) {
    throw "web/dist 不存在，请先构建（或去掉 -SkipBuild）"
}

# ---------- 3. 白名单打包（密钥 / 自选 / 持仓 / runtime 不进包） ----------
Step "打包部署文件"
$stage = Join-Path $env:TEMP ("watchtower_deploy_" + [DateTime]::Now.ToString("yyyyMMdd_HHmmss"))
New-Item -ItemType Directory -Path $stage | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage "data") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage "web") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage "scripts") | Out-Null
Copy-Item (Join-Path $Root "Dockerfile") $stage
Copy-Item (Join-Path $Root "pyproject.toml") $stage
Copy-Item (Join-Path $Root "app") (Join-Path $stage "app") -Recurse
Copy-Item (Join-Path $Root "web\dist") (Join-Path $stage "web\dist") -Recurse
Copy-Item (Join-Path $Root "data\themes.yaml") (Join-Path $stage "data")
Copy-Item (Join-Path $Root "data\trading_rules.yaml") (Join-Path $stage "data")
Copy-Item (Join-Path $Root "scripts\init_user_store.py") (Join-Path $stage "scripts")
Copy-Item (Join-Path $Root "scripts\probe_user_isolation.py") (Join-Path $stage "scripts")

# 服务器走阿里云 pip 镜像；本地 Dockerfile 不写死，打包时注入（写文件不带 BOM，docker 才能解析）
$dfPath = Join-Path $stage "Dockerfile"
$dfLines = Get-Content $dfPath
$dfOut = New-Object System.Collections.Generic.List[string]
foreach ($line in $dfLines) {
    $dfOut.Add($line)
    if ($line -match '^FROM\s') {
        $dfOut.Add('ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/')
    }
}
[System.IO.File]::WriteAllLines($dfPath, $dfOut)

$tar = "$stage.tar.gz"
tar -czf $tar -C $stage .
if ($LASTEXITCODE -ne 0) { throw "tar 打包失败" }

try {
    # ---------- 4. 上传 ----------
    Step "上传部署包到 $Server"
    scp $tar "${Server}:/tmp/watchtower_deploy.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "scp 上传失败" }

    # ---------- 5. 构建 + MySQL schema + 重建容器（服务只在最后中断） ----------
    Step "服务器端 docker build、初始化用户隔离表并重建容器"
    # Bootstrap is deliberately before docker rm: a missing/non-MySQL user
    # store leaves the currently running release untouched.  The bootstrap
    # command itself refuses any backend other than WATCH_USER_STORE_BACKEND=mysql.
    $remote = "set -e; cd $RemoteDir; rm -rf app web/dist scripts; tar -xzf /tmp/watchtower_deploy.tar.gz; rm /tmp/watchtower_deploy.tar.gz; docker build -t $Image .; docker run --rm --env-file $RemoteDir/watchtower.env -v $RemoteDir/scripts:/scripts:ro $Image python /scripts/init_user_store.py; docker rm -f $Container >/dev/null 2>&1 || true; docker run -d --name $Container --restart unless-stopped -p 127.0.0.1:8788:8788 -v $RemoteDir/data:/data --env-file $RemoteDir/watchtower.env $Image"
    ssh $Server $remote
    if ($LASTEXITCODE -ne 0) { throw "服务器端构建或重建失败" }
} finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $tar -Force -ErrorAction SilentlyContinue
}

# ---------- 6. 冒烟检查 ----------
if (-not $SkipVerify) {
    Step "冒烟检查 $HealthBase（等待容器冷启动）"
    $deadline = [DateTime]::Now.AddSeconds(120)
    $ok = $false
    while ([DateTime]::Now -lt $deadline) {
        $code = & curl.exe -sk -o NUL -w "%{http_code}" "$HealthBase/api/health"
        if ($code -eq "200") { $ok = $true; break }
        Start-Sleep -Seconds 5
    }
    if (-not $ok) { throw "健康检查 120 秒内未恢复，请 ssh $Server 查看 docker logs $Container" }
    foreach ($p in @("/api/health", "/api/dashboard?view=terminal", "/api/indices/minutes", "/api/sectors/rank")) {
        $code = & curl.exe -sk -o NUL -w "%{http_code}" "$HealthBase$p"
        Write-Host ("  {0}  {1}" -f $code, $p)
        if ($code -ne "200") { throw "冒烟检查失败：$p 返回 $code" }
    }
    if (-not (Test-Path $python)) { throw "未找到 .venv，无法运行双客户端隔离验收" }
    Step "运行双客户端用户隔离验收"
    & $python scripts/probe_user_isolation.py --base-url $HealthBase
    if ($LASTEXITCODE -ne 0) { throw "用户隔离验收失败" }
}

Write-Host "`n部署完成：$HealthBase" -ForegroundColor Green
