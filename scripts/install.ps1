# ============================================================================
# LeanFlow WSL Installer Bootstrap
# ============================================================================
# Windows convenience wrapper that installs LeanFlow inside WSL2 using the
# shared installer flow.
#
# Usage:
#   .\scripts\install.cmd
#   .\scripts\install.cmd -Distro Ubuntu
#   .\scripts\install.cmd -LinuxRepoDir "~/LeanFlow"
#   .\scripts\install.cmd -Python python3.13 -NonInteractive
#   .\scripts\install.cmd -LeanProbeSource "~/LeanProbe"
#   .\scripts\install.cmd -WithLemex
#
# ============================================================================

param(
    [switch]$WithWorkspace,
    [switch]$WithLemex,
    [string]$Distro = "",
    [string]$LinuxRepoDir = "~/LeanFlow",
    [string]$Branch = "",
    [string]$Python = "",
    [string]$LeanProbeSource = "",
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"

$DefaultRepoUrl = "https://github.com/epfl-lara/LeanFlow.git"
$script:ResolvedDistro = $null

function Write-Banner {
    Write-Host ""
    Write-Host "+---------------------------------------------------------+" -ForegroundColor Magenta
    Write-Host "|               LeanFlow WSL Installer                    |" -ForegroundColor Magenta
    Write-Host "+---------------------------------------------------------+" -ForegroundColor Magenta
    Write-Host "|   Windows uses WSL2 and the shared installer flow        |" -ForegroundColor Magenta
    Write-Host "+---------------------------------------------------------+" -ForegroundColor Magenta
    Write-Host ""
    Write-Host "[note] The first install downloads Python dependencies and Lean tools." -ForegroundColor Yellow
    Write-Host ""
}

function Write-Info {
    param([string]$Message)
    Write-Host "-> $Message" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "[ok] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[warn] $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host "[err] $Message" -ForegroundColor Red
}

function Resolve-Branch {
    if ($Branch) {
        return $Branch
    }

    try {
        $current = git -C (Join-Path $PSScriptRoot "..") rev-parse --abbrev-ref HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and $current) {
            $trimmed = $current.Trim()
            if ($trimmed -and $trimmed -ne "HEAD") {
                return $trimmed
            }
        }
    } catch {
    }

    return "main"
}

function Get-RerunCommand {
    $parts = @(".\scripts\install.cmd")
    if ($WithWorkspace) {
        $parts += "-WithWorkspace"
    }
    if ($WithLemex) {
        $parts += "-WithLemex"
    }
    if ($Distro) {
        $parts += "-Distro"
        $parts += "'" + $Distro.Replace("'", "''") + "'"
    }
    if ($LinuxRepoDir -and $LinuxRepoDir -ne "~/LeanFlow") {
        $parts += "-LinuxRepoDir"
        $parts += "'" + $LinuxRepoDir.Replace("'", "''") + "'"
    }
    if ($Branch) {
        $parts += "-Branch"
        $parts += "'" + $Branch.Replace("'", "''") + "'"
    }
    if ($Python) {
        $parts += "-Python"
        $parts += "'" + $Python.Replace("'", "''") + "'"
    }
    if ($LeanProbeSource) {
        $parts += "-LeanProbeSource"
        $parts += "'" + $LeanProbeSource.Replace("'", "''") + "'"
    }
    if ($NonInteractive) {
        $parts += "-NonInteractive"
    }
    return ($parts -join " ")
}

