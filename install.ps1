# Iron Repository installer for Windows.
#
# One line, from anywhere (installs into %LOCALAPPDATA%\Iron):
#   irm https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.ps1 | iex
# The same, with options:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.ps1))) -Yes -WithEncryption
# From a folder you cloned (Iron runs straight from that folder; move it? run this again):
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 [-WithEncryption] [-Yes]
#
# -Yes accepts the offer to install a missing Git or Python with winget. Nothing is ever
# installed without that, or without you answering "y".
#
# Environment (mostly for testing):
#   IRON_INSTALL_DIR      where to install (default %LOCALAPPDATA%\Iron)
#   IRON_INSTALL_URL      a git URL to clone, or a .zip to download, instead of GitHub
#   IRON_INSTALL_YES=1    same as -Yes
#   IRON_INSTALL_NO_PATH=1   do not add Iron to your user PATH
#   IRON_INSTALL_DRY_RUN=1   print winget commands instead of running them
param([switch]$WithEncryption, [switch]$Yes)

$IronRepoUrl = "https://github.com/RealDealCPA-VR/Iron-Repo.git"
$IronZipUrl = "https://github.com/RealDealCPA-VR/Iron-Repo/archive/refs/heads/main.zip"

function Test-Truthy([string]$value) { return [bool]$value -and @("0", "false", "no") -notcontains $value.ToLower() }

function Test-Pretend([string]$tool) { return ",$env:IRON_INSTALL_PRETEND_MISSING," -like "*,$tool,*" }

function Test-Python([string[]]$cmd) {
    try {
        $exe, $rest = $cmd
        & $exe @rest -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Find-Python {
    if (Test-Pretend "python") { return $null }
    foreach ($candidate in @(@("py", "-3"), @("python"))) {
        if ((Get-Command $candidate[0] -ErrorAction SilentlyContinue) -and (Test-Python $candidate)) { return , $candidate }
    }
    return $null
}

function Test-Git { return (-not (Test-Pretend "git")) -and [bool](Get-Command git -ErrorAction SilentlyContinue) }

function Confirm-Step([string]$question) {
    if ($Yes -or (Test-Truthy $env:IRON_INSTALL_YES)) { return $true }
    if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        Write-Host "$question [y/N] no (nobody to ask; run again with -Yes to accept)"
        return $false
    }
    $answer = Read-Host "$question [y/N]"
    return @("y", "yes") -contains $answer.Trim().ToLower()
}

function Update-SessionPath {
    # Reads the PATH that a fresh terminal would get, so a tool winget just installed is found.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user;$env:Path"
}

function Install-Prerequisite([string]$name, [string]$wingetId, [string]$link) {
    Write-Host "Iron needs $name, which is not installed."
    $cmd = "winget install --id $wingetId -e"
    $dryRun = (Test-Truthy $env:IRON_INSTALL_DRY_RUN) -or [bool]$env:IRON_INSTALL_PRETEND_MISSING
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "Install it from $link, then run this installer again."
        return $false
    }
    if (-not (Confirm-Step "Install it now with:   $cmd   ?")) {
        Write-Host "Not installing. Run it yourself:   $cmd   (or download from $link), then run this installer again."
        return $false
    }
    if ($dryRun) { Write-Host "[dry run] would run: $cmd"; return $false }
    & winget install --id $wingetId -e | Out-Host
    Update-SessionPath
    return $true
}

function Get-Prerequisites {
    $py = Find-Python
    if (-not $py) {
        $null = Install-Prerequisite "Python 3.8 or newer" "Python.Python.3.12" "https://www.python.org/downloads/  (tick 'Add python.exe to PATH')"
        $py = Find-Python
        if (-not $py) { throw "Iron needs Python 3.8 or newer." }
    }
    if (-not (Test-Git)) {
        $null = Install-Prerequisite "Git" "Git.Git" "https://git-scm.com/download/win"
        if (-not (Test-Git)) { throw "Iron needs Git." }
    }
    return , $py
}

