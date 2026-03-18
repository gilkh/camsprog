import os
import json
import threading
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .monitor import MonitorState
from pydantic import BaseModel, Field
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import requests
from requests.auth import HTTPDigestAuth
from datetime import datetime, timezone
import time

# Fast JSON helpers (prefer orjson)
try:
    import orjson

    def json_dumps(obj) -> bytes:
        return orjson.dumps(obj)
except Exception:
    def json_dumps(obj) -> bytes:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

app = FastAPI(title="Cams WebApp", version="0.1.0")

# Static and templates setup (use absolute paths for reliability)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

monitor = MonitorState(poll_interval=60)


def _normalize_smtp_to(value):
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        seen = set()
        for v in value:
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                continue
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
        return out
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("\n", ",").split(",")]
        out = []
        seen = set()
        for p in parts:
            if not p:
                continue
            k = p.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out
    return []


def _load_settings_from_file():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        cfg = data.get("config")
        if isinstance(cfg, dict):
            return cfg
        return {}
    except Exception:
        return {}


def _save_settings_to_file(update: dict):
    try:
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        cfg = data.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
        for key, value in update.items():
            cfg[key] = value
        data["config"] = cfg
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        pass


def _get_merged_settings() -> dict:
    file_s = _load_settings_from_file()
    try:
        db_s = app.state.db["settings"].find_one({"_id": "global"}) or {}
    except Exception:
        db_s = {}
    merged = dict(file_s if isinstance(file_s, dict) else {})
    if isinstance(db_s, dict):
        merged.update(db_s)
    merged.pop("_id", None)
    merged["smtp_to"] = _normalize_smtp_to(merged.get("smtp_to"))
    return merged


def _parse_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _get_smtp_targets(settings: dict) -> list[tuple[str, int]]:
    targets = []
    primary_host = (settings.get("smtp_host") or "").strip()
    primary_port = _parse_int(settings.get("smtp_port"))
    secondary_host = (settings.get("smtp_host_2") or "").strip()
    secondary_port = _parse_int(settings.get("smtp_port_2"))

    if primary_host and primary_port:
        targets.append((primary_host, primary_port))
    if secondary_host and secondary_port:
        # Avoid trying the exact same endpoint twice.
        if (secondary_host, secondary_port) not in targets:
            targets.append((secondary_host, secondary_port))
    return targets


