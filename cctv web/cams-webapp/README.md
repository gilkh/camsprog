# Cams WebApp

A lightweight web dashboard to monitor NVRs and cameras from your browser.

## Features (initial)
- Reads `config.json` from the project root to load NVRs
- Background polling to check reachability and update status
- Simple dashboard page listing name, IP, status, last online, camera/recording counts
- JSON API for integration

## Install
```powershell
python -m pip install -r cams-webapp/requirements.txt
```

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