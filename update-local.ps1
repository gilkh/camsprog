# Run from inside the repository after stopping the web app.
# Keep per-computer settings while migrating older, tracked configurations.
$ErrorActionPreference = 'Stop'
$repoRoot = git rev-parse --show-toplevel
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
    $trackedLocal = @(git -C $repoRoot ls-files -- 'cctv web/config.json' 'cctv web/state.json' 'cctv web/heartbeat.json' ':(glob)**/__pycache__/**' ':(glob)**/*.pyc')
    if ($LASTEXITCODE -ne 0) { throw 'Could not check tracked local-data files.' }
    if ($trackedLocal.Count -gt 0) {
        git -C $repoRoot restore --source=HEAD --staged --worktree -- @trackedLocal
        if ($LASTEXITCODE -ne 0) { throw 'Could not prepare local-data files for the update.' }
    }
    git -C $repoRoot pull --ff-only
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
