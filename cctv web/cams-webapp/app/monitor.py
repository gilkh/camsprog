import json
try:
    import orjson
    _HAS_ORJSON = True
except Exception:
    _HAS_ORJSON = False
import os
import threading
import time
import subprocess
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Any
import requests
from requests.auth import HTTPDigestAuth

# Use project root config.json (two levels up from cams-webapp/app)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
STATE_PATH = os.path.join(PROJECT_ROOT, "state.json")
EVENTS_PATH = os.path.join(PROJECT_ROOT, "events.json")


def load_nvrs_from_config() -> List[Dict[str, Any]]:
    """Load NVRs from config.json. Prefer top-level 'nvrs' if present; fallback to 'config.nvrs'."""
    if not os.path.exists(CONFIG_PATH):
        return []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    if isinstance(data, dict):
        if "nvrs" in data and isinstance(data["nvrs"], list):
            return data["nvrs"]
        cfg = data.get("config", {})
        if isinstance(cfg, dict) and isinstance(cfg.get("nvrs"), list):
            return cfg["nvrs"]
    return []


def ping_ip(ip: str, timeout_ms: int = 1000) -> bool:
    """Ping an IP once using Windows ping. Returns True if online."""
    try:
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), ip],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
        )
        return result.returncode == 0
    except Exception:
        return False