function Install-WSLDistro {
    param([string]$TargetDistro)

    $rerunCommand = Get-RerunCommand

    if ($NonInteractive) {
        throw "WSL distro '$TargetDistro' needs setup. Run wsl --install -d '$TargetDistro' --no-launch, initialize it, then rerun: $rerunCommand"
    }

    Write-Warn "No initialized WSL distro is available yet."
    Write-Info "Installing the WSL distro '$TargetDistro' now."
    Write-Warn "Windows may prompt for elevation, enable WSL features, or require a restart."
    Write-Warn "If WSL drops you into the new Linux shell, type 'exit' to return here, then rerun:"
    Write-Host "  $rerunCommand" -ForegroundColor Yellow

    # Native stderr must not terminate Windows PowerShell before we inspect its exit code.
    $savedErrorPreference = $ErrorActionPreference
    $savedNativePreference = $PSNativeCommandUseErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $PSNativeCommandUseErrorActionPreference = $false
        & wsl.exe --install -d $TargetDistro --no-launch
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedErrorPreference
        $PSNativeCommandUseErrorActionPreference = $savedNativePreference
    }
    if ($exitCode -ne 0) {
        throw "WSL distro install failed with code $exitCode"
    }

    Write-Host ""
    Write-Warn "WSL reported a successful install command."
    Write-Warn "If the distro setup opened a Linux shell, type 'exit' there to return to PowerShell."
    Write-Warn "If Windows asks you to restart or finish distro setup, do that first and then rerun:"
    Write-Host "  $rerunCommand" -ForegroundColor Yellow
    throw "Finish WSL installation and initialize '$TargetDistro', then rerun the installer."
}

function Get-InstalledWSLDistros {
    $ErrorActionPreference = "Continue"
    $PSNativeCommandUseErrorActionPreference = $false
    $listOutput = & wsl.exe --list --quiet 2>&1
    $exitCode = $LASTEXITCODE
    $listText = ((@($listOutput | ForEach-Object { $_.ToString() }) -join "`n") -replace "`0", "").Trim()
    if ($exitCode -ne 0) {
        # Current WSL returns an empty successful quiet list; older clients can
        # return a no-distributions diagnostic. Do not hide unrelated WSL failures.
        if ($listText -match '(?im)^\s*Windows Subsystem for Linux has no installed distributions\.?\s*$' -or
            $listText -match '\bWSL_E_DEFAULT_DISTRO_NOT_FOUND\b') {
            return @()
        }
        throw "Could not list installed WSL distributions (exit $exitCode): $listText. Check wsl --status and wsl --list --verbose; if WSL is not installed, run wsl --install, finish setup, then rerun."
    }

    $distros = @()
    foreach ($line in $listText.Split("`n")) {
        $trimmed = $line.Trim()
        if (-not $trimmed) {
            continue
        }
        $cleaned = ($trimmed -replace "`0", "").Trim()
        if ($cleaned) {
            $distros += $cleaned
        }
    }
    return $distros
}

function Get-WSLDistroMetadata {
    param([string[]]$InstalledDistros)

    if ($InstalledDistros.Count -eq 0) {
        return @()
    }
    $ErrorActionPreference = "Continue"
    $PSNativeCommandUseErrorActionPreference = $false
    $output = & wsl.exe --list --verbose 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $diagnostic = (($output | Out-String) -replace "`0", "").Trim()
        throw "Could not determine installed WSL versions (exit $exitCode): $diagnostic. Check wsl --list --verbose."
    }
    $lines = (($output | Out-String) -replace "`0", "").Split("`n")
    foreach ($name in $InstalledDistros) {
        $found = $false
        $pattern = '^\s*(\*)?\s*' + [regex]::Escape($name) + '\s+.+?\s+([12])\s*$'
        foreach ($line in $lines) {
            if ($line -match $pattern) {
                [pscustomobject]@{
                    Name = $name
                    Version = [int]$Matches[2]
                    Default = [bool]$Matches[1]
                }
                $found = $true
                break
            }
        }
        if (-not $found) {
            throw "Could not determine the WSL version of '$name'. Check wsl --list --verbose."
        }
    }
}