def _parse_nvr_time_epoch(value):
    if not isinstance(value, str):
        return None
    txt = value.strip()
    if not txt:
        return None
    lowered = txt.lower()
    if lowered in {"offline", "unknown", "auth failed", "not checked", "parse error"}:
        return None
    if lowered.startswith("time failed"):
        return None
    try:
        dt = datetime.fromisoformat(txt.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return int(dt.timestamp())
    except Exception:
        return None


def _build_current_alerts(snapshot: list, settings: dict) -> dict:
    now_ts = int(time.time())
    tolerance_sec = _parse_int(settings.get("time_tolerance"))
    if tolerance_sec is None or tolerance_sec < 30:
        tolerance_sec = 120

    out = {}
    for nvr in snapshot:
        ip = nvr.get("ip") or ""
        if not ip:
            continue
        name = nvr.get("name") or ip
        status = nvr.get("status") or "Unknown"

        if status == "Offline":
            alert_id = f"nvr_offline:{ip}"
            out[alert_id] = {
                "_id": alert_id,
                "alert_type": "nvr_offline",
                "severity": "critical",
                "nvr_ip": ip,
                "nvr_name": name,
                "message": f"NVR {name} ({ip}) is offline",
                "channel": None,
                "status": "active",
            }

        expected = _parse_int(nvr.get("recording_expected"))
        recording = _parse_int(nvr.get("recording_count"))
        if expected is not None and recording is not None and expected >= 0 and recording < expected:
            alert_id = f"recording_expected_mismatch:{ip}"
            out[alert_id] = {
                "_id": alert_id,
                "alert_type": "recording_expected_mismatch",
                "severity": "warning",
                "nvr_ip": ip,
                "nvr_name": name,
                "message": f"Recording count is {recording}, expected {expected}",
                "channel": None,
                "status": "active",
            }

        nvr_time_epoch = _parse_nvr_time_epoch(nvr.get("nvr_time"))
        if nvr_time_epoch is not None:
            drift = abs(now_ts - nvr_time_epoch)
            if drift > tolerance_sec:
                drift_min = int(round(drift / 60.0))
                alert_id = f"nvr_time_drift:{ip}"
                out[alert_id] = {
                    "_id": alert_id,
                    "alert_type": "nvr_time_drift",
                    "severity": "warning",
                    "nvr_ip": ip,
                    "nvr_name": name,
                    "message": f"NVR time drift is about {drift_min} minute(s)",
                    "channel": None,
                    "status": "active",
                }

        statuses = nvr.get("channel_statuses")
        if isinstance(statuses, list):
            for item in statuses:
                if not isinstance(item, dict):
                    continue
                if (item.get("status") or "") != "not-recording":
                    continue
                ch = item.get("channel")
                if ch is None:
                    continue
                alert_id = f"channel_not_recording:{ip}:{ch}"
                out[alert_id] = {
                    "_id": alert_id,
                    "alert_type": "channel_not_recording",
                    "severity": "non-critical",
                    "nvr_ip": ip,
                    "nvr_name": name,
                    "message": f"Channel {ch} is not recording",
                    "channel": ch,
                    "status": "active",
                }
    return out


def _send_alert_email(settings: dict, recipients: list[str], subject: str, body: str):
    targets = _get_smtp_targets(settings)
    username = settings.get("smtp_username") or None
    password = settings.get("smtp_password") or None
    use_tls = bool(settings.get("smtp_tls") or False)
    from_addr = settings.get("smtp_from") or None
    if not targets or not from_addr or not recipients:
        return False, "SMTP target(s), from, or recipients missing", None

    try:
        import smtplib
        from email.message import EmailMessage

        errors = []
        for host, port in targets:
            client = None
            try:
                client = smtplib.SMTP(host, port, timeout=12)
                if use_tls:
                    client.starttls()
                if username and password:
                    client.login(username, password)
                for to_addr in recipients:
                    msg = EmailMessage()
                    msg["Subject"] = subject
                    msg["From"] = from_addr
                    msg["To"] = to_addr
                    msg.set_content(body)
                    client.send_message(msg)
                return True, None, {"host": host, "port": port}
            except Exception as e:
                errors.append(f"{host}:{port} -> {e}")
            finally:
                if client is not None:
                    try:
                        client.quit()
                    except Exception:
                        pass
        return False, " | ".join(errors), None
    except Exception as e:
        return False, str(e), None


def _process_alert_cycle_once():
    try:
        db = app.state.db
        settings = _get_merged_settings()
        snapshot = monitor.get_snapshot()
        current_alerts = _build_current_alerts(snapshot, settings)
        now_ts = int(time.time())

        alerts_col = db["alerts"]
        email_col = db["email_events"]

        current_ids = list(current_alerts.keys())
        existing_by_id = {}
        if current_ids:
            for doc in alerts_col.find({"_id": {"$in": current_ids}}, {"_id": 1, "status": 1}):
                existing_by_id[doc.get("_id")] = doc

        for alert_id, alert_doc in current_alerts.items():
            base_set = {
                "alert_type": alert_doc.get("alert_type"),
                "severity": alert_doc.get("severity"),
                "nvr_ip": alert_doc.get("nvr_ip"),
                "nvr_name": alert_doc.get("nvr_name"),
                "message": alert_doc.get("message"),
                "channel": alert_doc.get("channel"),
                "status": "active",
                "last_seen": now_ts,
            }
            prev = existing_by_id.get(alert_id)
            if not prev:
                base_set.update({
                    "first_seen": now_ts,
                    "acknowledged": False,
                    "acknowledged_at": None,
                    "last_emailed_at": None,
                    "resolved_at": None,
                })
            elif prev.get("status") != "active":
                base_set.update({
                    "first_seen": now_ts,
                    "acknowledged": False,
                    "acknowledged_at": None,
                    "last_emailed_at": None,
                    "resolved_at": None,
                })
            alerts_col.update_one({"_id": alert_id}, {"$set": base_set}, upsert=True)

        if current_ids:
            alerts_col.update_many(
                {"status": "active", "_id": {"$nin": current_ids}},
                {"$set": {"status": "resolved", "resolved_at": now_ts}},
            )
        else:
            alerts_col.update_many(
                {"status": "active"},
                {"$set": {"status": "resolved", "resolved_at": now_ts}},
            )

        recipients = _normalize_smtp_to(settings.get("smtp_to"))
        if not recipients:
            return

        interval = _parse_int(settings.get("alert_email_interval_seconds"))
        if interval is None or interval < 30:
            interval = 1800
        due_before = now_ts - interval

        due_alerts = list(
            alerts_col.find(
                {
                    "status": "active",
                    "acknowledged": {"$ne": True},
                    "$or": [
                        {"last_emailed_at": {"$exists": False}},
                        {"last_emailed_at": None},
                        {"last_emailed_at": {"$lte": due_before}},
                    ],
                },
                {
                    "_id": 1,
                    "severity": 1,
                    "nvr_name": 1,
                    "nvr_ip": 1,
                    "message": 1,
                    "alert_type": 1,
                    "channel": 1,
                },
            )
        )
        if not due_alerts:
            return

        subject = f"Cams Alerts: {len(due_alerts)} active"
        lines = ["Active alerts detected:", ""]
        for d in due_alerts:
            sev = (d.get("severity") or "warning").upper()
            n = d.get("nvr_name") or d.get("nvr_ip") or "Unknown"
            ip = d.get("nvr_ip") or ""
            msg = d.get("message") or (d.get("alert_type") or "Alert")
            lines.append(f"- [{sev}] {n} ({ip}) - {msg}")
        body = "\n".join(lines)

        ok, err, smtp_used = _send_alert_email(settings, recipients, subject, body)
        alert_ids = [x.get("_id") for x in due_alerts if x.get("_id")]
        alerts_col.update_many(
            {"_id": {"$in": alert_ids}},
            {"$set": {"last_emailed_at": now_ts, "last_email_status": "success" if ok else "failed"}},
        )
        email_col.insert_one(
            {
                "created_at": now_ts,
                "subject": subject,
                "to": recipients,
                "alert_ids": alert_ids,
                "count": len(alert_ids),
                "success": bool(ok),
                "error": err,
                "smtp_used": smtp_used,
            }
        )
    except Exception:
        # Keep worker alive and avoid breaking request handlers.
        return


def _alert_worker_loop(stop_event: threading.Event):
    while not stop_event.is_set():
        _process_alert_cycle_once()
        stop_event.wait(30)

class NVRInput(BaseModel):
    name: str
    ip: str
    type: Optional[str] = Field(default=None, description="Milesight, Hikvision, or Uniview")
    username: Optional[str] = None
    password: Optional[str] = None
    recording_expected: Optional[int] = None


@app.on_event("startup")
def startup_event():
    app.state.mongo_client = MongoClient("mongodb://localhost:27017")
    app.state.db = app.state.mongo_client["cameras"]
    monitor.db = app.state.db
    try:
        s = app.state.db["settings"].find_one({"_id": "global"}) or {}
        ri = int(s.get("refresh_interval") or 60)
        if ri >= 1:
            monitor.poll_interval = ri
    except Exception:
        pass
    monitor.start()
    app.state.alert_stop_event = threading.Event()
    app.state.alert_thread = threading.Thread(
        target=_alert_worker_loop,
        args=(app.state.alert_stop_event,),
        name="alert-worker",
        daemon=True,
    )
    app.state.alert_thread.start()
    _process_alert_cycle_once()


@app.on_event("shutdown")
def shutdown_event():
    monitor.stop()
    try:
        app.state.alert_stop_event.set()
        t = getattr(app.state, "alert_thread", None)
        if t and t.is_alive():
            t.join(timeout=2)
    except Exception:
        pass
    try:
        app.state.mongo_client.close()
    except Exception:
        pass


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    return templates.TemplateResponse("calendar.html", {"request": request})


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            # Initial render; client script fetches data
            "nvrs": [],
        },
    )