class MonitorState:
    def __init__(self, poll_interval: int = 60, db=None):
        self.poll_interval = poll_interval
        self.lock = threading.Lock()
        self.running = False
        self.thread: threading.Thread | None = None
        self.nvrs: List[Dict[str, Any]] = []
        self.db = db

    def load(self):
        if self.db is not None:
            try:
                docs = list(self.db["nvrs"].find({}, {"_id": 0}))
                if not docs:
                    docs = load_nvrs_from_config() or []
                    if docs:
                        fields_to_keep = {"name", "ip", "type", "username", "password", "status", "last_online", "offline_since", "date_time_status", "nvr_time", "camera_count", "recording_count"}
                        cleaned = []
                        for n in docs:
                            cleaned.append({k: n.get(k) for k in fields_to_keep})
                        if cleaned:
                            self.db["nvrs"].insert_many(cleaned)
                self.nvrs = docs or []
            except Exception:
                self.nvrs = load_nvrs_from_config() or []
        else:
            self.nvrs = load_nvrs_from_config() or []
        for nvr in self.nvrs:
            nvr.setdefault("status", "Unknown")
            nvr.setdefault("last_online", None)
            nvr.setdefault("offline_since", None)
            nvr.setdefault("camera_count", "Unknown")
            nvr.setdefault("recording_count", "Unknown")
            nvr.setdefault("recording_expected", None)

    def get_snapshot(self) -> List[Dict[str, Any]]:
        with self.lock:
            # Return a shallow copy safe for JSON
            return [dict(nvr) for nvr in self.nvrs]

    def start(self):
        if self.running:
            return
        self.running = True
        self.load()
        self.thread = threading.Thread(target=self._loop, name="monitor-loop", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1)

    def refresh_once(self):
        """Run one refresh cycle synchronously."""
        self._refresh()

    def _loop(self):
        while self.running:
            try:
                self._refresh()
            except Exception:
                # Keep loop alive
                pass
            time.sleep(self.poll_interval)

    def _refresh(self):
        now_ts = time.time()
        with self.lock:
            if not self.nvrs:
                self.load()
            for nvr in self.nvrs:
                ip = nvr.get("ip")
                if not ip:
                    continue
                online = ping_ip(ip)
                if online:
                    if nvr.get("status") == "Offline":
                        # Recovery: finalize offline interval
                        off_start = nvr.get("offline_since")
                        if off_start:
                            self._record_offline_interval(ip, off_start, now_ts)
                        nvr["offline_since"] = None
                    nvr["status"] = "Online"
                    nvr["last_online"] = now_ts
                    # Update vendor-specific stats
                    self._update_vendor_stats(nvr)
                else:
                    # Mark offline and set offline_since if first time
                    if nvr.get("status") != "Offline":
                        nvr["offline_since"] = now_ts
                    nvr["status"] = "Offline"
                    nvr["nvr_time"] = "Offline"
                    nvr["camera_count"] = "Offline"
                    nvr["recording_count"] = "Offline"
        # Persist fast to separate state file (avoid rewriting config.json each refresh)
        self._write_state()

    def _write_back(self):
        try:
            if self.db is not None:
                fields_to_keep = {"name", "ip", "type", "username", "password", "status", "last_online", "offline_since", "date_time_status", "recording_expected"}
                for n in self.nvrs:
                    cleaned = {k: n.get(k) for k in fields_to_keep}
                    ip = cleaned.get("ip")
                    if ip:
                        self.db["nvrs"].update_one({"ip": ip}, {"$set": cleaned}, upsert=True)
                return
            data = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            fields_to_keep = {"name", "ip", "type", "username", "password", "status", "last_online", "offline_since", "date_time_status", "recording_expected"}
            cleaned_nvrs = []
            for n in self.nvrs:
                cleaned = {k: n.get(k) for k in fields_to_keep}
                cleaned_nvrs.append(cleaned)
            data["nvrs"] = cleaned_nvrs
            tmp_path = CONFIG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, CONFIG_PATH)
        except Exception:
            pass

    def _record_offline_interval(self, ip: str, start_ts: float, end_ts: float) -> None:
        try:
            if self.db is not None:
                self.db["nvr_events"].insert_one({
                    "ip": ip,
                    "type": "offline",
                    "start": int(start_ts),
                    "end": int(end_ts),
                })
                return
            events = {}
            if os.path.exists(EVENTS_PATH):
                try:
                    with open(EVENTS_PATH, "r", encoding="utf-8") as f:
                        events = json.load(f)
                except Exception:
                    events = {}
            if not isinstance(events, dict):
                events = {}
            arr = events.get(ip)
            if not isinstance(arr, list):
                arr = []
            arr.append({"type": "offline", "start": int(start_ts), "end": int(end_ts)})
            events[ip] = arr
            tmp = EVENTS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, EVENTS_PATH)
        except Exception:
            pass

    def get_events(self, from_ts: int | None, to_ts: int | None) -> Dict[str, List[Dict[str, Any]]]:
        now = int(time.time())
        if to_ts is None:
            to_ts = now
        if from_ts is None:
            from_ts = to_ts - 14 * 24 * 3600
        out: Dict[str, List[Dict[str, Any]]] = {}
        try:
            if self.db is not None:
                cur = self.db["nvr_events"].find({
                    "type": "offline",
                    "$or": [
                        {"start": {"$lte": to_ts}, "end": {"$gte": from_ts}},
                        {"start": {"$gte": from_ts, "$lte": to_ts}},
                    ]
                }, {"_id": 0})
                for doc in cur:
                    ip = doc.get("ip")
                    if not ip:
                        continue
                    arr = out.get(ip)
                    if not isinstance(arr, list):
                        arr = []
                    arr.append({"type": "offline", "start": int(doc.get("start", 0)), "end": int(doc.get("end", to_ts))})
                    out[ip] = arr
            else:
                if os.path.exists(EVENTS_PATH):
                    try:
                        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        data = {}
                    if isinstance(data, dict):
                        for ip, arr in data.items():
                            if not isinstance(arr, list):
                                continue
                            for ev in arr:
                                if ev.get("type") != "offline":
                                    continue
                                s = int(ev.get("start", 0))
                                e = int(ev.get("end", to_ts))
                                if s <= to_ts and e >= from_ts:
                                    lst = out.get(ip)
                                    if not isinstance(lst, list):
                                        lst = []
                                    lst.append({"type": "offline", "start": s, "end": e})
                                    out[ip] = lst
            # include currently offline intervals as open-ended
            with self.lock:
                for nvr in self.nvrs:
                    ip = nvr.get("ip")
                    if not ip:
                        continue
                    if nvr.get("status") == "Offline" and nvr.get("offline_since"):
                        s = int(nvr.get("offline_since") or now)
                        e = now
                        if s <= to_ts and e >= from_ts:
                            arr = out.get(ip)
                            if not isinstance(arr, list):
                                arr = []
                            arr.append({"type": "offline", "start": s, "end": e})
                            out[ip] = arr
        except Exception:
            pass
        return out

    def _write_state(self):
        try:
            if self.db is not None:
                snapshot = self.get_snapshot()
                for n in snapshot:
                    ip = n.get("ip")
                    if not ip:
                        continue
                    update = {
                        "status": n.get("status"),
                        "last_online": n.get("last_online"),
                        "offline_since": n.get("offline_since"),
                        "nvr_time": n.get("nvr_time"),
                        "camera_count": n.get("camera_count"),
                        "recording_count": n.get("recording_count"),
                        "recording_motion_config_count": n.get("recording_motion_config_count"),
                    }
                    self.db["nvrs"].update_one({"ip": ip}, {"$set": update}, upsert=True)
                return
            snapshot = self.get_snapshot()
            payload = {"nvrs": []}
            for n in snapshot:
                payload["nvrs"].append({
                    "ip": n.get("ip"),
                    "name": n.get("name"),
                    "status": n.get("status"),
                    "last_online": n.get("last_online"),
                    "offline_since": n.get("offline_since"),
                    "nvr_time": n.get("nvr_time"),
                    "camera_count": n.get("camera_count"),
                    "recording_count": n.get("recording_count"),
                    "recording_motion_config_count": n.get("recording_motion_config_count"),
                })
            tmp_path = STATE_PATH + ".tmp"
            if _HAS_ORJSON:
                with open(tmp_path, "wb") as f:
                    f.write(orjson.dumps(payload))
            else:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, STATE_PATH)
        except Exception:
            pass

    # --- Persistence for add/update operations ---
    def add_or_update_nvr(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Add a new NVR or update existing by IP, then persist to config.json."""
        required = ["name", "ip"]
        for key in required:
            if not data.get(key):
                raise ValueError(f"Missing required field: {key}")
        nvr_type = data.get("type") or data.get("vendor")
        if nvr_type:
            if nvr_type.lower() in ("milesight", "mileSight"):
                nvr_type = "Milesight"
            elif nvr_type.lower() in ("milesight old", "milesight_old", "old milesight"):
                nvr_type = "Milesight Old"
            elif nvr_type.lower() in ("hikvision", "hickvision", "hik"):
                nvr_type = "Hikvision"
            elif nvr_type.lower() in ("uniview", "unv"):
                nvr_type = "Uniview"
        with self.lock:
            # Normalize defaults
            new_nvr = {
                "name": data.get("name"),
                "ip": data.get("ip"),
                "type": nvr_type,
                "username": data.get("username") or "",
                "password": data.get("password") or "",
                "status": "Unknown",
                "last_online": None,
                "offline_since": None,
                "camera_count": "Unknown",
                "recording_count": "Unknown",
                "nvr_time": "Not checked",
            }
            # Update if exists, else append
            replaced = False
            for i, existing in enumerate(self.nvrs):
                if existing.get("ip") == new_nvr["ip"]:
                    self.nvrs[i] = {**existing, **new_nvr}
                    replaced = True
                    break
            if not replaced:
                self.nvrs.append(new_nvr)
        # Persist
        self._write_back()
        return new_nvr

    def delete_nvr(self, ip: str) -> bool:
        """Delete NVR by IP and persist changes."""
        if not ip:
            return False
        changed = False
        with self.lock:
            before = len(self.nvrs)
            self.nvrs = [n for n in self.nvrs if n.get("ip") != ip]
            changed = len(self.nvrs) != before
        if changed:
            if self.db is not None:
                try:
                    self.db["nvrs"].delete_one({"ip": ip})
                except Exception:
                    pass
            self._write_back()
        return changed

    # --- Vendor-specific stats ---
    def _update_vendor_stats(self, nvr: Dict[str, Any]) -> None:
        ip = nvr.get("ip")
        vendor = (nvr.get("type") or "").strip()
        username = nvr.get("username") or "admin"
        password = nvr.get("password") or "admin"
        if not ip:
            return
        try:
            if vendor == "Milesight":
                # Time (robust: JSON or key=value formats)
                time_url = f"http://{ip}/sdk.cgi?action=get.system.time"
                r = requests.get(time_url, auth=(username, password), timeout=5)
                if r.status_code == 200:
                    time_str = self._parse_milesight_time_response(r.text)
                    nvr["nvr_time"] = time_str or "Unknown"
                else:
                    nvr["nvr_time"] = f"Time failed: {r.status_code}"

                # Cameras (prefer ipclist JSON) and build connected set
                connected_ids: set[int] | None = None
                ipclist_url = f"http://{ip}/sdk.cgi?action=get.camera.ipclist&format=json"
                rc = requests.get(ipclist_url, auth=(username, password), timeout=5)
                if rc.status_code == 200 and rc.text:
                    ids = self._parse_milesight_ipclist_connected_ids(rc.text)
                    if ids is not None:
                        connected_ids = ids
                        nvr["camera_count"] = len(ids)
                    else:
                        cnt = self._parse_milesight_camera_ipclist_connected_count(rc.text)
                        if cnt is not None:
                            nvr["camera_count"] = cnt
                else:
                    # Fallback to legacy camera list parsing
                    cam_url = f"http://{ip}/sdk.cgi?action=get.camera.list"
                    rcl = requests.get(cam_url, auth=(username, password), timeout=5)
                    if rcl.status_code == 200 and rcl.text:
                        cam_count, rec_cfg_count = self._parse_milesight_camera_list(rcl.text)
                        if cam_count is not None:
                            nvr["camera_count"] = cam_count
                        # derive configured-to-record indices and intersect with connected
                        cfg_indices = self._parse_milesight_record_config_indices(rcl.text)
                        # recording counting removed

                # Fallback to system status for camera count (multiple patterns)
                ss_url = f"http://{ip}/sdk.cgi?action=get.system.status"
                rs = requests.get(ss_url, auth=(username, password), timeout=5)
                if rs.status_code == 200 and rs.text:
                    cam_count = self._parse_milesight_system_status_camera_count(rs.text)
                    if cam_count is not None:
                        nvr["camera_count"] = cam_count

                # Recording count from ipcstatus (intersect with connected cameras)
                ipc_url = f"http://{ip}/sdk.cgi?action=get.status.ipcstatus"
                try:
                    ir = requests.get(ipc_url, auth=(username, password), timeout=5)
                    if ir.status_code == 200 and ir.text:
                        ipc_conn, ipc_rec = self._parse_milesight_ipcstatus_details(ir.text)
                        if connected_ids is not None and ipc_rec:
                            nvr["recording_count"] = len(connected_ids & ipc_rec)
                        elif ipc_conn and ipc_rec:
                            nvr["recording_count"] = len(ipc_conn & ipc_rec)
                        elif ipc_rec:
                            nvr["recording_count"] = len(ipc_rec)
                except Exception:
                    pass

            elif vendor == "Milesight Old":
                time_url = f"http://{ip}/sdk.cgi?action=get.system.time"
                r = requests.get(time_url, auth=(username, password), timeout=6)
                if r.status_code == 200 and r.text:
                    time_str = self._parse_milesight_time_response(r.text)
                    nvr["nvr_time"] = time_str or "Unknown"
                else:
                    nvr["nvr_time"] = f"Time failed: {r.status_code}"
                ipc_url = f"http://{ip}/sdk.cgi?action=get.status.ipcstatus"
                ir = requests.get(ipc_url, auth=(username, password), timeout=6)
                if ir.status_code == 200 and ir.text:
                    ipc_conn, ipc_rec = self._parse_milesight_ipcstatus_details(ir.text)
                    if ipc_conn:
                        nvr["camera_count"] = len(ipc_conn)
                    else:
                        cc = self._parse_milesight_ipcstatus_channel_count(ir.text)
                        if cc is not None:
                            nvr["camera_count"] = cc
                    connected = ipc_conn
                    if not connected:
                        cam_count = nvr.get("camera_count")
                        if isinstance(cam_count, int):
                            connected = set(range(cam_count))
                    if connected and ipc_rec:
                        nvr["recording_count"] = len(connected & ipc_rec)
                    elif ipc_rec:
                        nvr["recording_count"] = len(ipc_rec)

            elif vendor == "Hikvision":
                # Time (namespace-agnostic)
                time_url = f"http://{ip}/ISAPI/System/time"
                r = requests.get(time_url, auth=HTTPDigestAuth(username, password), timeout=5)
                if r.status_code == 200:
                    try:
                        root = ET.fromstring(r.text)
                        time_str = None
                        for elem in root.iter():
                            tag = elem.tag
                            if isinstance(tag, str) and (tag.endswith("localTime") or tag.endswith("time")):
                                if elem.text and elem.text.strip():
                                    time_str = elem.text.strip()
                                    break
                        nvr["nvr_time"] = time_str or "Unknown"
                    except Exception:
                        nvr["nvr_time"] = "Parse error"
                else:
                    # Fallback: device status currentDeviceTime
                    status_url = f"http://{ip}/ISAPI/System/status"
                    rs = requests.get(status_url, auth=HTTPDigestAuth(username, password), timeout=5)
                    if rs.status_code == 200:
                        try:
                            root = ET.fromstring(rs.text)
                            time_str = None
                            for elem in root.iter():
                                tag = elem.tag
                                if isinstance(tag, str) and tag.endswith("currentDeviceTime"):
                                    if elem.text and elem.text.strip():
                                        time_str = elem.text.strip()
                                        break
                            nvr["nvr_time"] = time_str or f"Time failed: {rs.status_code}"
                        except Exception:
                            nvr["nvr_time"] = f"Time failed: {rs.status_code}"
                    else:
                        nvr["nvr_time"] = f"Time failed: {rs.status_code}"

                # --- Camera count: try inputs/channels, then InputProxy/channels/status, then Streaming ---
                connected_ids = None
                ch_url = f"http://{ip}/ISAPI/System/Video/inputs/channels"
                rc = requests.get(ch_url, auth=HTTPDigestAuth(username, password), timeout=5)
                if rc.status_code == 200 and rc.text:
                    connected_ids = self._parse_hikvision_inputs_connected_ids(rc.text)
                    if connected_ids:
                        nvr["camera_count"] = len(connected_ids)
                if not connected_ids:
                    # Fallback: InputProxy/channels/status (many Hikvision NVRs use this)
                    proxy_url = f"http://{ip}/ISAPI/ContentMgmt/InputProxy/channels/status"
                    ps = requests.get(proxy_url, auth=HTTPDigestAuth(username, password), timeout=5)
                    if ps.status_code == 200 and ps.text:
                        connected_ids = self._parse_hikvision_inputproxy_channels_status_connected_ids(ps.text)
                        if connected_ids:
                            # IDs from InputProxy are string IDs like '1','2'; convert to int set
                            int_ids = set()
                            for cid in connected_ids:
                                try:
                                    int_ids.add(int(cid))
                                except Exception:
                                    pass
                            connected_ids = int_ids if int_ids else connected_ids
                            nvr["camera_count"] = len(connected_ids)
                if not connected_ids:
                    stream_url = f"http://{ip}/ISAPI/Streaming/channels"
                    sc = requests.get(stream_url, auth=HTTPDigestAuth(username, password), timeout=5)
                    if sc.status_code == 200 and sc.text:
                        count = self._parse_hikvision_streaming_channels_physical_count(sc.text)
                        if count is not None:
                            nvr["camera_count"] = count

                # --- Recording config: check DefaultRecordingMode per track ---
                tracks_url = f"http://{ip}/ISAPI/ContentMgmt/record/tracks"
                tr = requests.get(tracks_url, auth=HTTPDigestAuth(username, password), timeout=5)
                if tr.status_code == 200 and tr.text:
                    rec_configured = self._parse_hikvision_record_tracks_configured_channels(tr.text)
                    if connected_ids and rec_configured:
                        nvr["recording_count"] = len(connected_ids & rec_configured)
                    elif rec_configured:
                        nvr["recording_count"] = len(rec_configured)
            elif vendor == "Uniview":
                time_url = f"http://{ip}/ISAPI/System/time"
                r = requests.get(time_url, auth=HTTPDigestAuth(username, password), timeout=6)
                if r.status_code == 200 and r.text:
                    try:
                        root = ET.fromstring(r.text)
                        time_str = None
                        for elem in root.iter():
                            tag = elem.tag
                            if not isinstance(tag, str):
                                continue
                            lt = tag.lower()
                            if lt.endswith("localtime") or lt.endswith("time") or lt.endswith("devicetime") or lt.endswith("currentdevicetime"):
                                if elem.text and elem.text.strip():
                                    time_str = elem.text.strip()
                                    break
                        nvr["nvr_time"] = time_str or "Unknown"
                    except Exception:
                        nvr["nvr_time"] = "Parse error"
                else:
                    status_url = f"http://{ip}/ISAPI/System/status"
                    rs = requests.get(status_url, auth=HTTPDigestAuth(username, password), timeout=6)
                    if rs.status_code == 200 and rs.text:
                        try:
                            root = ET.fromstring(rs.text)
                            time_str = None
                            for elem in root.iter():
                                tag = elem.tag
                                if not isinstance(tag, str):
                                    continue
                                lt = tag.lower()
                                if lt.endswith("currentdevicetime") or lt.endswith("devicetime") or lt.endswith("localtime"):
                                    if elem.text and elem.text.strip():
                                        time_str = elem.text.strip()
                                        break
                            nvr["nvr_time"] = time_str or f"Time failed: {r.status_code}"
                        except Exception:
                            nvr["nvr_time"] = f"Time failed: {r.status_code}"
                    else:
                        nvr["nvr_time"] = f"Time failed: {r.status_code}"

                # Uniview camera count via LAPI
                cam_count_val = None
                lapi_url = f"http://{ip}/LAPI/V1.0/Channels/System/ChannelDetailInfos"
                try:
                    lr = requests.get(lapi_url, auth=HTTPDigestAuth(username, password), timeout=6)
                except Exception:
                    lr = None
                if lr and lr.status_code == 200 and lr.text:
                    cc = self._parse_uniview_channel_detail_infos_camera_count(lr.text)
                    if cc is not None:
                        cam_count_val = cc
                if cam_count_val is None:
                    # Fallback to ISAPI
                    stream_url = f"http://{ip}/ISAPI/Streaming/channels"
                    sc = requests.get(stream_url, auth=HTTPDigestAuth(username, password), timeout=5)
                    if sc.status_code == 200 and sc.text:
                        cam_count_val = self._parse_hikvision_streaming_channels_physical_count(sc.text)
                if cam_count_val is None:
                    ch_url = f"http://{ip}/ISAPI/System/Video/inputs/channels"
                    rc = requests.get(ch_url, auth=HTTPDigestAuth(username, password), timeout=5)
                    if rc.status_code == 200 and rc.text:
                        cam_count_val = self._parse_hikvision_channels_count(rc.text)
                if cam_count_val is not None:
                    nvr["camera_count"] = cam_count_val

                # recording counting removed
        except Exception:
            # Keep refresh robust; do not crash loop
            pass

    def _parse_milesight_camera_list(self, text: str):
        """Return (camera_count, recording_count) from Milesight camera list. Supports JSON and key=value."""
        # Try JSON first
        camera_count = None
        recording_count = None
        try:
            data = json.loads(text)
            # Common patterns: {"cameras": [...]}, {"camera": [...]}, or {"channels": [...]}
            for key in ("cameras", "camera", "channels"):
                arr = data.get(key)
                if isinstance(arr, list) and arr:
                    camera_count = len(arr)
                    # Count recording flags if present
                    rec = 0
                    for item in arr:
                        if not isinstance(item, dict):
                            continue
                        vals = {k.lower(): str(v).strip().lower() for k, v in item.items()}
                        if any(vals.get(k, "") in {"1", "true", "on", "enabled", "enable", "start"} for k in ("record", "recording", "record_enable", "recordenabled", "recordstatus")):
                            rec += 1
                    recording_count = rec if rec > 0 else recording_count
                    break
        except Exception:
            pass

        if camera_count is not None and recording_count is not None:
            return camera_count, recording_count

        # Fallback: key=value text
        try:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            indices = set()
            recording_indices = set()
            online_indices = set()

            bracket_re = re.compile(r"([a-zA-Z]+)\[(\d+)\]\.([a-zA-Z_]+)=(.*)")
            index_re = re.compile(r"^(?:index|id)=(\d+)")
            true_vals = {"1", "true", "on", "enabled", "enable", "start"}

            for l in lines:
                m = bracket_re.match(l)
                if m:
                    idx = int(m.group(2))
                    key = m.group(3).lower()
                    val = m.group(4).strip().lower()
                    indices.add(idx)
                    if ("record" in key or key in {"recording", "record_enable", "recordenabled", "recordstatus"}) and val in true_vals:
                        recording_indices.add(idx)
                    if ("online" in key or "status" in key) and val in {"online", "connected", "true", "1"}:
                        online_indices.add(idx)
                    continue
                m2 = index_re.match(l)
                if m2:
                    indices.add(int(m2.group(1)))
                # Explicit count
                m3 = re.search(r"(?:count|camera_number|channel_count|channels)=(\d+)", l, flags=re.IGNORECASE)
                if m3 and camera_count is None:
                    camera_count = int(m3.group(1))

            # Prefer online camera count if available, else total indices, else explicit count
            if online_indices:
                camera_count = len(online_indices)
            elif indices and camera_count is None:
                camera_count = len(indices)
            rec_count_generic = sum(1 for l in lines if ("record" in l.lower() and "=1" in l.replace(" ", "")))
            if recording_indices:
                recording_count = len(recording_indices)
            elif rec_count_generic > 0:
                recording_count = rec_count_generic
            return camera_count, recording_count
        except Exception:
            return None, None

    def _parse_milesight_camera_ipclist_connected_count(self, text: str) -> int | None:
        """Parse Milesight ipclist JSON: count connected/online cameras if available; fallback to 'cnt' or len(list)."""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                lst = data.get("list")
                if isinstance(lst, list) and lst:
                    online = 0
                    for item in lst:
                        if isinstance(item, dict):
                            vals = {k.lower(): str(v).strip().lower() for k, v in item.items()}
                            if vals.get("online") in {"1", "true", "on", "connected"} or vals.get("status") in {"online", "connected"}:
                                online += 1
                    if online > 0:
                        return online
                if isinstance(data.get("cnt"), int):
                    return data["cnt"]
                if isinstance(lst, list):
                    return len(lst)
        except Exception:
            pass
        return None

    def _parse_milesight_ipcstatus_online_count(self, text: str) -> int | None:
        try:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            online = set()
            pat1 = re.compile(r"^[A-Za-z_]+\[(\d+)\]\.[A-Za-z_]+=(.+)")
            for l in lines:
                m = pat1.match(l)
                if not m:
                    continue
                idx = int(m.group(1))
                val = m.group(2).strip().lower()
                ll = l.lower()
                if ("status" in ll or "online" in ll) and val in {"online", "connected", "true", "1"}:
                    online.add(idx)
            if online:
                return len(online)
            # Fallback: try to count bracketed channel indices
            indices = set()
            m2 = re.findall(r"(?:camera|channel)\[(\d+)\]", text, flags=re.IGNORECASE)
            for g in m2:
                try:
                    indices.add(int(g))
                except Exception:
                    pass
            return len(indices) if indices else None
        except Exception:
            return None

    def _parse_milesight_ipcstatus_channel_count(self, text: str) -> int | None:
        try:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            ch = set()
            m = re.compile(r"^chnid\[(\d+)\]=")
            for l in lines:
                s = m.match(l)
                if s:
                    try:
                        ch.add(int(s.group(1)))
                    except Exception:
                        pass
            return len(ch) if ch else None
        except Exception:
            return None

    def _parse_milesight_ipcstatus_details(self, text: str):
        """Parse ipcstatus to get connected (with image) and recording channel IDs.
        Handles formats:
          record[0]=1                   -> channel 0 recording flag
          connectStatus[0][0]=1         -> channel 0 main stream connected
          connection[0][0]=2            -> channel 0 connection type
          chnid[0]=0                    -> channel index mapping
          ipc[0].status=online          -> channel 0 status
        Returns (connected_ids: set[int], recording_ids: set[int]).
        """
        try:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            # Three patterns: key[idx]=val, key[idx][sub]=val, key[idx].attr=val
            pat_simple = re.compile(r'^([a-zA-Z_]+)\[(\d+)\]=(.*)')
            pat_double = re.compile(r'^([a-zA-Z_]+)\[(\d+)\]\[(\d+)\](?:\.([a-zA-Z_]+))?=(.*)')
            pat_dotattr = re.compile(r'^([a-zA-Z_]+)\[(\d+)\]\.([a-zA-Z_]+)=(.*)')

            # Per channel: record flag, connect flag
            channel_record: dict[int, bool] = {}
            channel_connected: dict[int, bool] = {}
            channel_seen: set[int] = set()

            for l in lines:
                # Try double-bracket first (more specific)
                m = pat_double.match(l)
                if m:
                    prefix = m.group(1).lower()
                    idx = int(m.group(2))
                    sub = int(m.group(3))
                    val = m.group(5).strip().lower()
                    channel_seen.add(idx)
                    # connectStatus[idx][0]=1 means main stream connected
                    if prefix == 'connectstatus' and sub == 0:
                        channel_connected[idx] = val in ('1', 'true', 'connected', 'on')
                    continue

                # Try dot-attr: ipc[0].status=online
                m = pat_dotattr.match(l)
                if m:
                    prefix = m.group(1).lower()
                    idx = int(m.group(2))
                    attr = m.group(3).lower()
                    val = m.group(4).strip().lower()
                    channel_seen.add(idx)
                    if attr in ('status', 'online', 'connectstatus') and 'record' not in attr:
                        channel_connected[idx] = val in ('online', 'connected', 'true', '1', 'on')
                    if 'record' in attr:
                        channel_record[idx] = val in ('1', 'true', 'on', 'enabled', 'enable', 'start', 'started', 'recording')
                    continue

                # Try simple bracket: record[0]=1, chnid[0]=0
                m = pat_simple.match(l)
                if m:
                    prefix = m.group(1).lower()
                    idx = int(m.group(2))
                    val = m.group(3).strip().lower()
                    channel_seen.add(idx)
                    if prefix == 'record':
                        channel_record[idx] = val in ('1', 'true', 'on', 'enabled', 'enable', 'start', 'started', 'recording')
                    elif prefix in ('status', 'online') and 'record' not in prefix:
                        channel_connected[idx] = val in ('online', 'connected', 'true', '1', 'on')
                    continue

            connected_ids: set[int] = set()
            recording_ids: set[int] = set()

            for idx in channel_seen:
                # Connected: if we have explicit connect info use it; else assume connected
                if idx in channel_connected:
                    if channel_connected[idx]:
                        connected_ids.add(idx)
                else:
                    # No explicit connect info for this channel: assume connected if listed
                    connected_ids.add(idx)

                # Recording: explicit flag
                if channel_record.get(idx, False):
                    recording_ids.add(idx)

            return connected_ids, recording_ids
        except Exception:
            return set(), set()

    def _parse_milesight_ipclist_connected_ids(self, text: str) -> set[int] | None:
        """Parse Milesight ipclist JSON: return set of channel IDs that are connected.
        Checks connectState, state, online, status fields.
        connectState=1 or state=2 means connected on Milesight NVRs.
        """
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                lst = data.get("list")
                if isinstance(lst, list) and lst:
                    ids = set()
                    for i, item in enumerate(lst):
                        if not isinstance(item, dict):
                            continue
                        vals = {k.lower(): str(v).strip().lower() for k, v in item.items()}
                        online = (
                            vals.get("online") in {"1", "true", "on", "connected"}
                            or vals.get("status") in {"online", "connected"}
                            or vals.get("connectstate") in {"1", "true", "connected"}
                            or vals.get("state") in {"2"}  # Milesight state=2 means connected
                        )
                        if online:
                            cid = item.get("id") if isinstance(item.get("id"), int) else i
                            ids.add(int(cid))
                    return ids if ids else None
        except Exception:
            return None
        return None

    def _parse_milesight_ipcstatus_recording_count(self, text: str) -> int | None:
        """Best-effort parse of recording count from ipcstatus text.
        Scans for per-channel 'record' flags in bracketed key/value lines.
        """
        try:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            record_true = {"1", "true", "on", "enabled", "enable", "start"}
            pat1 = re.compile(r"^[A-Za-z_]+\[(\d+)\]\.[A-Za-z_]+=(.+)")
            pat2 = re.compile(r"^[A-Za-z_]+\[(\d+)\]\[(\d+)\]\.[A-Za-z_]+=(.+)")
            rec_keys = {"record", "recording", "record_enable", "recordenabled", "recordstatus", "isrecording"}
            rec_indices = set()
            for l in lines:
                ll = l.lower()
                if "record" not in ll:
                    continue
                m2 = pat2.match(l)
                if m2:
                    ch = int(m2.group(1))
                    val = m2.group(4).strip().lower()
                    if any(k in ll for k in rec_keys) and val in record_true:
                        rec_indices.add(ch)
                    continue
                m1 = pat1.match(l)
                if m1:
                    ch = int(m1.group(1))
                    val = m1.group(2).strip().lower()
                    if any(k in ll for k in rec_keys) and val in record_true:
                        rec_indices.add(ch)
            if rec_indices:
                return len(rec_indices)
            generic = sum(1 for l in lines if ("record" in l.lower() and re.search(r"=\s*(1|true|on|enabled)", l, flags=re.IGNORECASE)))
            return generic if generic > 0 else None
        except Exception:
            return None

    def _parse_milesight_record_config_indices(self, text: str) -> set[int] | None:
        try:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            rec_keys = {"record", "recording", "record_enable", "recordenabled", "recordstatus", "isrecording"}
            true_vals = {"1", "true", "on", "enabled", "enable", "start"}
            pat1 = re.compile(r"^[A-Za-z_]+\[(\d+)\]\.[A-Za-z_]+=(.+)")
            pat2 = re.compile(r"^[A-Za-z_]+\[(\d+)\]\[(\d+)\]\.[A-Za-z_]+=(.+)")
            indices = set()
            for l in lines:
                ll = l.lower()
                m2 = pat2.match(l)
                if m2:
                    ch = int(m2.group(1))
                    val = m2.group(4).strip().lower()
                    if any(k in ll for k in rec_keys) and val in true_vals:
                        indices.add(ch)
                    continue
                m1 = pat1.match(l)
                if m1:
                    ch = int(m1.group(1))
                    val = m1.group(2).strip().lower()
                    if any(k in ll for k in rec_keys) and val in true_vals:
                        indices.add(ch)
            return indices if indices else None
        except Exception:
            return None

    def _parse_milesight_motion_config_count(self, text: str) -> int | None:
        try:
            lines = [l.strip().lower() for l in text.splitlines() if l.strip()]
            motion_indices = set()
            pat = re.compile(r"([a-zA-Z]+)\[(\d+)\]\.[a-zA-Z_]+=(.+)")
            for l in lines:
                m = pat.match(l)
                if not m:
                    continue
                idx = int(m.group(2))
                val = m.group(3).strip().lower()
                if ("motion" in l or "md" in l) and val in {"1", "true", "on", "enabled", "enable", "start"}:
                    motion_indices.add(idx)
            return len(motion_indices) if motion_indices else None
        except Exception:
            return None

    def _parse_milesight_system_status_camera_count(self, text: str):
        """Parse camera count from Milesight system status (multiple patterns)."""
        patterns = [
            r"camera_number=(\d+)",
            r"camera.count=(\d+)",
            r"channel_count=(\d+)",
            r"channels=(\d+)",
            r"camera\s*number\s*[:=]\s*(\d+)",
        ]
        for p in patterns:
            m = re.search(p, text, flags=re.IGNORECASE)
            if m:
                return int(m.group(1))
        # Fallback: count bracketed entries
        bracket_count = len(re.findall(r"(?:camera|channel)\[(\d+)\]", text, flags=re.IGNORECASE))
        return bracket_count if bracket_count > 0 else None

    def _parse_hikvision_channels_count(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            count = 0
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('VideoInputChannel') or tag.endswith('videoInputChannel')):
                    enabled = True
                    for child in elem:
                        ct = child.tag
                        if isinstance(ct, str) and ct.lower().endswith('enabled'):
                            val = (child.text or '').strip().lower()
                            enabled = val in {'true', '1'}
                            break
                    if enabled:
                        count += 1
            if count > 0:
                return count
            # Fallback: count inputPort elements
            fallback = sum(1 for elem in root.iter() if isinstance(elem.tag, str) and elem.tag.endswith('inputPort'))
            return fallback if fallback > 0 else None
        except Exception:
            return None

    def _parse_hikvision_recording_count(self, xml_text: str):
        """Best-effort parser: counts enabled record tracks."""
        try:
            root = ET.fromstring(xml_text)
            count_enabled = 0
            count_total = 0
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('Track') or tag.endswith('track')):
                    count_total += 1
                    enabled = None
                    for child in elem:
                        ct = child.tag
                        if isinstance(ct, str) and (ct.lower().endswith('enabled') or ct.lower().endswith('trackenabled')):
                            val = (child.text or '').strip().lower()
                            enabled = val in {'true', '1'}
                            break
                    if enabled:
                        count_enabled += 1
            if count_enabled > 0:
                return count_enabled
            if count_total > 0:
                return count_total
            return None
        except Exception:
            return None

    def _parse_hikvision_record_status(self, xml_text: str):
        """Parse /ISAPI/ContentMgmt/record/status XML to count actively recording channels."""
        try:
            root = ET.fromstring(xml_text)
            recording = 0
            total = 0
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('RecordingStatus') or tag.endswith('recordingStatus')):
                    total += 1
                    status_val = None
                    enabled_val = None
                    for child in elem:
                        ct = child.tag
                        if isinstance(ct, str):
                            text = (child.text or '').strip().lower()
                            if ct.lower().endswith('status'):
                                status_val = text
                            elif ct.lower().endswith('enabled'):
                                enabled_val = text
                    if (status_val in {'started', 'on', 'true'} or enabled_val in {'true', '1'}):
                        recording += 1
            if recording > 0:
                return recording
            return total if total > 0 else None
        except Exception:
            return None

    def _parse_hikvision_streaming_channels_physical_count(self, xml_text: str) -> int | None:
        """Derive physical camera count from /ISAPI/Streaming/channels.
        Uses dynVideoInputChannelID when present; otherwise maps id like 101/102 => channel 1.
        """
        try:
            root = ET.fromstring(xml_text)
            physical_ids = set()
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('StreamingChannel') or tag.endswith('streamingChannel')):
                    dyn_id = None
                    stream_id = None
                    for child in elem:
                        ct = child.tag
                        if isinstance(ct, str):
                            if ct.endswith('dynVideoInputChannelID') and child.text:
                                try:
                                    dyn_id = int(child.text.strip())
                                except Exception:
                                    pass
                            elif ct.endswith('id') and child.text:
                                try:
                                    stream_id = int(child.text.strip())
                                except Exception:
                                    pass
                    if dyn_id is not None:
                        physical_ids.add(dyn_id)
                    elif stream_id is not None:
                        # Map 101/102 => 1; 201/202 => 2, etc.
                        physical_ids.add(stream_id // 100)
            return len(physical_ids) if physical_ids else None
        except Exception:
            return None

    def _parse_hikvision_inputs_connected_ids(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            ids = set()
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('VideoInputChannel') or tag.endswith('videoInputChannel')):
                    vid = None
                    enabled = False
                    has_video = True
                    for child in elem:
                        ct = child.tag
                        if isinstance(ct, str):
                            tx = (child.text or '').strip()
                            if ct.endswith('id'):
                                try:
                                    vid = int(tx)
                                except Exception:
                                    pass
                            elif ct.endswith('videoInputEnabled'):
                                enabled = tx.lower() in {'true','1'}
                            elif ct.endswith('resDesc'):
                                has_video = tx.upper() != 'NO VIDEO'
                    if vid is not None and enabled and has_video:
                        ids.add(vid)
            return ids if ids else None
        except Exception:
            return None

    def _parse_hikvision_record_tracks_enabled_channels(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            enabled = set()
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and tag.endswith('Track'):
                    chan = None
                    is_enabled = False
                    for child in elem:
                        ct = child.tag
                        if isinstance(ct, str):
                            tx = (child.text or '').strip()
                            lct = ct.lower()
                            if lct.endswith('srcchannel') or lct.endswith('channel'):
                                try:
                                    chan = int(tx)
                                except Exception:
                                    pass
                            elif lct.endswith('enable'):
                                is_enabled = tx.lower() == 'true'
                    if chan is not None and is_enabled:
                        enabled.add(chan)
            return enabled if enabled else None
        except Exception:
            return None

    def _parse_hikvision_record_tracks_configured_channels(self, xml_text: str) -> set[int] | None:
        """Parse record/tracks XML and return channels configured to record.
        A channel is 'configured to record' if its DefaultRecordingMode is a
        recording mode (CMR=continuous, MR=motion, ER=event, etc.) rather than
        OFF or empty. Falls back to Enable flag if no DefaultRecordingMode found.
        Uses SrcChannel (physical channel) when available; falls back to id/100.
        """
        try:
            root = ET.fromstring(xml_text)
            configured = set()
            has_mode = False
            for elem in root.iter():
                tag = elem.tag
                if not (isinstance(tag, str) and tag.endswith('Track')):
                    continue
                chan = None
                track_id = None
                rec_mode = None
                enable_flag = None
                for child in elem:
                    ct = child.tag
                    if not isinstance(ct, str):
                        continue
                    tx = (child.text or '').strip()
                    lct = ct.lower()
                    if lct.endswith('srcchannel'):
                        try:
                            chan = int(tx)
                        except Exception:
                            pass
                    elif lct.endswith('id') and not lct.endswith('guid'):
                        try:
                            track_id = int(tx)
                        except Exception:
                            pass
                    elif lct.endswith('defaultrecordingmode'):
                        rec_mode = tx.upper()
                        has_mode = True
                    elif lct == 'enable' or lct.endswith('trackenable'):
                        enable_flag = tx.lower() in ('true', '1')
                # Determine physical channel
                if chan is None and track_id is not None:
                    chan = track_id // 100 if track_id >= 100 else track_id
                if chan is None:
                    continue
                # Determine if configured to record
                if rec_mode is not None:
                    # CMR=continuous, MR=motion, ER=event, AR=alarm — all are recording modes
                    if rec_mode and rec_mode not in ('OFF', 'NONE', ''):
                        configured.add(chan)
                elif enable_flag:
                    configured.add(chan)
            return configured if configured else None
        except Exception:
            return None

    def _parse_hikvision_record_status_unique(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            recording_channels = set()
            channels_seen = set()
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('RecordingStatus') or tag.endswith('recordingStatus')):
                    status_val = None
                    enabled_val = None
                    channel_id = None
                    for child in elem:
                        ct = child.tag
                        if isinstance(ct, str):
                            text = (child.text or '').strip()
                            lct = ct.lower()
                            if lct.endswith('status'):
                                status_val = text.lower()
                            elif lct.endswith('enabled'):
                                enabled_val = text.lower()
                            elif lct.endswith('channelid') or lct.endswith('videoinputid') or lct.endswith('dynvideoinputchannelid'):
                                channel_id = text.strip()
                    if channel_id:
                        channels_seen.add(channel_id)
                        if (status_val in {'started', 'on', 'true'} or enabled_val in {'true', '1'}):
                            recording_channels.add(channel_id)
            if recording_channels:
                return len(recording_channels)
            return len(channels_seen) if channels_seen else None
        except Exception:
            return None

    def _parse_hikvision_recording_tracks_unique(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            channels_enabled = set()
            channels_seen = set()
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('Track') or tag.endswith('track')):
                    enabled = None
                    channel_id = None
                    for child in elem:
                        ct = child.tag
                        if isinstance(ct, str):
                            lct = ct.lower()
                            val = (child.text or '').strip()
                            if lct.endswith('enabled') or lct.endswith('trackenabled'):
                                enabled = val.lower() in {'true', '1'}
                            elif lct.endswith('channelid') or lct.endswith('videoinputid') or lct.endswith('dynvideoinputchannelid'):
                                channel_id = val
                    if channel_id:
                        channels_seen.add(channel_id)
                        if enabled:
                            channels_enabled.add(channel_id)
            if channels_enabled:
                return len(channels_enabled)
            return len(channels_seen) if channels_seen else None
        except Exception:
            return None

    def _parse_hikvision_inputproxy_channels_count(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            count = 0
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('InputProxyChannel') or tag.endswith('inputProxyChannel') or tag.endswith('Channel')):
                    has_id = any((isinstance(ch.tag, str) and ch.tag.lower().endswith('id') and (ch.text or '').strip()) for ch in elem)
                    if has_id:
                        count += 1
            return count if count > 0 else None
        except Exception:
            return None

    def _parse_hikvision_inputproxy_channels_status_recording_count(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            recording = 0
            total = 0
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('InputProxyChannelStatus') or tag.endswith('inputProxyChannelStatus') or tag.endswith('ChannelStatus')):
                    total += 1
                    rec = None
                    connected = None
                    for ch in elem:
                        ct = ch.tag
                        if isinstance(ct, str):
                            tx = (ch.text or '').strip().lower()
                            if ct.lower().endswith('recording') or ct.lower().endswith('status'):
                                if tx in {'started', 'on', 'true', '1'}:
                                    rec = True
                            if ct.lower().endswith('online') or ct.lower().endswith('connectstatus'):
                                if tx in {'online', 'connected', 'true', '1'}:
                                    connected = True
                    if rec:
                        recording += 1
            if recording > 0:
                return recording
            return total if total > 0 else None
        except Exception:
            return None

    def _parse_hikvision_inputproxy_channels_status_connected_count(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            connected = 0
            total = 0
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('InputProxyChannelStatus') or tag.endswith('inputProxyChannelStatus') or tag.endswith('ChannelStatus')):
                    total += 1
                    is_conn = False
                    for ch in elem:
                        ct = ch.tag
                        if isinstance(ct, str):
                            tx = (ch.text or '').strip().lower()
                            if ct.lower().endswith('online') or ct.lower().endswith('connectstatus') or ct.lower().endswith('zerovideo'):
                                if ct.lower().endswith('zerovideo') and tx in {'true','1'}:
                                    is_conn = False
                                elif tx in {'online', 'connected', 'true', '1'}:
                                    is_conn = True
                    if is_conn:
                        connected += 1
            if connected > 0:
                return connected
            return total if total > 0 else None
        except Exception:
            return None

    def _parse_hikvision_inputproxy_channels_status_connected_ids(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            ids = set()
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('InputProxyChannelStatus') or tag.endswith('inputProxyChannelStatus') or tag.endswith('ChannelStatus')):
                    is_conn = False
                    chan_id = None
                    for ch in elem:
                        ct = ch.tag
                        if isinstance(ct, str):
                            tx = (ch.text or '').strip().lower()
                            lct = ct.lower()
                            if lct.endswith('online') or lct.endswith('connectstatus') or lct.endswith('zerovideo'):
                                if lct.endswith('zerovideo') and tx in {'true','1'}:
                                    is_conn = False
                                elif tx in {'online', 'connected', 'true', '1'}:
                                    is_conn = True
                            if lct.endswith('id') or lct.endswith('channelid') or lct.endswith('videoinputid') or lct.endswith('dynvideoinputchannelid'):
                                chan_id = (ch.text or '').strip()
                    if is_conn and chan_id:
                        ids.add(chan_id)
            return ids if ids else None
        except Exception:
            return None

    def _parse_hikvision_inputproxy_record_config_count(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            count = 0
            total = 0
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('InputProxyChannel') or tag.endswith('inputProxyChannel') or tag.endswith('Channel')):
                    total += 1
                    configured = False
                    for ch in elem:
                        ct = ch.tag
                        if isinstance(ct, str):
                            tx = (ch.text or '').strip().lower()
                            # Heuristics: recording enabled flags or schedule types
                            if ct.lower().endswith('recordenabled') and tx in {'true','1','on'}:
                                configured = True
                            if ct.lower().endswith('recordingscheduletype') or ct.lower().endswith('recordingmode') or ct.lower().endswith('recordschedule'):
                                if any(k in tx for k in ['motion','event','alarm','continuous']):
                                    configured = True
                    if configured:
                        count += 1
            if count > 0:
                return count
            return total if total > 0 else None
        except Exception:
            return None

    def _parse_hikvision_inputproxy_record_config_ids(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            ids = set()
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('InputProxyChannel') or tag.endswith('inputProxyChannel') or tag.endswith('Channel')):
                    configured = False
                    chan_id = None
                    for ch in elem:
                        ct = ch.tag
                        if isinstance(ct, str):
                            tx = (ch.text or '').strip().lower()
                            lct = ct.lower()
                            if lct.endswith('recordenabled') and tx in {'true','1','on'}:
                                configured = True
                            if lct.endswith('recordingscheduletype') or lct.endswith('recordingmode') or lct.endswith('recordschedule'):
                                if any(k in tx for k in ['motion','event','alarm','continuous']):
                                    configured = True
                            if lct.endswith('id') or lct.endswith('channelid') or lct.endswith('videoinputid') or lct.endswith('dynvideoinputchannelid'):
                                chan_id = (ch.text or '').strip()
                    if configured and chan_id:
                        ids.add(chan_id)
            return ids if ids else None
        except Exception:
            return None

    def _parse_hikvision_inputproxy_motion_config_count(self, xml_text: str):
        try:
            root = ET.fromstring(xml_text)
            motion_cfg = 0
            total = 0
            for elem in root.iter():
                tag = elem.tag
                if isinstance(tag, str) and (tag.endswith('InputProxyChannel') or tag.endswith('inputProxyChannel') or tag.endswith('Channel')):
                    total += 1
                    mode_is_motion = False
                    for ch in elem:
                        ct = ch.tag
                        if isinstance(ct, str):
                            tx = (ch.text or '').strip().lower()
                            if ct.lower().endswith('recordingscheduletype') or ct.lower().endswith('recordingmode') or ct.lower().endswith('recordschedule') or ct.lower().endswith('schedule'):
                                if any(k in tx for k in ['motion', 'event', 'alarm']):
                                    mode_is_motion = True
                    if mode_is_motion:
                        motion_cfg += 1
            if motion_cfg > 0:
                return motion_cfg
            return total if total > 0 else None
        except Exception:
            return None

    def _parse_milesight_time_response(self, text: str) -> str | None:
        """Parse Milesight time response supporting JSON or key=value formats."""
        # Try JSON
        try:
            data = json.loads(text)
            for key in ("manual_time", "time", "local_time", "datetime"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
        except Exception:
            pass
        # Fallback: regex on key=value
        patterns = [
            r"manual_time=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
            r"time=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
            r"local_time=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
        ]
        for p in patterns:
            m = re.search(p, text)
            if m:
                return m.group(1)
        return None

    def _parse_milesight_record_status(self, text: str) -> int | None:
        """Parse Milesight get.record.status text to count actively recording channels."""
        try:
            lines = [l.strip().lower() for l in text.splitlines() if l.strip()]
            true_vals = {"1", "true", "on", "enabled", "enable", "start"}
            recording = 0
            bracket_re = re.compile(r"([a-zA-Z]+)\[(\d+)\]\.([a-zA-Z_]+)=(.*)")
            for l in lines:
                m = bracket_re.match(l)
                if m:
                    key = m.group(3).lower()
                    val = m.group(4).strip().lower()
                    if ("record" in key or key in {"recording", "record_status", "recordenabled"}) and val in true_vals:
                        recording += 1
                    continue
                if ("record" in l and "=1" in l.replace(" ", "")):
                    recording += 1
            return recording if recording > 0 else None
        except Exception:
            return None

    def _parse_uniview_channel_detail_infos_camera_count(self, text: str) -> int | None:
        try:
            data = json.loads(text)
            # Common structures: {"Response": {"Data": {"DetailInfos": [{"Status":1,...}]}}}
            obj = data
            if isinstance(obj, dict) and "Response" in obj and isinstance(obj["Response"], dict):
                obj = obj["Response"]
            if isinstance(obj, dict) and "Data" in obj and isinstance(obj["Data"], dict):
                obj = obj["Data"]
            detail = obj.get("DetailInfos") if isinstance(obj, dict) else None
            if isinstance(detail, list) and detail:
                return sum(1 for d in detail if isinstance(d, dict) and int(d.get("Status", 0)) == 1)
            # Fallback: Nums may reflect total channels
            nums = obj.get("Nums") if isinstance(obj, dict) else None
            if isinstance(nums, int):
                return nums
        except Exception:
            return None
        return None
