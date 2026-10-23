$ErrorActionPreference = "Stop"
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    # Token is entered into a masked local dialog, never a command-line argument.
    python -m photo_auditor configure
    if ($LASTEXITCODE -ne 0) { throw "Подключение фотоаудитора не завершено" }
} finally {
    Pop-Location
}
