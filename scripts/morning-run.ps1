# Утренний прогон оркестратора. Бриф: inbox/brief.md. Результат: runs/<дата>/.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
$date = Get-Date -Format "yyyy-MM-dd"
$run = Join-Path $root "runs\$date"
New-Item -ItemType Directory -Force $run | Out-Null
Set-Location $root
claude -p "/morning-run $date" --output-format json |
    Out-File -Encoding utf8 (Join-Path $run "session.json")
Get-Content (Join-Path $run "summary.md") -Encoding UTF8 -ErrorAction SilentlyContinue
