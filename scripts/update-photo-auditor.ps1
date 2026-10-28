# Переводит ПК на фотоаудитор-помощника (ветка codex/photo-audit-agent-ffrkpl) и перезапускает
# службу аудитора и фотоагентов, не прерывая идущую генерацию.
#   План без изменений:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\update-photo-auditor.ps1
#   Выполнить:           ... -File scripts\update-photo-auditor.ps1 -Apply
# Перед переключением локальные изменения сохраняются: git stash (tracked) и копия
# новых файлов в runs\backups\before-advisory-<время>\. Папка runs\ и секреты не трогаются.
# Очередь фото на сервере только читается (mode=ro), чтобы перезапустить агентов в простое.
param([switch]$Apply, [int]$WaitMinutes = 45)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$branch = 'codex/photo-audit-agent-ffrkpl'
$target = "origin/$branch"
$root = Split-Path -Parent $PSScriptRoot
$gitExe = (Get-Command git -CommandType Application | Select-Object -First 1).Source
function git { & $gitExe -c "safe.directory=$root" @args }
Push-Location $root
try {
    git fetch origin $branch
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось получить ветку с GitHub' }
    $changed = @(git -c core.quotepath=off diff --name-only $target)
    $untracked = @(git -c core.quotepath=off ls-files --others --exclude-standard)
    Write-Output "Текущая версия: $(git rev-parse --short HEAD) ($(git rev-parse --abbrev-ref HEAD)) -> $(git rev-parse --short $target)"
    Write-Output "Локальных файлов, отличающихся от новой версии: $($changed.Count); новых неотслеживаемых: $($untracked.Count)"
    if (-not $Apply) {
        Write-Output 'План: сохранить локальные изменения, переключить код, проверить режим advisory,'
        Write-Output 'перезапустить службу аудитора, дождаться простоя очереди и перезапустить фотоагентов.'
        Write-Output 'Для выполнения добавьте -Apply.'
        return
    }

    $stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
    $backup = Join-Path $root "runs/backups/before-advisory-$stamp"
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    git rev-parse HEAD | Set-Content -Encoding ascii -LiteralPath (Join-Path $backup 'HEAD.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось сохранить исходный HEAD; переключение отменено' }
    $stashCommit = git stash create
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать резерв локальных tracked-правок; переключение отменено' }
    if ($stashCommit) {
        git stash store -m "before-advisory-$stamp" $stashCommit
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось сохранить локальные изменения' }
        Set-Content -Encoding ascii -LiteralPath (Join-Path $backup 'stash.txt') -Value $stashCommit
    }
    foreach ($file in $untracked) {
        $copy = Join-Path $backup ("untracked/" + $file)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $copy) | Out-Null
        Copy-Item -LiteralPath (Join-Path $root $file) -Destination $copy
    }
    Write-Output "Резервная копия: $backup"

    git checkout -f -B $branch $target
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось переключить код; резервная копия сохранена' }
    $mode = python -c "import photo_auditor.integration, photo_auditor.service; from photo_auditor.mode import settings; print(settings('.')['mode'])"
    if ($LASTEXITCODE -ne 0 -or $mode -ne 'advisory') { throw "Новый код не прошёл проверку импорта (режим: $mode)" }
    Write-Output 'Код обновлён, режим аудитора: advisory (помощник).'

    # Служба аудитора: прерванные проверки возвращаются в очередь, фото не генерируются заново.
    $auditors = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '-m photo_auditor run$' })
    if ($auditors.Count -eq 0) {
        Start-ScheduledTask -TaskName 'AgentsMY Photo Auditor'
    } else {
        & (Join-Path $root 'tools/reload_photo_auditor.ps1')
    }
    Write-Output 'Служба фотоаудитора перезапущена.'

    # Фотоагенты держат код аудитора в памяти: без перезапуска новый режим к ним не применится.
    $queueQuery = @'
import json, sqlite3
db = sqlite3.connect('file:/root/ritualb2b/queue.db?mode=ro', uri=True)
print(json.dumps(dict(db.execute("SELECT status, count(*) FROM jobs WHERE status IN ('pending','processing') GROUP BY status").fetchall())))
'@
    function Get-ProcessingJobs {
        $answer = $queueQuery | ssh -o BatchMode=yes -o ConnectTimeout=15 sprintbox-itp 'python3 -'
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось прочитать очередь фото' }
        $state = $answer | ConvertFrom-Json
        if ($state.processing) { return [int]$state.processing }
        return 0
    }
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    while ((Get-ProcessingJobs) -gt 0) {
        if ((Get-Date) -gt $deadline) {
            Write-Output "Фотоагенты заняты дольше $WaitMinutes мин; генерацию не прерываю."
            Write-Output 'Код уже обновлён. Запустите скрипт с -Apply ещё раз, когда очередь освободится.'
            return
        }
        Write-Output 'Идёт генерация фото; жду завершения (проверка каждые 30 с)...'
        Start-Sleep -Seconds 30
    }
    $watchdogTask = Get-ScheduledTask -TaskName 'RitualB2B_Watchdog'
    $wasEnabled = $watchdogTask.State -ne 'Disabled'
    if (-not $wasEnabled) {
        Write-Output 'Задача RitualB2B_Watchdog выключена: фотоагентов не останавливаю, их некому будет запустить.'
        Write-Output 'Новый режим применится к ним при следующем запуске.'
        return
    }
    try {
        Disable-ScheduledTask -TaskName 'RitualB2B_Watchdog' | Out-Null
        Stop-ScheduledTask -TaskName 'RitualB2B_Watchdog'
        if ((Get-ProcessingJobs) -gt 0) { throw 'Фотоагент только что взял задание; повторите позже' }
        $photoProcesses = @(Get-CimInstance Win32_Process | Where-Object {
            $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match '(remote_agent|agent_watchdog)\.py'
        })
        foreach ($record in $photoProcesses) {
            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.ProcessId)" -ErrorAction SilentlyContinue
            if ($current -and $current.CreationDate -eq $record.CreationDate -and $current.CommandLine -eq $record.CommandLine) {
                Stop-Process -Id $record.ProcessId -ErrorAction Stop
            }
        }
        Write-Output "Остановлено процессов фотоагента: $($photoProcesses.Count)"
    }
    finally {
        Enable-ScheduledTask -TaskName 'RitualB2B_Watchdog' | Out-Null
        Start-ScheduledTask -TaskName 'RitualB2B_Watchdog'
    }
    for ($tick = 0; $tick -lt 60; $tick++) {
        $agents = @(Get-CimInstance Win32_Process | Where-Object {
            $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'remote_agent\.py'
        })
        if ($agents.Count -gt 0) { break }
        Start-Sleep -Seconds 2
    }
    if ($agents.Count -eq 0) { throw 'RitualB2B_Watchdog не запустил фотоагентов за 2 минуты; проверьте Планировщик' }
    Write-Output "Фотоагентов запущено с новым кодом: $($agents.Count)"
    Write-Output 'Готово: аудитор работает помощником и больше не останавливает контент-завод.'
}
finally {
    Pop-Location
}