@app.get("/api/nvrs")
def api_nvrs():
    return JSONResponse(monitor.get_snapshot())

@app.get("/api/state")
def api_state():
    data = {"nvrs": monitor.get_snapshot()}
    return Response(content=json_dumps(data), media_type="application/json")


@app.post("/api/refresh")
def api_refresh():
    monitor.refresh_once()
    return {"status": "ok"}


@app.post("/api/nvrs")
def api_add_nvr(nvr: NVRInput):
    try:
        created = monitor.add_or_update_nvr(nvr.dict())
        return {"status": "ok", "nvr": created}
    except ValueError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@app.delete("/api/nvrs/{ip}")
def api_delete_nvr(ip: str):
    deleted = monitor.delete_nvr(ip)
    if deleted:
        return {"status": "ok"}
    return JSONResponse({"status": "error", "message": "NVR not found"}, status_code=404)


@app.post("/api/nvrs/{ip}/expected_recording")
def api_set_expected_recording(ip: str, payload: dict):
    try:
        expected = payload.get("expected")
        if expected is None:
            return JSONResponse({"status": "error", "message": "expected required"}, status_code=400)
        try:
            expected_int = int(expected)
            if expected_int < 0:
                return JSONResponse({"status": "error", "message": "expected must be >= 0"}, status_code=400)
        except Exception:
            return JSONResponse({"status": "error", "message": "expected must be integer"}, status_code=400)
        with monitor.lock:
            found = False
            for i, nvr in enumerate(monitor.nvrs):
                if nvr.get("ip") == ip:
                    monitor.nvrs[i]["recording_expected"] = expected_int
                    found = True
                    break
        if not found:
            return JSONResponse({"status": "error", "message": "NVR not found"}, status_code=404)
        monitor._write_back()
        return {"status": "ok", "expected": expected_int}
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to set expected"}, status_code=500)


@app.post("/api/nvrs/{ip}/expected_recording/copy_from_cameras")
def api_copy_expected_from_cameras(ip: str):
    try:
        with monitor.lock:
            found = None
            for nvr in monitor.nvrs:
                if nvr.get("ip") == ip:
                    found = nvr
                    break
            if not found:
                return JSONResponse({"status": "error", "message": "NVR not found"}, status_code=404)
            cam = found.get("camera_count")
            try:
                cam_int = int(cam)
            except Exception:
                return JSONResponse({"status": "error", "message": "camera_count not available"}, status_code=400)
            found["recording_expected"] = cam_int
        monitor._write_back()
        return {"status": "ok", "expected": cam_int}
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to copy expected"}, status_code=500)
@app.get("/api/settings")
def api_get_settings():
    try:
        s = _get_merged_settings()
        # include current runtime interval
        s["refresh_interval"] = int(s.get("refresh_interval") or monitor.poll_interval or 60)
        s["smtp_to"] = _normalize_smtp_to(s.get("smtp_to"))
        s["alert_email_interval_seconds"] = int(s.get("alert_email_interval_seconds") or 1800)
        return JSONResponse(s)
    except Exception:
        return JSONResponse({
            "refresh_interval": monitor.poll_interval,
            "smtp_host": None,
            "smtp_port": None,
            "smtp_host_2": None,
            "smtp_port_2": None,
            "smtp_username": None,
            "smtp_password": None,
            "smtp_tls": False,
            "smtp_from": None,
            "smtp_to": [],
            "alert_email_interval_seconds": 1800,
        })


