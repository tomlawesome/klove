$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Create .venv and install requirements-dev.lock before running tests."
}

& $python -m ruff format --check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m pytest --cov=klove --cov-branch --cov-report=term-missing --cov-report=json -q
exit $LASTEXITCODE
