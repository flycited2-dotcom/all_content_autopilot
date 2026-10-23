param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$autopilotRoot = Split-Path -Parent $PSScriptRoot
Push-Location $autopilotRoot
try {
    if (-not $Apply) {
        python tools/deploy_autopilot.py
        if ($LASTEXITCODE -ne 0) { throw 'Server plan failed' }
        python tools/install_photo_audit.py
        if ($LASTEXITCODE -ne 0) { throw 'Photo agent plan failed' }
        return
    }
    if (Test-Path -LiteralPath 'runs/chat/pending-turn.json') {
        throw 'Listener has a pending turn; wait for completion before switching'
    }
    $queueCode = Get-Content -LiteralPath 'runs/2026-10-23/autopilot/photo_queue_state.py' -Raw
    $queueJson = $queueCode | ssh -o BatchMode=yes -o ConnectTimeout=15 sprintbox-itp 'cd /opt/content-factory && PYTHONPATH=src .venv/bin/python -'
    if ($LASTEXITCODE -ne 0) { throw 'Photo queue preflight failed' }
    $photoQueue = $queueJson | ConvertFrom-Json
    if ($photoQueue.queue.pending -gt 0 -or $photoQueue.queue.processing -gt 0) {
        throw 'Photo queue is busy; no active generation was interrupted'
    }
    $auditStatus = python -m photo_auditor status
    if ($LASTEXITCODE -ne 0) { throw 'Photo auditor preflight failed' }
    $auditCounts = $auditStatus | ConvertFrom-Json
    if (-not $auditCounts.bot_configured -or -not $auditCounts.chatgpt_connected) {
        throw 'Photo auditor connection is not ready'
    }
    if ($auditCounts.jobs.checking -gt 0 -or $auditCounts.jobs.queued -gt 0) {
        throw 'Photo auditor is busy; wait for the current checks'
    }
    $watchdogTask = Get-ScheduledTask -TaskName 'RitualB2B_Watchdog'
    $photoProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match '(remote_agent|agent_watchdog)\.py'
    })
    $watchdogs = @($photoProcesses | Where-Object { $_.CommandLine -match 'agent_watchdog\.py' })
    $agents = @($photoProcesses | Where-Object { $_.CommandLine -match 'remote_agent\.py' })
    $auditors = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -match '(?i)(?:^|\s)-m\s+photo_auditor\s+run(?:\s|$)'
    })
    if ($auditors.Count -gt 1) { throw 'Multiple photo-auditor processes found' }
    if ($watchdogs.Count -gt 1 -or $agents.Count -gt 2) { throw 'Ambiguous photo agent processes' }
    if ($watchdogs.Count -eq 0 -and $agents.Count -gt 0) { throw 'Unmanaged photo agents found' }
    if ($watchdogs.Count -eq 1 -and @($agents | Where-Object { $_.ParentProcessId -ne $watchdogs[0].ProcessId }).Count -gt 0) {
        throw 'Photo agents have an unexpected parent process'
    }
    $wasEnabled = $watchdogTask.State -ne 'Disabled'
    $listenerStopped = $false
    $auditorStopped = $false
    try {
        Disable-ScheduledTask -TaskName 'RitualB2B_Watchdog' | Out-Null
        Stop-ScheduledTask -TaskName 'RitualB2B_Watchdog'
        foreach ($record in $photoProcesses) {
            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.ProcessId)" -ErrorAction SilentlyContinue
            if ($current -and $current.CreationDate -eq $record.CreationDate -and $current.CommandLine -eq $record.CommandLine) {
                Stop-Process -Id $record.ProcessId -ErrorAction Stop
            }
        }
        Stop-ScheduledTask -TaskName 'AgentsMY Photo Auditor'
        $auditorStopped = $true
        foreach ($record in $auditors) {
            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.ProcessId)" -ErrorAction SilentlyContinue
            if ($current -and $current.CreationDate -eq $record.CreationDate -and $current.CommandLine -eq $record.CommandLine) {
                Stop-Process -Id $record.ProcessId -ErrorAction Stop
            }
        }
        Stop-ScheduledTask -TaskName 'AgentsMY Listener'
        $listenerStopped = $true
        python tools/deploy_autopilot.py --apply
        if ($LASTEXITCODE -ne 0) { throw 'Server update failed; rollback was attempted' }
        python tools/install_photo_audit.py --apply
        if ($LASTEXITCODE -ne 0) { throw 'Photo bridge update failed; original source was restored' }
    }
    finally {
      try {
        if ($auditorStopped) { Start-ScheduledTask -TaskName 'AgentsMY Photo Auditor' }
      }
      finally {
       try {
        if ($listenerStopped) {
            for ($restartWait = 0; $restartWait -lt 20; $restartWait++) {
                $listeners = @(Get-CimInstance Win32_Process | Where-Object {
                    $_.Name -eq 'python.exe' -and $_.CommandLine -match '(?i)(?:^|\s)-m\s+listener(?:\s|$)'
                })
                if ($listeners.Count -eq 0) { break }
                Start-Sleep -Seconds 1
            }
            if ($listeners.Count -eq 1) { Stop-Process -Id $listeners[0].ProcessId -ErrorAction Stop }
            if ($listeners.Count -gt 1) { throw 'Multiple listeners found; did not start a duplicate' }
            Start-ScheduledTask -TaskName 'AgentsMY Listener'
        }
       }
       finally {
        if ($wasEnabled) {
            Enable-ScheduledTask -TaskName 'RitualB2B_Watchdog' | Out-Null
            Start-ScheduledTask -TaskName 'RitualB2B_Watchdog'
        }
       }
      }
    }
    Write-Output 'Autopilot server and photo-audit bridge installed; background tasks restarted.'
}
finally {
    Pop-Location
}