@app.post("/api/settings")
def api_set_settings(payload: dict):
    try:
        update = {}
        if "refresh_interval" in payload:
            try:
                ri = int(payload.get("refresh_interval"))
                if ri >= 1:
                    update["refresh_interval"] = ri
                    monitor.poll_interval = ri
            except Exception:
                pass
        for key in ("smtp_host", "smtp_host_2", "smtp_username", "smtp_password", "smtp_from"):
            if key in payload:
                val = payload.get(key)
                update[key] = val if val is not None else None
        if "smtp_to" in payload:
            update["smtp_to"] = _normalize_smtp_to(payload.get("smtp_to"))
        if "smtp_port" in payload:
            try:
                update["smtp_port"] = int(payload.get("smtp_port"))
            except Exception:
                update["smtp_port"] = None
        if "smtp_port_2" in payload:
            try:
                update["smtp_port_2"] = int(payload.get("smtp_port_2"))
            except Exception:
                update["smtp_port_2"] = None
        if "smtp_tls" in payload:
            update["smtp_tls"] = bool(payload.get("smtp_tls"))
        if "alert_email_interval_seconds" in payload:
            try:
                v = int(payload.get("alert_email_interval_seconds"))
                if v >= 60:
                    update["alert_email_interval_seconds"] = v
            except Exception:
                pass
        if update:
            try:
                app.state.db["settings"].update_one({"_id": "global"}, {"$set": update}, upsert=True)
            except Exception:
                pass
            _save_settings_to_file(update)
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to save settings"}, status_code=500)


@app.post("/api/nvrs/password-all")
def api_set_all_password(payload: dict):
    try:
        new_pass = payload.get("password")
        if not new_pass:
            return JSONResponse({"status": "error", "message": "Password required"}, status_code=400)
        try:
            app.state.db["nvrs"].update_many({}, {"$set": {"password": new_pass}})
        except Exception:
            pass
        with monitor.lock:
            for i, n in enumerate(monitor.nvrs):
                monitor.nvrs[i]["password"] = new_pass
        monitor._write_back()
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to update passwords"}, status_code=500)


