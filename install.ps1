# Iron Repository installer for Windows.
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1                  # basic
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -WithEncryption  # + optional encryption package
#
# Nothing is downloaded for the basic install: it only puts this folder's bin\
# on your PATH, so Iron runs straight from this folder. Move the folder? Run it again.
param([switch]$WithEncryption)
$ErrorActionPreference = "Stop"
$bin = Join-Path $PSScriptRoot "bin"

function Test-Python([string[]]$cmd) {
    try {
        $exe, $rest = $cmd
        & $exe @rest -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

$py = $null
foreach ($candidate in @(@("py", "-3"), @("python"))) {
    if ((Get-Command $candidate[0] -ErrorAction SilentlyContinue) -and (Test-Python $candidate)) { $py = $candidate; break }
}
if (-not $py) {
    Write-Host "Iron needs Python 3.8 or newer: https://www.python.org/downloads/  (tick 'Add python.exe to PATH')"
    exit 1
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Iron needs Git: https://git-scm.com/download/win"
    exit 1
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (($userPath -split ";") -notcontains $bin) {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $bin).TrimStart(";"), "User")
    Write-Host "Added $bin to your PATH. Terminals opened from now on will find 'iron'."
}
$env:Path = "$bin;$env:Path"

if ($WithEncryption) {
    $exe, $rest = $py
    & $exe @rest -m pip install --user --quiet cryptography
}

& (Join-Path $bin "iron.cmd") doctor
Write-Host ""
Write-Host "Next: open a NEW terminal, go to a project folder, and run:   iron setup"