function Test-WSLBash {
    param([string]$TargetDistro, [switch]$AllowRoot)

    $probeArgs = @()
    if ($TargetDistro) {
        $probeArgs += @("-d", $TargetDistro)
    }
    $probeArgs += @("--exec", "bash", "--noprofile", "--norc", "-c", "printf ok:; id -u")

    $ErrorActionPreference = "Continue"
    $PSNativeCommandUseErrorActionPreference = $false
    $probeOutput = & wsl.exe @probeArgs 2>$null
    $exitCode = $LASTEXITCODE
    $probeText = ($probeOutput | Out-String).Trim()
    if ($exitCode -ne 0 -or $probeText -notmatch '^ok:([0-9]+)$') {
        return $false
    }
    if ($Matches[1] -eq "0" -and -not $AllowRoot) {
        Write-Warn "'$TargetDistro' currently runs as root. Open it from the Windows Start menu and finish Linux user setup, then rerun. Use -Distro '$TargetDistro' only for an intentional root install."
        return $false
    }
    return $true
}

function Ensure-WSL {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Write-Err "WSL is not installed on this machine."
        Write-Info "Install it with:"
        Write-Host "  wsl --install -d Ubuntu" -ForegroundColor Yellow
        throw "WSL is required"
    }

    $installedDistros = @(Get-InstalledWSLDistros)
    if ($Distro) {
        if ($installedDistros -notcontains $Distro) {
            Install-WSLDistro -TargetDistro $Distro
        }
        $metadata = @(Get-WSLDistroMetadata -InstalledDistros $installedDistros)
        $selected = @($metadata | Where-Object { $_.Name -eq $Distro })[0]
        if ($selected.Version -ne 2) {
            throw "'$Distro' uses WSL 1. LeanFlow requires WSL 2; convert it yourself or select a WSL 2 distro."
        }
        if (-not (Test-WSLBash -TargetDistro $Distro -AllowRoot)) {
            Write-Err "Could not start the WSL distro '$Distro'."
            Write-Info "Open that distro once to finish first-run setup, then rerun this installer."
            throw "WSL shell unavailable"
        }
        $script:ResolvedDistro = $Distro
        return
    }

    if ($installedDistros.Count -gt 0) {
        $metadata = @(Get-WSLDistroMetadata -InstalledDistros $installedDistros)
        $candidates = @($metadata | Where-Object { $_.Version -eq 2 } | Sort-Object `
            @{Expression = { -not $_.Default }}, @{Expression = { $_.Name -ne "Ubuntu" }})
        foreach ($candidate in $candidates) {
            if (Test-WSLBash -TargetDistro $candidate.Name) {
                $script:ResolvedDistro = $candidate.Name
                return
            }
        }
        throw "No initialized WSL 2 bash shell with a normal Linux user is available. Open your distro from the Windows Start menu and finish Linux user setup, then rerun or select an initialized WSL 2 distro with -Distro."
    }

    Install-WSLDistro -TargetDistro "Ubuntu"
}

function Build-InstallScript {
    return @'
set -euo pipefail

if ! command -v base64 >/dev/null 2>&1; then
  echo "[err] The WSL distro needs base64 (coreutils) to read the installer payload."
  exit 1
fi
decode_argument() {
  local decoded
  decoded="$(printf '%s' "$1" | base64 -d; printf '.')"
  decoded="${decoded%.}"
  printf '%s' "${decoded:1}"
}
REPO_URL="$(decode_argument "${1:?missing REPO_URL}")"
REQUESTED_BRANCH="$(decode_argument "${2:?missing REQUESTED_BRANCH}")"
TARGET_DIR_RAW="$(decode_argument "${3:?missing TARGET_DIR_RAW}")"
CREATE_WORKSPACE="$(decode_argument "${4:?missing CREATE_WORKSPACE}")"
REQUESTED_PYTHON="$(decode_argument "${5-}")"
ALLOW_BRANCH_FALLBACK="$(decode_argument "${6:?missing ALLOW_BRANCH_FALLBACK}")"
NONINTERACTIVE="$(decode_argument "${7:?missing NONINTERACTIVE}")"
LEAN_PROBE_SOURCE_RAW="$(decode_argument "${8:?missing LEAN_PROBE_SOURCE}")"
WITH_LEMEX="$(decode_argument "${9:?missing WITH_LEMEX}")"
if [ "$NONINTERACTIVE" = "1" ]; then
  export GIT_TERMINAL_PROMPT=0
fi

case "$TARGET_DIR_RAW" in
  "~")
    TARGET_DIR="$HOME"
    ;;
  "~/"*)
    TARGET_DIR="$HOME/${TARGET_DIR_RAW:2}"
    ;;
  *)
    TARGET_DIR="$TARGET_DIR_RAW"
    ;;
esac

python_supported() {
  command -v "$1" >/dev/null 2>&1 && "$1" -c 'import venv, ensurepip, sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1
}
select_python() {
  if [ -n "$REQUESTED_PYTHON" ]; then
    if python_supported "$REQUESTED_PYTHON"; then
      SELECTED_PYTHON="$REQUESTED_PYTHON"
      return 0
    fi
    return 1
  fi
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3 python; do
    if python_supported "$candidate"; then
      SELECTED_PYTHON="$candidate"
      return 0
    fi
  done
  return 1
}
if [ -n "$REQUESTED_PYTHON" ] && ! select_python; then
  echo "[err] The selected WSL Python needs Python 3.12+ with venv support: $REQUESTED_PYTHON"
  exit 1
fi
if ! command -v git >/dev/null 2>&1 || ! select_python; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    if [ "$(id -u)" = "0" ]; then
      DEBIAN_FRONTEND=noninteractive apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv python3-pip
    elif command -v sudo >/dev/null 2>&1; then
      sudo_args=()
      if [ "$NONINTERACTIVE" = "1" ]; then
        sudo_args+=(-n)
      fi
      sudo "${sudo_args[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update
      sudo "${sudo_args[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv python3-pip
    else
      echo "[err] Install git, Python 3.12+, and Python venv support inside WSL, then rerun."
      exit 1
    fi
  else
    echo "[err] Install git, Python 3.12+, and Python venv support inside WSL, then rerun."
    exit 1
  fi
fi

if ! select_python; then
  echo "[err] The WSL distro needs Python 3.12+ with venv support."
  echo "  Install a supported Python, select it with -Python, or use a newer WSL distro, then rerun."
  exit 1
fi

CHOSEN_BRANCH="$REQUESTED_BRANCH"
if ! git ls-remote --exit-code --heads "$REPO_URL" "$REQUESTED_BRANCH" >/dev/null 2>&1; then
  if [ "$ALLOW_BRANCH_FALLBACK" != "1" ]; then
    echo "[err] The requested branch is unavailable: $REQUESTED_BRANCH"
    exit 1
  fi
  if [ "$REQUESTED_BRANCH" != "main" ]; then
    echo "-> Branch $REQUESTED_BRANCH is not published on origin; falling back to main."
  fi
  CHOSEN_BRANCH="main"
fi

mkdir -p "$(dirname "$TARGET_DIR")"

if [ -e "$TARGET_DIR/.git" ]; then
  checkout_status="$(git -C "$TARGET_DIR" status --porcelain)"
  if [ -n "$checkout_status" ]; then
    echo "[err] Existing WSL checkout at $TARGET_DIR has local changes."
    echo "  Commit or stash them, then rerun this installer."
    exit 1
  fi
  git -C "$TARGET_DIR" fetch origin "$CHOSEN_BRANCH" --tags
  if ! git -C "$TARGET_DIR" merge-base --is-ancestor HEAD FETCH_HEAD; then
    echo "[err] Existing WSL checkout contains unpublished or divergent commits."
    echo "  Keep that checkout intact; use a different -LinuxRepoDir or reconcile it yourself."
    exit 1
  fi
  if git -C "$TARGET_DIR" show-ref --verify --quiet "refs/heads/$CHOSEN_BRANCH"; then
    if ! git -C "$TARGET_DIR" merge-base --is-ancestor "refs/heads/$CHOSEN_BRANCH" FETCH_HEAD; then
      echo "[err] The selected local branch contains unpublished or divergent commits."
      echo "  Use a different -LinuxRepoDir or reconcile that branch yourself."
      exit 1
    fi
    git -C "$TARGET_DIR" switch "$CHOSEN_BRANCH"
  else
    git -C "$TARGET_DIR" switch -c "$CHOSEN_BRANCH" --track "origin/$CHOSEN_BRANCH"
  fi
  git -C "$TARGET_DIR" pull --ff-only origin "$CHOSEN_BRANCH"
  git -C "$TARGET_DIR" submodule update --init --recursive
else
  git clone --branch "$CHOSEN_BRANCH" --recurse-submodules "$REPO_URL" "$TARGET_DIR"
fi

cd "$TARGET_DIR"
if [ "$CREATE_WORKSPACE" = "1" ]; then
  echo "-> -WithWorkspace is accepted for compatibility."
  echo "-> Run leanflow project init inside your Lean project after installation."
fi
install_args=(--python "$SELECTED_PYTHON")
required_flags=()
if [ "$NONINTERACTIVE" = "1" ]; then
  install_args+=(--non-interactive)
  required_flags+=(--non-interactive)
fi
if [ -n "$LEAN_PROBE_SOURCE_RAW" ]; then
  case "$LEAN_PROBE_SOURCE_RAW" in
    "~") LEAN_PROBE_SOURCE="$HOME" ;;
    "~/"*) LEAN_PROBE_SOURCE="$HOME/${LEAN_PROBE_SOURCE_RAW:2}" ;;
    *) LEAN_PROBE_SOURCE="$LEAN_PROBE_SOURCE_RAW" ;;
  esac
  install_args+=(--lean-probe-source "$LEAN_PROBE_SOURCE")
  required_flags+=(--lean-probe-source)
fi
if [ "$WITH_LEMEX" = "1" ]; then
  install_args+=(--with-lemex)
  required_flags+=(--with-lemex)
fi
if [ "${#required_flags[@]}" -gt 0 ]; then
  if ! installer_help="$(./scripts/install-internal.sh --help)"; then
    echo "[err] The selected branch does not expose a compatible shared installer."
    echo "  Select a published matching revision with -Branch, or install the coordinated source bundle inside initialized WSL."
    exit 1
  fi
  for requested_flag in "${required_flags[@]}"; do
    if ! grep -Eq -- "(^|[[:space:]])${requested_flag}([[:space:]]|$)" <<< "$installer_help"; then
      echo "[err] The selected branch's shared installer does not support $requested_flag."
      echo "  Select a published matching revision with -Branch, or install the coordinated source bundle inside initialized WSL."
      exit 1
    fi
  done
fi
echo "-> The shared installer uses matching local LeanProbe source or downloads its pinned version."
echo "  Use -LeanProbeSource to select local source explicitly, including for offline installation."
./scripts/install-internal.sh "${install_args[@]}"
'@
}

function ConvertTo-NativeArgument {
    param([string]$Value)

    # ProcessStartInfo.ArgumentList is unavailable in Windows PowerShell 5.1.
    # Apply Microsoft's native argv rules, including trailing backslashes.
    $escaped = $Value -replace '(\\*)"', '${1}${1}\"'
    $escaped = $escaped -replace '(\\+)$', '${1}${1}'
    return '"' + $escaped + '"'
}

function Resolve-WSLPath {
    param([string]$WindowsPath)

    $normalizedPath = $WindowsPath -replace '\\', '/'

    $pathArgs = @()
    if ($script:ResolvedDistro) {
        $pathArgs += @("-d", $script:ResolvedDistro)
    }
    $pathArgs += @("--exec", "wslpath", "-u", "-a", $normalizedPath)

    # wslpath emits Linux UTF-8. Read it directly instead of changing the user's
    # console encoding or passing its non-ASCII output through a legacy code page.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = (Get-Command wsl.exe -CommandType Application).Source
    $startInfo.Arguments = (@($pathArgs | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false, $true)
    $startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $startInfo.EnvironmentVariables['WSL_UTF8'] = '1'
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "Could not start WSL path translation."
        }
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $wslPath = $outputTask.GetAwaiter().GetResult().Trim()
        $diagnostic = $errorTask.GetAwaiter().GetResult().Trim()
        if ($process.ExitCode -ne 0 -or -not $wslPath) {
            throw "Could not translate Windows path into a WSL path: $WindowsPath. $diagnostic"
        }
        return $wslPath
    } finally {
        $process.Dispose()
    }
}

function Main {
    Write-Banner
    Write-Info "LeanFlow on Windows runs through WSL2."
    Write-Info "This bootstrap clones into your WSL home and then runs ./scripts/install-internal.sh there."
    Ensure-WSL

    $resolvedBranch = Resolve-Branch
    Write-Info "Using repository branch: $resolvedBranch"
    if ($script:ResolvedDistro) {
        Write-Info "Using WSL distro: $script:ResolvedDistro"
    } else {
        Write-Info "Using your default WSL distro"
    }
    Write-Info "Using WSL repo path: $LinuxRepoDir"

    $bashScript = (Build-InstallScript).Replace("`r`n", "`n").Replace("`r", "`n")
    $workspaceFlag = if ($WithWorkspace.IsPresent) { "1" } else { "0" }
    $tempDir = Join-Path $env:TEMP ("leanflow-wsl-bootstrap-" + [guid]::NewGuid().ToString("N"))
    $tempScriptPath = Join-Path $tempDir "install.sh"
    try {
        New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
        [System.IO.File]::WriteAllText(
            $tempScriptPath,
            $bashScript,
            [System.Text.UTF8Encoding]::new($false)
        )
        $wslScriptPath = Resolve-WSLPath -WindowsPath $tempScriptPath

        $wslArgs = @()
        if ($script:ResolvedDistro) {
            $wslArgs += @("-d", $script:ResolvedDistro)
        }
        $allowBranchFallback = if ($Branch) { "0" } else { "1" }
        $noninteractiveFlag = if ($NonInteractive) { "1" } else { "0" }
        $lemexFlag = if ($WithLemex) { "1" } else { "0" }
        $payload = @($DefaultRepoUrl, $resolvedBranch, $LinuxRepoDir, $workspaceFlag, $Python, $allowBranchFallback, $noninteractiveFlag, $LeanProbeSource, $lemexFlag)
        # Windows PowerShell's legacy native argument handling loses embedded
        # quotes. Pass only ASCII base64 payloads across the native boundary.
        $encodedPayload = @($payload | ForEach-Object {
            [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes("." + $_))
        })
        $wslArgs += @("--exec", "bash", $wslScriptPath)
        $wslArgs += $encodedPayload

        # Keep WSL attached directly to the terminal for interactive sudo prompts.
        $savedErrorPreference = $ErrorActionPreference
        $savedNativePreference = $PSNativeCommandUseErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $PSNativeCommandUseErrorActionPreference = $false
            & wsl.exe @wslArgs
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $savedErrorPreference
            $PSNativeCommandUseErrorActionPreference = $savedNativePreference
        }
        if ($exitCode -ne 0) {
            throw "WSL install flow exited with code $exitCode"
        }
    } finally {
        Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
    }

    Write-Host ""
    Write-Success "LeanFlow is installed in WSL."
    Write-Info "For daily use, open your WSL shell inside your Lean project and run:"
    Write-Host '  export PATH="$HOME/.local/bin:$PATH"' -ForegroundColor Yellow
    Write-Host "  leanflow" -ForegroundColor Yellow
    Write-Host ""
}

try {
    Main
} catch {
    Write-Host ""
    Write-Err "$_"
    Write-Host ""
    exit 1
}