@app.get("/api/diagnose/{ip}")
def api_diagnose(ip: str):
    nvr = None
    for x in monitor.get_snapshot():
        if x.get("ip") == ip:
            nvr = x
            break
    if not nvr:
        return JSONResponse({"status": "error", "message": "NVR not found"}, status_code=404)
    vendor = (nvr.get("type") or "").strip()
    username = nvr.get("username") or "admin"
    password = nvr.get("password") or "admin"
    out = {"ip": ip, "vendor": vendor, "results": [], "camera_count": None, "recording_count": None}
    try:
        if vendor in ("Milesight", "Milesight Old"):
            urls = [
                ("get.system.time", f"http://{ip}/sdk.cgi?action=get.system.time", None),
                ("get.camera.ipclist", f"http://{ip}/sdk.cgi?action=get.camera.ipclist&format=json", None),
                ("get.camera.list", f"http://{ip}/sdk.cgi?action=get.camera.list", None),
                ("get.system.status", f"http://{ip}/sdk.cgi?action=get.system.status", None),
                ("get.status.ipcstatus", f"http://{ip}/sdk.cgi?action=get.status.ipcstatus", None),
            ]
            for name, url, auth in urls:
                try:
                    r = requests.get(url, auth=(username, password), timeout=6)
                    body = r.text[:1000] if r.text else ""
                    parsed = {}
                    if name == "get.camera.ipclist" and r.status_code == 200 and body:
                        ids = monitor._parse_milesight_ipclist_connected_ids(body)
                        if ids is not None:
                            parsed["camera_count"] = len(ids)
                        else:
                            cc = monitor._parse_milesight_camera_ipclist_connected_count(body)
                            if cc is not None:
                                parsed["camera_count"] = cc
                    if name == "get.camera.list" and r.status_code == 200 and body:
                        cc, rc = monitor._parse_milesight_camera_list(body)
                        if cc is not None:
                            parsed["camera_count"] = cc
                        if rc is not None:
                            parsed["recording_count"] = rc
                    if name == "get.system.status" and r.status_code == 200 and body:
                        cc = monitor._parse_milesight_system_status_camera_count(body)
                        if cc is not None:
                            parsed["camera_count"] = cc
                    if name == "get.status.ipcstatus" and r.status_code == 200 and body:
                        rc = monitor._parse_milesight_ipcstatus_recording_count(body)
                        if rc is not None:
                            parsed["recording_count"] = rc
                        ch = monitor._parse_milesight_ipcstatus_channel_count(body)
                        if ch is not None:
                            parsed["camera_count"] = ch
                    out["results"].append({"endpoint": name, "status": r.status_code, "body_sample": body, "parsed": parsed})
                except Exception as e:
                    out["results"].append({"endpoint": name, "error": str(e)})
            for r in out["results"]:
                p = r.get("parsed") or {}
                if out["camera_count"] is None and p.get("camera_count") is not None:
                    out["camera_count"] = p["camera_count"]
                if out["recording_count"] is None and p.get("recording_count") is not None:
                    out["recording_count"] = p["recording_count"]
        elif vendor == "Uniview":
            urls = [
                ("ISAPI/System/time", f"http://{ip}/ISAPI/System/time", HTTPDigestAuth(username, password)),
                ("ISAPI/System/status", f"http://{ip}/ISAPI/System/status", HTTPDigestAuth(username, password)),
                ("ISAPI/Streaming/channels", f"http://{ip}/ISAPI/Streaming/channels", HTTPDigestAuth(username, password)),
                ("ISAPI/System/Video/inputs/channels", f"http://{ip}/ISAPI/System/Video/inputs/channels", HTTPDigestAuth(username, password)),
                ("ISAPI/ContentMgmt/record/tracks", f"http://{ip}/ISAPI/ContentMgmt/record/tracks", HTTPDigestAuth(username, password)),
            ]
            for name, url, auth in urls:
                try:
                    r = requests.get(url, auth=auth, timeout=6)
                    body = r.text[:2000] if r.text else ""
                    parsed = {}
                    if name == "ISAPI/Streaming/channels" and r.status_code == 200 and body:
                        cc = monitor._parse_hikvision_streaming_channels_physical_count(body)
                        if cc is not None:
                            parsed["camera_count"] = cc
                    if name == "ISAPI/System/Video/inputs/channels" and r.status_code == 200 and body:
                        cc = monitor._parse_hikvision_channels_count(body)
                        if cc is not None:
                            parsed["camera_count"] = cc
                    if name == "ISAPI/ContentMgmt/record/tracks" and r.status_code == 200 and body:
                        rc = monitor._parse_hikvision_record_tracks_enabled_channels(body)
                        if rc:
                            parsed["recording_channels"] = list(rc)
                    out["results"].append({"endpoint": name, "status": r.status_code, "body_sample": body[:1000], "parsed": parsed})
                except Exception as e:
                    out["results"].append({"endpoint": name, "error": str(e)})
        else:
            urls = [
                ("ISAPI/Streaming/channels", f"http://{ip}/ISAPI/Streaming/channels", HTTPDigestAuth(username, password)),
                ("ISAPI/System/Video/inputs/channels", f"http://{ip}/ISAPI/System/Video/inputs/channels", HTTPDigestAuth(username, password)),
                ("ISAPI/ContentMgmt/InputProxy/channels", f"http://{ip}/ISAPI/ContentMgmt/InputProxy/channels", HTTPDigestAuth(username, password)),
                ("ISAPI/ContentMgmt/InputProxy/channels/status", f"http://{ip}/ISAPI/ContentMgmt/InputProxy/channels/status", HTTPDigestAuth(username, password)),
                ("ISAPI/ContentMgmt/record/status", f"http://{ip}/ISAPI/ContentMgmt/record/status", HTTPDigestAuth(username, password)),
                ("ISAPI/ContentMgmt/record/tracks", f"http://{ip}/ISAPI/ContentMgmt/record/tracks", HTTPDigestAuth(username, password)),
            ]
            for name, url, auth in urls:
                try:
                    r = requests.get(url, auth=auth, timeout=6)
                    body = r.text[:2000] if r.text else ""
                    parsed = {}
                    if name == "ISAPI/Streaming/channels" and r.status_code == 200 and body:
                        cc = monitor._parse_hikvision_streaming_channels_physical_count(body)
                        if cc is not None:
                            parsed["camera_count"] = cc
                    if name == "ISAPI/System/Video/inputs/channels" and r.status_code == 200 and body:
                        cc = monitor._parse_hikvision_channels_count(body)
                        if cc is not None:
                            parsed["camera_count"] = cc
                    if name == "ISAPI/ContentMgmt/InputProxy/channels" and r.status_code == 200 and body:
                        cc = monitor._parse_hikvision_inputproxy_channels_count(body)
                        if cc is not None:
                            parsed["camera_count"] = cc
                    if name == "ISAPI/ContentMgmt/record/status" and r.status_code == 200 and body:
                        rc = monitor._parse_hikvision_record_status(body)
                        if rc is not None:
                            parsed["recording_count"] = rc
                    if name == "ISAPI/ContentMgmt/record/tracks" and r.status_code == 200 and body:
                        rc = monitor._parse_hikvision_recording_count(body)
                        if rc is not None:
                            parsed["recording_count"] = rc
                    if name == "ISAPI/ContentMgmt/InputProxy/channels/status" and r.status_code == 200 and body:
                        rc = monitor._parse_hikvision_inputproxy_channels_status_recording_count(body)
                        if rc is not None:
                            parsed["recording_count"] = rc
                    out["results"].append({"endpoint": name, "status": r.status_code, "body_sample": body[:1000], "parsed": parsed})
                except Exception as e:
                    out["results"].append({"endpoint": name, "error": str(e)})
            for r in out["results"]:
                p = r.get("parsed") or {}
                if out["camera_count"] is None and p.get("camera_count") is not None:
                    out["camera_count"] = p["camera_count"]
                if out["recording_count"] is None and p.get("recording_count") is not None:
                    out["recording_count"] = p["recording_count"]
        return JSONResponse(out)
    except Exception:
        return JSONResponse({"status": "error", "message": "Diagnosis failed"}, status_code=500)


