param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$BrowserPath = "",
    [switch]$NoClean
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = (Resolve-Path (Join-Path $projectRoot $Python)).Path
$specPath = Join-Path $projectRoot "packaging\WebE2ETestStudio.spec"

if ($BrowserPath) {
    $env:WEB_E2E_BUNDLED_BROWSERS = (Resolve-Path $BrowserPath).Path
} else {
    Remove-Item Env:WEB_E2E_BUNDLED_BROWSERS -ErrorAction SilentlyContinue
}

$arguments = @("-m", "PyInstaller", "--noconfirm")
if (-not $NoClean) {
    $arguments += "--clean"
}
$arguments += $specPath

Push-Location $projectRoot
try {
    & $pythonPath @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host "Desktop distribution: $projectRoot\dist\WebE2ETestStudio"