function Save-Url([string]$url, [string]$file) {
    if ($url -like "file:*") { Copy-Item -LiteralPath ([Uri]$url).LocalPath $file; return }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
}

function Expand-Iron([string]$url, [string]$dest) {
    # Unpacks a downloaded .zip next to $dest (same drive, so the final move is a rename).
    $work = "$dest.download-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        Write-Host "Downloading $url"
        Save-Url $url (Join-Path $work "iron.zip")
        Expand-Archive -LiteralPath (Join-Path $work "iron.zip") -DestinationPath (Join-Path $work "x")
        $top = @(Get-Item (Join-Path $work "x")) + @(Get-ChildItem -Directory (Join-Path $work "x")) |
            Where-Object { Test-Path (Join-Path $_.FullName "iron\cli.py") } | Select-Object -First 1
        if (-not $top) { throw "$url does not contain Iron." }
        Move-Item -LiteralPath $top.FullName $dest
        Set-Content -LiteralPath (Join-Path $dest ".iron-source") -Value $url -Encoding ASCII
    } finally {
        Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
    }
}

function Get-Iron([string]$dest) {
    $url = $env:IRON_INSTALL_URL
    if (-not $url) { $url = if (Test-Git) { $IronRepoUrl } else { $IronZipUrl } }
    if ($url -match "\.zip$") { Expand-Iron $url $dest; return }
    Write-Host "Cloning $url"
    & git clone --depth 1 --quiet $url $dest | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "git clone $url failed." }
}

function Install-Remote([string[]]$py) {
    $dir = $env:IRON_INSTALL_DIR
    if (-not $dir) { $dir = Join-Path $env:LOCALAPPDATA "Iron" }
    $dir = [IO.Path]::GetFullPath($dir)
    if ((Test-Path (Join-Path $dir ".git")) -or (Test-Path (Join-Path $dir "iron\cli.py"))) {
        Write-Host "Iron is already installed in $dir; updating it."
        $exe, $rest = $py
        & $exe @rest (Join-Path $dir "bin\iron_launch.py") cli update | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "iron update failed." }
        return $dir
    }
    if ((Test-Path $dir) -and (Get-ChildItem -Force -LiteralPath $dir | Select-Object -First 1)) {
        throw "$dir already exists and is not an Iron install. Set IRON_INSTALL_DIR to another folder."
    }
    $parent = Split-Path -Parent $dir
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
    if (Test-Path $dir) { Remove-Item -LiteralPath $dir }
    Get-Iron $dir
    Write-Host "Installed Iron into $dir"
    return $dir
}

function Install-Iron {
    $ErrorActionPreference = "Stop"
    $py = Get-Prerequisites
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "iron\cli.py"))) { $root = $PSScriptRoot }
    else { $root = Install-Remote $py }
    $bin = Join-Path $root "bin"

    if (Test-Truthy $env:IRON_INSTALL_NO_PATH) {
        Write-Host "Not changing PATH (IRON_INSTALL_NO_PATH). Iron is in $bin"
    } else {
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if (-not $userPath) { $userPath = "" }
        if (($userPath -split ";") -notcontains $bin) {
            [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $bin).TrimStart(";"), "User")
            Write-Host "Added $bin to your PATH. Terminals opened from now on will find 'iron'."
        }
    }
    $env:Path = "$bin;$env:Path"

    if ($WithEncryption) {
        $exe, $rest = $py
        & $exe @rest -m pip install --user --quiet cryptography
    }

    & (Join-Path $bin "iron.cmd") doctor
    Write-Host ""
    Write-Host "Next: open a NEW terminal, go to a project folder, and run:   iron setup"
}

$code = 0
try { Install-Iron } catch { Write-Host "Iron was not installed: $($_.Exception.Message)" -ForegroundColor Red; $code = 1 }
if ($PSCommandPath) { exit $code }
