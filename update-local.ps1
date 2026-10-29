# Run from inside the repository after stopping the web app.
# Keep per-computer settings while migrating older, tracked configurations.
$ErrorActionPreference = 'Stop'
$gitCommand = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$gitExecutable = if ($gitCommand) { $gitCommand.Source } else { $null }
if (!$gitExecutable) {
    $candidates = @()
    foreach ($installRoot in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, "$env:LOCALAPPDATA\Programs")) {
        if ($installRoot) { $candidates += Join-Path $installRoot 'Git\cmd\git.exe' }
    }
    # GitHub Desktop includes Git even when Git is absent from PowerShell's PATH.
    foreach ($desktopRoot in @("$env:LOCALAPPDATA\GitHubDesktop", "$env:ProgramFiles\GitHub Desktop")) {
        $candidates += Join-Path $desktopRoot 'resources\app\git\cmd\git.exe'
        Get-ChildItem -LiteralPath $desktopRoot -Directory -Filter 'app-*' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | ForEach-Object {
                $candidates += Join-Path $_.FullName 'resources\app\git\cmd\git.exe'
            }
    }
    $gitExecutable = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (!$gitExecutable) { throw 'Git was not found. Install Git for Windows or GitHub Desktop, then rerun this updater.' }
Write-Host "Using Git: $gitExecutable"
$repoRoot = & $gitExecutable rev-parse --show-toplevel
if ($LASTEXITCODE -ne 0) { throw 'Open PowerShell inside the camsprog repository first.' }

$configBackupDir = Join-Path ([System.IO.Path]::GetTempPath()) ('camsprog-settings-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $configBackupDir | Out-Null
Write-Host "Local settings backup: $configBackupDir"
$localSettings = @('config.json', 'state.json', 'heartbeat.json')
$backedUp = @()
foreach ($name in $localSettings) {
    $original = Join-Path $repoRoot ('cctv web/' + $name)
    if (Test-Path -LiteralPath $original -PathType Leaf) {
        Copy-Item -LiteralPath $original -Destination (Join-Path $configBackupDir $name)
        $backedUp += $name
    }
}

try {
    # Restore only known local-data files; preserve all source-code edits.
    $trackedLocal = @(& $gitExecutable -C $repoRoot ls-files -- 'cctv web/config.json' 'cctv web/state.json' 'cctv web/heartbeat.json' ':(glob)**/__pycache__/**' ':(glob)**/*.pyc')
    if ($LASTEXITCODE -ne 0) { throw 'Could not check tracked local-data files.' }
    if ($trackedLocal.Count -gt 0) {
        & $gitExecutable -C $repoRoot restore --source=HEAD --staged --worktree -- @trackedLocal
        if ($LASTEXITCODE -ne 0) { throw 'Could not prepare local-data files for the update.' }
    }
    & $gitExecutable -C $repoRoot pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        throw 'Git pull failed. Resolve the Git error and run this updater again.'
    }
}
finally {
    foreach ($name in $backedUp) {
        Copy-Item -LiteralPath (Join-Path $configBackupDir $name) -Destination (Join-Path $repoRoot ('cctv web/' + $name)) -Force
    }
    Write-Host "Local settings restored. Backup retained at: $configBackupDir"
}

Write-Host 'Update complete. You can restart the web app.'
