import os
import json
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
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

monitor = MonitorState(poll_interval=60)

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


@app.on_event("shutdown")
def shutdown_event():
    monitor.stop()
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
        s = app.state.db["settings"].find_one({"_id": "global"}) or {}
        if "_id" in s:
            s.pop("_id", None)
        # include current runtime interval
        s["refresh_interval"] = int(s.get("refresh_interval") or monitor.poll_interval or 60)
        return JSONResponse(s)
    except Exception:
        return JSONResponse({
            "refresh_interval": monitor.poll_interval,
            "smtp_host": None,
            "smtp_port": None,
            "smtp_username": None,
            "smtp_password": None,
            "smtp_tls": False,
            "smtp_from": None,
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
        for key in ("smtp_host", "smtp_username", "smtp_password", "smtp_from"):
            if key in payload:
                val = payload.get(key)
                update[key] = val if val is not None else None
        if "smtp_port" in payload:
            try:
                update["smtp_port"] = int(payload.get("smtp_port"))
            except Exception:
                update["smtp_port"] = None
        if "smtp_tls" in payload:
            update["smtp_tls"] = bool(payload.get("smtp_tls"))
        if update:
            app.state.db["settings"].update_one({"_id": "global"}, {"$set": update}, upsert=True)
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

    try:
        if vendor in ("Milesight", "Milesight Old"):
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            url = f"http://{ip}/sdk.cgi?action=set.system.time&manual_time={requests.utils.quote(ts)}"
            r = requests.get(url, auth=(username, password), timeout=6)
            ok = r.status_code == 200
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
            iso = now.isoformat(timespec="seconds")
            url = f"http://{ip}/ISAPI/System/time"
            payloads = [
                f"<Time><localTime>{iso}</localTime></Time>",
                f"<Time><timeMode>manual</timeMode><localTime>{iso}</localTime><timeZone>{hikvision_timezone(now)}</timeZone></Time>",
            ]
            r = None
            ok = False
            for body in payloads:
                r = requests.put(
                    url,
                    data=body.encode("utf-8"),
                    headers={"Content-Type": "application/xml"},
                    auth=HTTPDigestAuth(username, password),
                    timeout=8,
                )
                if 200 <= r.status_code < 300:
                    ok = True
                    break
        else:
            return JSONResponse({"status": "error", "message": "Unsupported vendor for sync"}, status_code=400)
        if ok:
            monitor.refresh_once()
            return {"status": "ok"}
        return JSONResponse({"status": "error", "message": f"Device responded {r.status_code}"}, status_code=502)
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


@app.post("/api/smtp/test")
def api_smtp_test(payload: dict):
    try:
        to_addr = (payload.get("to") or "").strip()
        if not to_addr:
            return JSONResponse({"status": "error", "message": "Recipient 'to' is required"}, status_code=400)
        s = app.state.db["settings"].find_one({"_id": "global"}) or {}
        host = s.get("smtp_host") or None
        port = int(s.get("smtp_port") or 0)
        username = s.get("smtp_username") or None
        password = s.get("smtp_password") or None
        use_tls = bool(s.get("smtp_tls") or False)
        from_addr = s.get("smtp_from") or None
        if not host or not port or not from_addr:
            return JSONResponse({"status": "error", "message": "SMTP host, port, and from must be set"}, status_code=400)
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = "Cams WebApp SMTP Test"
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.set_content("This is a test email from Cams WebApp.")
        if use_tls:
            client = smtplib.SMTP(host, port, timeout=10)
            client.starttls()
        else:
            client = smtplib.SMTP(host, port, timeout=10)
        try:
            if username and password:
                client.login(username, password)
            client.send_message(msg)
        finally:
            try:
                client.quit()
            except Exception:
                pass
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
