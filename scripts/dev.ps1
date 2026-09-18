# Common development tasks.
# Usage: .\scripts\dev.ps1 <run|test|lint|format|typecheck|check|migrate|upgrade>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('run', 'test', 'lint', 'format', 'typecheck', 'check', 'migrate', 'upgrade')]
    [string]$Task,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "Virtualenv not found at $python. Create it with: py -V:3.12 -m venv .venv"
}

Push-Location $root
try {
    switch ($Task) {
        # --timeout-graceful-shutdown: on a reload uvicorn waits for open
        # connections to finish, and the SSE streams (notifications, search
        # progress) never do. Without a limit one open browser tab leaves the
        # server hung mid-reload, accepting nothing.
        'run' { & $python -m uvicorn app.main:app --reload --timeout-graceful-shutdown 3 }
        'test' { & $python -m pytest @Rest }
        'lint' { & $python -m ruff check . --fix }
        'format' { & $python -m ruff format . }
        'typecheck' { & $python -m mypy app }
        'check' {
            & $python -m ruff check .
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            & $python -m mypy app
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            & $python -m pytest
        }
        'migrate' {
            $message = if ($Rest) { $Rest -join ' ' } else { 'auto' }
            & $python -m alembic revision --autogenerate -m $message
        }
        'upgrade' { & $python -m alembic upgrade head }
    }
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