@app.post("/api/nvrs/{ip}/sync_time")
def api_sync_time(ip: str):
    nvr = None
    for x in monitor.get_snapshot():
        if x.get("ip") == ip:
            nvr = x
            break
    if not nvr:
        return JSONResponse({"status": "error", "message": "NVR not found"}, status_code=404)
    vendor = (nvr.get("type") or "").strip()
    username = nvr.get("username") or "admin"
    password = nvr.get("password") or "admin"
    now = datetime.now().astimezone()

    def hikvision_timezone(ts: datetime) -> str:
        # Hikvision expects CST offset with inverted sign convention.
        offset = ts.utcoffset()
        total_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
        abs_minutes = abs(total_minutes)
        hh = abs_minutes // 60
        mm = abs_minutes % 60
        sign = "-" if total_minutes >= 0 else "+"
        return f"CST{sign}{hh}:{mm:02d}:00"

    def read_milesight_time_value(session: requests.Session, web_auth_ok: bool) -> str | None:
        try:
            if web_auth_ok:
                resp = monitor._milesight_web_get(session, ip, username, password, "/sdk.cgi", "action=get.system.time", timeout=6)
            else:
                resp = requests.get(f"http://{ip}/sdk.cgi?action=get.system.time", auth=(username, password), timeout=6)
            if resp.status_code != 200:
                return None
            return monitor._parse_milesight_time_response(resp.text)
        except Exception:
            return None

    def milesight_lockout_message() -> str | None:
        try:
            pwd_md5 = monitor._milesight_md5(password)
            chk = requests.get(f"http://{ip}/checkUser?user={username}&password={pwd_md5}&type=1", timeout=6)
            if chk.status_code != 200 or not chk.text:
                return None
            blank_time = None
            state_type = None
            for line in chk.text.splitlines():
                line = line.strip()
                if line.startswith("blankTime="):
                    try:
                        blank_time = int(line.split("=", 1)[1].strip())
                    except Exception:
                        blank_time = None
                elif line.startswith("type="):
                    try:
                        state_type = int(line.split("=", 1)[1].strip())
                    except Exception:
                        state_type = None
            if blank_time is not None and blank_time > 0:
                return f"Milesight account temporarily locked ({blank_time}s remaining)"
            if state_type is not None and state_type < 0:
                return "Milesight login pre-check failed"
            return None
        except Exception:
            return None

    def read_uniview_timentp_epoch() -> int | None:
        try:
            resp = requests.get(f"http://{ip}/LAPI/V1.0/System/TimeNTP", auth=HTTPDigestAuth(username, password), timeout=8)
            if resp.status_code != 200:
                return None
            payload = resp.json() if resp.text else {}
            data = payload.get("Response", {}).get("Data", {}) if isinstance(payload, dict) else {}
            val = data.get("DeviceTime") if isinstance(data, dict) else None
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str) and val.strip().isdigit():
                return int(val.strip())
            return None
        except Exception:
            return None

    def uniview_epoch_close_to_target(after_epoch: int, target_epoch: int) -> bool:
        return abs(after_epoch - target_epoch) <= 180

    try:
        ok = False
        verification_error = None
        if vendor in ("Milesight", "Milesight Old"):
            lock_msg = milesight_lockout_message()
            if lock_msg:
                return JSONResponse({"status": "error", "message": lock_msg}, status_code=429)

            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            target_minute = now.strftime("%Y-%m-%d %H:%M")
            session = requests.Session()
            web_auth_ok = monitor._milesight_web_login(session, ip, username, password, timeout=6)
            before_time = read_milesight_time_value(session, web_auth_ok)

            action = f"action=set.system.time&manual_time={requests.utils.quote(ts)}"
            if web_auth_ok:
                r = monitor._milesight_web_get(session, ip, username, password, "/sdk.cgi", action, timeout=6)
            else:
                url = f"http://{ip}/sdk.cgi?{action}"
                r = requests.get(url, auth=(username, password), timeout=6)

            if r.status_code != 200:
                # Some Milesight firmware variants expose SDK under /V1.0.
                alt_url = f"http://{ip}/V1.0/sdk.cgi?{action}"
                r = requests.get(alt_url, auth=(username, password), timeout=6)

            cmd_ok = r.status_code == 200
            after_time = None
            if cmd_ok:
                for _ in range(3):
                    time.sleep(0.5)
                    after_time = read_milesight_time_value(session, web_auth_ok)
                    if after_time:
                        break
                changed = bool(before_time and after_time and before_time != after_time)
                close_to_target = bool(after_time and after_time.startswith(target_minute))
                ok = bool(after_time and (changed or close_to_target))
                if not ok:
                    verification_error = f"Time verification failed (before={before_time}, after={after_time})"
            else:
                if r.status_code == 401:
                    verification_error = "Milesight authentication failed on time endpoint"
                ok = False
        elif vendor == "Hikvision":
            iso = now.isoformat(timespec="seconds")
            tz = hikvision_timezone(now)
            body = f"""
<Time>
  <timeMode>manual</timeMode>
  <localTime>{iso}</localTime>
  <timeZone>{tz}</timeZone>
</Time>
""".strip()
            url = f"http://{ip}/ISAPI/System/time"
            r = requests.put(url, data=body.encode("utf-8"), headers={"Content-Type": "application/xml"}, auth=HTTPDigestAuth(username, password), timeout=8)
            ok = 200 <= r.status_code < 300
        elif vendor == "Uniview":
            # Uniview web UI uses LAPI TimeNTP JSON endpoint for time updates.
            url = f"http://{ip}/LAPI/V1.0/System/TimeNTP"
            sync_mode_url = f"http://{ip}/LAPI/V1.0/Channels/System/Time/Synchronization"
            before_epoch = read_uniview_timentp_epoch()

            # Disable automatic synchronization so manual DeviceTime writes can take effect.
            sm_get = requests.get(sync_mode_url, auth=HTTPDigestAuth(username, password), timeout=8)
            if sm_get.status_code == 200:
                try:
                    sm_data = sm_get.json() if sm_get.text else {}
                    enabled = sm_data.get("Response", {}).get("Data", {}).get("Enabled") if isinstance(sm_data, dict) else None
                    if int(enabled or 0) != 0:
                        requests.put(
                            sync_mode_url,
                            json={"Enabled": 0},
                            headers={"Content-Type": "application/json"},
                            auth=HTTPDigestAuth(username, password),
                            timeout=8,
                        )
                except Exception:
                    pass

            r = requests.get(url, auth=HTTPDigestAuth(username, password), timeout=8)
            if r.status_code == 200:
                data = {}
                try:
                    data = r.json()
                except Exception:
                    data = {}

                resp = data.get("Response") if isinstance(data, dict) else {}
                cur = resp.get("Data") if isinstance(resp, dict) else {}
                if not isinstance(cur, dict):
                    cur = {}

                tz_offsets = [
                    -12, -11, -10, -9, -8, -7, -6, -5, -4.5, -4, -3.5, -3,
                    -2, -1, 0, 1, 2, 3, 3.5, 4, 4.5, 5, 5.5, 5.75, 6, 6.5,
                    7, 8, 9, 9.5, 10, 11, 12, 13,
                ]
                offset = now.utcoffset()
                offset_hours = (offset.total_seconds() / 3600.0) if offset is not None else 0.0
                tz_index = min(range(len(tz_offsets)), key=lambda i: abs(tz_offsets[i] - offset_hours))

                ntp = cur.get("NTPServerInfo") if isinstance(cur.get("NTPServerInfo"), dict) else {}
                offset = now.utcoffset()
                offset_seconds = int(offset.total_seconds()) if offset is not None else 0
                payload = {
                    "TimeZone": int(tz_index),
                    # This firmware expects local-epoch style seconds (epoch + timezone offset).
                    "DeviceTime": int(now.timestamp()) + offset_seconds,
                    "DateFormat": int(cur.get("DateFormat", 0) or 0),
                    "HourFormat": int(cur.get("HourFormat", 0) or 0),
                    "NTPServerInfo": {
                        "Enabled": int(ntp.get("Enabled", 0) or 0),
                        "AddressType": int(ntp.get("AddressType", 0) or 0),
                        "Address": ntp.get("Address", "") or "",
                        "Port": int(ntp.get("Port", 123) or 123),
                        "SynchronizeInterval": int(ntp.get("SynchronizeInterval", 60) or 60),
                    },
                }

                r = requests.put(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    auth=HTTPDigestAuth(username, password),
                    timeout=8,
                )
                if 200 <= r.status_code < 300:
                    command_ok = False
                    try:
                        rb = r.json()
                        status_code = rb.get("Response", {}).get("StatusCode") if isinstance(rb, dict) else None
                        command_ok = (status_code == 0 or status_code == "0" or status_code is None)
                    except Exception:
                        command_ok = True

                    if command_ok:
                        after_epoch = None
                        for _ in range(3):
                            time.sleep(0.5)
                            after_epoch = read_uniview_timentp_epoch()
                            if after_epoch is not None:
                                break
                        target_epoch = int(now.timestamp())
                        changed = bool(
                            before_epoch is not None
                            and after_epoch is not None
                            and abs(after_epoch - before_epoch) >= 30
                        )
                        close_to_target = bool(
                            after_epoch is not None
                            and uniview_epoch_close_to_target(after_epoch, target_epoch)
                        )
                        ok = bool(after_epoch is not None and (changed or close_to_target))
                        if not ok:
                            verification_error = f"Time verification failed (before={before_epoch}, after={after_epoch}, target={target_epoch})"
                    else:
                        ok = False
        else:
            return JSONResponse({"status": "error", "message": "Unsupported vendor for sync"}, status_code=400)
        if ok:
            monitor.refresh_once()
            return {"status": "ok"}
        detail = verification_error or f"Device responded {r.status_code}"
        return JSONResponse({"status": "error", "message": detail}, status_code=502)
    except Exception:
        return JSONResponse({"status": "error", "message": "Sync request failed"}, status_code=500)


