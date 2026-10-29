# Cams WebApp

A lightweight web dashboard to monitor NVRs and cameras from your browser.

## Features (initial)
- Reads `config.json` from the project root to load NVRs
- Background polling to check reachability and update status
- Simple dashboard page listing name, IP, status, last online, camera/recording counts
- JSON API for integration

## Recorder accounts

Use the small people icon in each recorder's Actions cell to load its account list.
Other users and administrators appear in separate groups. Hikvision and Milesight use
their native account lists; other recorders use ONVIF GetUsers. ONVIF accounts may differ from
local recorder accounts, and require ONVIF to be enabled with appropriate permissions.
The saved recorder login is used automatically; no second login is requested.

Recorders generally do not return account passwords. Enter a known password once
and choose **Save in app**, then use **Show saved** to reveal it. Saving here does
not change the recorder login or device password. Saved passwords are kept in the
app's MongoDB database, using the same access boundary as existing recorder logins.
Revealed values are cleared when the panel closes. Account lookup happens only when
the panel is opened or reloaded, rather than during monitoring polls.

## Time synchronization

Sync uses the server's clock and local timezone, so configure those correctly on
the computer running the app. Milesight, Hikvision, and Uniview use native APIs
with clock read-back checks. If native sync fails, or the recorder has no type
selected, the app tries ONVIF SetSystemDateAndTime and verifies its UTC clock.
Manual sync switches the recorder to manual time; automatic NTP can be re-enabled
in its own settings. A success response requires the clock to be within 15 seconds.
Unsupported firmware, disabled ONVIF, inaccessible devices, or accounts without
time-setting permission produce an error instead of an unverified success.
Each dashboard row shows whether sync was verified and the measured clock difference.
The result stays visible through live table updates. **Sync All** shows the verified
and failed totals and a separate outcome for each recorder.

### Automatic time sync

In Settings → Time Synchronization, enable automatic time sync and choose all
recorders or select one or more NVRs. Set the drift threshold (default: 3 minutes)
and how often to check (default: every minute), then save. This threshold is separate
from the dashboard's drift warning threshold. Switching off and saving stops new
automatic corrections.

The server reads a fresh clock from each chosen recorder and corrects it only when
the absolute drift exceeds the threshold. Manual and automatic sync cannot run at
the same time on the same recorder. Unreadable clocks are never corrected blindly;
failed lookups and corrections have a five-minute retry delay. Settings survive
restarts in MongoDB, and latest per-recorder outcomes are stored in
`auto_time_sync_results`. The Settings panel shows the latest running-session checks,
including measured drift and whether a correction was verified.

Automatic sync is off by default and runs in the web app server, independently of
whether a browser page is open. It pauses if its saved settings cannot be read.

## Install

Each computer keeps its own `cctv web/config.json`; Git does not track local
settings, runtime state, or Python bytecode. `config.example.json` contains empty,
safe defaults. The Windows launcher copies it only when `config.json` is missing.
For a manual installation, run this from the `cctv web` directory before starting:

```powershell
if (!(Test-Path -LiteralPath .\config.json)) {
    Copy-Item -LiteralPath .\config.example.json -Destination .\config.json
}
```

```powershell
python -m pip install -r cams-webapp/requirements.txt
```

### Updating an older installation

Older versions tracked `config.json`, runtime state, and Python bytecode. If pulling
reports that local changes would be overwritten, stop the web app and run the
following from the repository root in PowerShell. The updater backs up local
settings outside the repository, pulls the code, and restores those settings even
if the pull fails. It leaves source-code edits alone and retains the backup.

```powershell
$updater = Join-Path $env:TEMP 'camsprog-update.ps1'
Invoke-WebRequest 'https://raw.githubusercontent.com/gilkh/camsprog/main/update-local.ps1' -OutFile $updater -ErrorAction Stop
powershell -NoProfile -ExecutionPolicy Bypass -File $updater
```

Restart the app afterward. Future pulls leave this computer's configuration alone.
Once updated, you can also run `.\update-local.ps1` from the repository root.

## Run
```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --app-dir cams-webapp
```

Then open `http://127.0.0.1:8000/` in your browser.

## Notes
- The webapp uses a background thread to poll NVRs every 60 seconds.
- It reads from the root `config.json`. If both `config.nvrs` and top-level `nvrs` exist, the top-level list is used.
- Camera/recording counts are displayed if present in `config.json`; otherwise they show `Unknown`.
- Vendor-specific camera/recording retrieval can be added later.
