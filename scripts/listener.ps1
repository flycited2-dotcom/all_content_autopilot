# Слушатель оркестратора на ПК. Запускается Планировщиком при входе (scripts/install-listener-task.ps1).
# ORCH_API_TOKEN — пользовательская переменная окружения Windows (не хранится в git).
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not $env:ORCH_API_TOKEN) { $env:ORCH_API_TOKEN = [Environment]::GetEnvironmentVariable("ORCH_API_TOKEN", "User") }
if (-not $env:ORCH_TUNNEL_KEY) { $env:ORCH_TUNNEL_KEY = Join-Path $env:USERPROFILE ".ssh\id_agents_tunnel" }
New-Item -ItemType Directory -Force (Join-Path $root "runs\chat") | Out-Null
python -m listener *>> (Join-Path $root "runs\chat\listener.log")