@app.post("/api/nvrs/sync_time_all")
def api_sync_time_all():
    results = []
    for nvr in monitor.get_snapshot():
        ip = nvr.get("ip")
        if not ip:
            continue
        try:
            res = api_sync_time(ip)
            if isinstance(res, dict):
                results.append({"ip": ip, "status": "ok"})
            else:
                payload = getattr(res, "body", None)
                results.append({"ip": ip, "status": "error"})
        except Exception:
            results.append({"ip": ip, "status": "error"})
    return {"status": "ok", "results": results}


@app.get("/api/events")
def api_events(request: Request):
    try:
        q = request.query_params
        try:
            from_ts = int(q.get("from")) if q.get("from") is not None else None
        except Exception:
            from_ts = None
        try:
            to_ts = int(q.get("to")) if q.get("to") is not None else None
        except Exception:
            to_ts = None
        data = monitor.get_events(from_ts, to_ts)
        return JSONResponse(data)
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to load events"}, status_code=500)


@app.get("/api/logs")
def api_logs():
    try:
        db = app.state.db
        active = list(
            db["alerts"].find(
                {"status": "active"},
                {"_id": 1, "alert_type": 1, "severity": 1, "nvr_name": 1, "nvr_ip": 1, "channel": 1, "message": 1, "first_seen": 1, "last_seen": 1, "acknowledged": 1, "acknowledged_at": 1},
            ).sort([("severity", 1), ("first_seen", -1)])
        )
        recent_ack = list(
            db["alert_ack_events"].find(
                {},
                {"_id": 0, "alert_id": 1, "severity": 1, "nvr_name": 1, "nvr_ip": 1, "message": 1, "acknowledged_at": 1},
            ).sort([("acknowledged_at", -1)]).limit(200)
        )
        emails = list(
            db["email_events"].find(
                {},
                {"_id": 0, "created_at": 1, "subject": 1, "to": 1, "count": 1, "success": 1, "error": 1},
            ).sort([("created_at", -1)]).limit(200)
        )
        critical_count = sum(1 for a in active if a.get("severity") == "critical")
        warning_count = sum(1 for a in active if a.get("severity") == "warning")
        non_critical_count = sum(1 for a in active if a.get("severity") == "non-critical")
        return {
            "status": "ok",
            "active_alerts": active,
            "ack_history": recent_ack,
            "email_history": emails,
            "counts": {
                "total": len(active),
                "critical": critical_count,
                "warning": warning_count,
                "non_critical": non_critical_count,
            },
        }
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to load logs"}, status_code=500)


