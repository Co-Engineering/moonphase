# Install the Moonphase desktop app on Windows.
#
#   irm https://raw.githubusercontent.com/Co-Engineering/moonphase/main/scripts/install-app.ps1 | iex
#
# This is the app, not the server. It connects to a Moonphase you are already
# running — it asks for the address on first launch — and installing it changes
# nothing on any server.
#
# Installs for the current user, so it needs no administrator rights. Running it
# again upgrades in place, which is also how you update.

$ErrorActionPreference = 'Stop'

$repo = if ($env:MOONPHASE_REPO) { $env:MOONPHASE_REPO } else { 'Co-Engineering/moonphase' }
$channel = if ($env:MOONPHASE_CHANNEL) { $env:MOONPHASE_CHANNEL } else { 'edge' }

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Blue }
function Write-Warn($message) { Write-Host " warn $message" -ForegroundColor Yellow }

# The architecture of the machine, not of PowerShell: a 32-bit host on a 64-bit
# machine should still get the 64-bit build.
$arch = switch ($env:PROCESSOR_ARCHITECTURE) {
  'ARM64' { 'arm64' }
  default { 'x64' }
}

Write-Step "Looking up the $channel build"
try {
  $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$channel" `
    -Headers @{ 'User-Agent' = 'moonphase-installer' }
} catch {
  throw "Could not reach GitHub to find the $channel release. $_"
}

# Sorted newest first: a release can carry more than one build for the same
# platform and architecture (a stale one left over from a previous publish, an
# `edge` republish that failed to clean up after itself), and picking anything
# but the most recent one installs an old build with no indication that it is
# not the current one.
$asset = $release.assets |
  Where-Object { $_.name -like '*.exe' -and $_.name -like "*$arch*" } |
  Sort-Object -Property created_at -Descending |
  Select-Object -First 1

# Only one Windows build for this architecture is normal; falling back to the
# sole installer beats failing with "not found".
if (-not $asset) {
  $asset = $release.assets |
    Where-Object { $_.name -like '*.exe' } |
    Sort-Object -Property created_at -Descending |
    Select-Object -First 1
}
if (-not $asset) {
  throw "No Windows build in the $channel release."
}

$installer = Join-Path ([System.IO.Path]::GetTempPath()) $asset.name

Write-Step "Downloading $($asset.name)"
# Progress rendering makes Invoke-WebRequest dramatically slower on large files.
$previousProgress = $ProgressPreference
$ProgressPreference = 'SilentlyContinue'
try {
  Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installer
} finally {
  $ProgressPreference = $previousProgress
}

# Unsigned, so SmartScreen would put a "Windows protected your PC" panel in
# front of a double-click. Running it from here with /S installs silently for
# the current user. Nothing is disabled and nothing is bypassed — the file is
# simply run by the installer you chose to invoke.
Write-Step "Installing for $env:USERNAME (no administrator rights needed)"
$process = Start-Process -FilePath $installer -ArgumentList '/S' -Wait -PassThru
if ($process.ExitCode -ne 0) {
  throw "The installer exited with code $($process.ExitCode)."
}

Remove-Item $installer -Force -ErrorAction SilentlyContinue

Write-Host ''
Write-Host 'Moonphase is installed.' -ForegroundColor Green
Write-Host ''
Write-Host '  Find it in the Start menu, or on your desktop.'
Write-Host ''
Write-Host 'On first launch it asks for the address of your Moonphase server.'
Write-Host 'Do not have one yet? https://co-engineering.github.io/moonphase/getting-started/docker/'
Write-Host ''

if ($release.prerelease) {
  Write-Warn 'This is the edge build, rebuilt on every commit.'
}
