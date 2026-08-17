from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
VENV_SCRIPTS = VENV_DIR / "Scripts"


def _server_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(ROOT)
        if not env.get("PYTHONPATH")
        else str(ROOT) + os.pathsep + env["PYTHONPATH"]
    )
    if VENV_DIR.exists():
        env["VIRTUAL_ENV"] = str(VENV_DIR)
        env["PATH"] = str(VENV_SCRIPTS) + os.pathsep + env.get("PATH", "")
    return _inject_cloudbase_message_env(env)


# 本地起服务只需要消息存储的 CloudBase 连接参数。容器专属的路径/端口/持久化
# 配置（/tmp 路径、WATCH_PORT、cloudbase_nosql 状态后端等）绝不注入，本地行为不变。
_CLOUDBASE_MESSAGE_ENV_KEYS = (
    "WATCH_CLOUDBASE_ENV_ID",
    "WATCH_CLOUDBASE_API_TOKEN",
    "WATCH_CLOUDBASE_MYSQL_INSTANCE",
    "WATCH_CLOUDBASE_MYSQL_SCHEMA",
    "WATCH_CLOUDBASE_MYSQL_OPENID",
    "WATCH_MESSAGE_STORE_BACKEND",
)


def _find_tcb_cli() -> str | None:
    import shutil

    for name in ("tcb", "tcb.cmd"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm" / "tcb.cmd",
        Path("G:/KimiData/daimon-share/daimon/npm-global/tcb.cmd"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _inject_cloudbase_message_env(env: dict[str, str]) -> dict[str, str]:
    """本地缺 CloudBase 消息库配置时，从 CloudRun 服务配置临时拉取注入。

    密钥只进子进程环境变量、不落盘（遵循 README 的密钥约束）；拉取失败
    不阻断启动，仅提示——此时星球消息面板为空是预期表现。
    """
    if env.get("WATCH_CLOUDBASE_API_TOKEN"):
        return env
    cloudbaserc = ROOT / "cloudbaserc.json"
    tcb = _find_tcb_cli()
    if not cloudbaserc.exists() or not tcb:
        return env
    import json
    import subprocess

    try:
        rc = json.loads(cloudbaserc.read_text(encoding="utf-8"))
        env_id = str(rc.get("envId") or "").strip()
        service = str((rc.get("cloudrun") or {}).get("name") or "").strip()
        if not (env_id and service):
            return env
        result = subprocess.run(
            [tcb, "cloudrun", "detail", "--service-name", service, "--env-id", env_id, "--json"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        raw = result.stdout or ""
        detail = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        server_config = (detail.get("data") or {}).get("ServerConfig") or detail.get("ServerConfig") or {}
        env_params = server_config.get("EnvParams") or ""
        params = json.loads(env_params) if isinstance(env_params, str) else dict(env_params)
        injected = [
            key
            for key in _CLOUDBASE_MESSAGE_ENV_KEYS
            if params.get(key) and not env.get(key)
        ]
        for key in injected:
            env[key] = str(params[key])
        if injected:
            print(f"已从 CloudRun 服务配置注入本地消息库连接（仅进程内，不落盘）: {', '.join(injected)}")
    except Exception as exc:  # noqa: BLE001
        print(f"CloudBase 消息库连接注入跳过（{exc!r}）；本地星球消息将不可用。")
    return env


def _python_executable() -> str:
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _terminate_existing_project_servers(host: str, port: int) -> list[int]:
    if os.name != "nt":
        return []
    current_pid = os.getpid()
    root = _powershell_quote(str(ROOT))
    venv = _powershell_quote(str(VENV_DIR))
    app_dir = _powershell_quote(str(ROOT / "app"))
    ps = f"""
$current = {current_pid}
$port = {int(port)}
$root = {root}
$venv = {venv}
$appDir = {app_dir}
$processIds = New-Object System.Collections.Generic.HashSet[int]
$listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object {{ $_.LocalPort -eq $port }}
foreach ($listener in $listeners) {{
    $id = [int]$listener.OwningProcess
    if ($id -eq 0 -or $id -eq $current) {{
        continue
    }}
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
    if ($null -eq $proc) {{
        continue
    }}
    $cmd = [string]$proc.CommandLine
    $isProjectServer = (
        ($cmd -like "*uvicorn app.main:app*" -or $cmd -like "*scripts\\dev_server.py*" -or $cmd -like "*scripts/dev_server.py*") -and
        ($cmd -like "*$venv*" -or $cmd -like "*$root*" -or $cmd -like "*$appDir*")
    )
    if ($isProjectServer) {{
        [void]$processIds.Add($id)
    }}
}}
foreach ($id in $processIds) {{
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    Write-Output $id
}}
"""
    import shutil
    import subprocess

    # 某些环境（如精简 PATH 的终端）里 "powershell" 不在 PATH，会抛
    # FileNotFoundError 导致整个启动脚本崩掉。显式回退到 System32 全路径。
    powershell_exe = (
        shutil.which("powershell")
        or shutil.which("powershell.exe")
        or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    result = subprocess.run(
        [powershell_exe, "-NoProfile", "-Command", ps],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    stopped: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            stopped.append(int(line))
    if stopped:
        time.sleep(1)
    return stopped


def _wait_for_port_available(host: str, port: int, *, timeout_seconds: float = 8.0) -> bool:
    deadline = time.time() + max(0.1, float(timeout_seconds))
    while time.time() < deadline:
        if _port_available(host, port):
            return True
        time.sleep(0.25)
    return _port_available(host, port)


def main() -> None:
    host = os.getenv("WATCH_HOST", "127.0.0.1")
    port = int(os.getenv("WATCH_PORT", "8788"))
    log_level = os.getenv("WATCH_LOG_LEVEL", "info")
    # 默认启用热更新；需要关闭时设 WATCH_DEV_RELOAD=0
    reload_enabled = os.getenv("WATCH_DEV_RELOAD", "1").lower() in {"1", "true", "yes"}
    stopped = _terminate_existing_project_servers(host, port)
    if stopped:
        print(f"stopped existing project server processes: {', '.join(str(pid) for pid in stopped)}")
    if not _wait_for_port_available(host, port):
        print(f"dev server already appears to be listening on {host}:{port}; not starting another instance.")
        raise SystemExit(0)
    command = [
        _python_executable(),
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        log_level,
    ]
    if reload_enabled:
        # 前端已由 web/ 的 vite dev（HMR）/ vite build 接管，后端热更新只盯 Python 源码。
        command.extend(
            [
                "--reload",
                "--reload-dir",
                str(ROOT / "app"),
                "--reload-include",
                "*.py",
            ]
        )
    os.chdir(ROOT)
    # 注意：os.execve 在部分 Windows 终端环境（如 MSYS2/git-bash 子进程）下
    # 会直接段错误，表现为“运行没反应”。改用 subprocess 前台等待：
    # 标准输入输出原样透传，Ctrl+C 通过控制台进程组正常终止服务。
    import subprocess

    try:
        completed = subprocess.run(command, env=_server_env(), check=False)
    except KeyboardInterrupt:
        raise SystemExit(0)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