@app.post("/api/logs/ack")
def api_logs_ack(payload: dict):
    try:
        alert_id = (payload or {}).get("alert_id")
        if not alert_id:
            return JSONResponse({"status": "error", "message": "alert_id is required"}, status_code=400)
        now_ts = int(time.time())
        db = app.state.db
        found = db["alerts"].find_one({"_id": alert_id, "status": "active"})
        if not found:
            return JSONResponse({"status": "error", "message": "Active alert not found"}, status_code=404)
        db["alerts"].update_one(
            {"_id": alert_id},
            {"$set": {"acknowledged": True, "acknowledged_at": now_ts}},
        )
        db["alert_ack_events"].insert_one(
            {
                "alert_id": alert_id,
                "severity": found.get("severity"),
                "nvr_name": found.get("nvr_name"),
                "nvr_ip": found.get("nvr_ip"),
                "message": found.get("message"),
                "acknowledged_at": now_ts,
            }
        )
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to acknowledge alert"}, status_code=500)


@app.post("/api/logs/ack_all")
def api_logs_ack_all(payload: dict):
    try:
        severity = (payload or {}).get("severity")
        now_ts = int(time.time())
        q = {"status": "active", "acknowledged": {"$ne": True}}
        if severity in {"critical", "warning", "non-critical"}:
            q["severity"] = severity
        db = app.state.db
        to_ack = list(db["alerts"].find(q, {"_id": 1, "severity": 1, "nvr_name": 1, "nvr_ip": 1, "message": 1}))
        if not to_ack:
            return {"status": "ok", "count": 0}
        ids = [x.get("_id") for x in to_ack if x.get("_id")]
        db["alerts"].update_many(
            {"_id": {"$in": ids}},
            {"$set": {"acknowledged": True, "acknowledged_at": now_ts}},
        )
        if ids:
            rows = []
            for d in to_ack:
                rows.append(
                    {
                        "alert_id": d.get("_id"),
                        "severity": d.get("severity"),
                        "nvr_name": d.get("nvr_name"),
                        "nvr_ip": d.get("nvr_ip"),
                        "message": d.get("message"),
                        "acknowledged_at": now_ts,
                    }
                )
            db["alert_ack_events"].insert_many(rows)
        return {"status": "ok", "count": len(ids)}
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to acknowledge alerts"}, status_code=500)


@app.post("/api/smtp/test")
def api_smtp_test(payload: dict):
    to_list = []
    try:
        payload = payload or {}
        try:
            db_s = app.state.db["settings"].find_one({"_id": "global"}) or {}
        except Exception:
            db_s = {}
        file_s = _load_settings_from_file()
        s = dict(file_s)
        if isinstance(db_s, dict):
            s.update(db_s)

        to_all_saved = bool(payload.get("to_all_saved"))
        to_list = []
        if to_all_saved:
            to_list = _normalize_smtp_to(s.get("smtp_to"))
            if not to_list:
                return JSONResponse({"status": "error", "message": "No saved recipients found in settings"}, status_code=400)
        else:
            if isinstance(payload.get("to"), list):
                to_list = _normalize_smtp_to(payload.get("to"))
            else:
                to_addr = (payload.get("to") or "").strip()
                to_list = _normalize_smtp_to([to_addr] if to_addr else [])
            if not to_list:
                return JSONResponse({"status": "error", "message": "Recipient 'to' is required"}, status_code=400)

        ok, err, smtp_used = _send_alert_email(
            s,
            to_list,
            "Cams WebApp SMTP Test",
            "This is a test email from Cams WebApp.",
        )
        if not ok:
            return JSONResponse({"status": "error", "message": err or "SMTP send failed"}, status_code=500)
        sent = list(to_list)
        try:
            app.state.db["email_events"].insert_one(
                {
                    "created_at": int(time.time()),
                    "subject": "Cams WebApp SMTP Test",
                    "to": sent,
                    "alert_ids": [],
                    "count": len(sent),
                    "success": True,
                    "error": None,
                    "email_type": "test",
                    "smtp_used": smtp_used,
                }
            )
        except Exception:
            pass
        return {"status": "ok", "sent": sent, "count": len(sent)}
    except Exception as e:
        try:
            app.state.db["email_events"].insert_one(
                {
                    "created_at": int(time.time()),
                    "subject": "Cams WebApp SMTP Test",
                    "to": to_list,
                    "alert_ids": [],
                    "count": len(to_list),
                    "success": False,
                    "error": str(e),
                    "email_type": "test",
                }
            )
        except Exception:
            pass
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
