import json
try:
    import orjson
    _HAS_ORJSON = True
except Exception:
    _HAS_ORJSON = False
import hashlib
import os
import threading
import time
import subprocess
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any
import requests
from requests.auth import HTTPDigestAuth
import concurrent.futures

# Use project root config.json (two levels up from cams-webapp/app)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
STATE_PATH = os.path.join(PROJECT_ROOT, "state.json")
EVENTS_PATH = os.path.join(PROJECT_ROOT, "events.json")
HEARTBEAT_PATH = os.path.join(PROJECT_ROOT, "heartbeat.json")


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
    def __init__(self, poll_interval: int = 15, db=None):
        self.poll_interval = poll_interval
        self.lock = threading.Lock()
        self.running = False
        self.thread: threading.Thread | None = None
        self.nvrs: List[Dict[str, Any]] = []
        self.db = db
        self._first_heartbeat_ts: float | None = None  # timestamp when monitoring first started tracking
        self._listeners: List[threading.Event] = []
        self._listeners_lock = threading.Lock()
        self.last_refresh_duration: float = 0.0
        self.refresh_count: int = 0
        self.last_refresh_finish: float = 0.0

    def load(self):
        if self.db is not None:
            try:
                docs = list(self.db["nvrs"].find({}, {"_id": 0}))
                if not docs:
                    docs = load_nvrs_from_config() or []
                    if docs:
                        fields_to_keep = {"name", "ip", "type", "username", "password", "status", "last_online", "offline_since", "date_time_status", "nvr_time", "camera_count", "recording_count", "disk_status"}
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
            nvr.setdefault("disk_status", "Unknown")
            nvr.setdefault("recording_expected", None)
            nvr.setdefault("channel_statuses", [])
        # --- Heartbeat: detect system-off gap and record unknown intervals ---
        self._init_heartbeat()

    def get_snapshot(self) -> List[Dict[str, Any]]:
        with self.lock:
            # Return a shallow copy safe for JSON
            return [dict(nvr) for nvr in self.nvrs]

    def _init_heartbeat(self):
        """On startup, read last heartbeat.  If there is a gap > 2× poll_interval,
        record that gap as an 'unknown' interval for every NVR (system was off)."""
        now_ts = int(time.time())
        last_hb = self._read_heartbeat()
        if last_hb is not None and isinstance(last_hb, (int, float)):
            gap = now_ts - int(last_hb)
            # Only treat as a real gap if > 2× poll interval (otherwise it is just normal jitter)
            threshold = max(self.poll_interval * 2, 180)
            if gap > threshold:
                gap_start = int(last_hb)
                self._record_unknown_gap(gap_start, now_ts)
                self._finalize_stale_offline_before_gap(gap_start)
        # Record the first-ever heartbeat if not set
        if self._first_heartbeat_ts is None:
            stored_first = self._read_first_heartbeat()
            if stored_first is not None:
                self._first_heartbeat_ts = stored_first
            else:
                self._first_heartbeat_ts = now_ts
                self._write_first_heartbeat(now_ts)
        self._write_heartbeat(now_ts)

    def _finalize_stale_offline_before_gap(self, gap_start: int) -> None:
        """Split stale offline state around a restart gap.

        If a device was marked Offline before shutdown, keep only the observed part
        as offline (offline_since -> gap_start), and force post-restart status to
        Unknown so the next refresh starts a fresh offline interval.
        """
        for nvr in self.nvrs:
            if nvr.get("status") != "Offline":
                continue
            ip = nvr.get("ip")
            off_start = nvr.get("offline_since")
            if ip and isinstance(off_start, (int, float)):
                off_start_i = int(off_start)
                if off_start_i < gap_start:
                    self._record_offline_interval(ip, off_start_i, gap_start)
            nvr["status"] = "Unknown"
            nvr["offline_since"] = None
            nvr["nvr_time"] = "Unknown"
            nvr["camera_count"] = "Unknown"
            nvr["recording_count"] = "Unknown"
            nvr["channel_statuses"] = []

    def _read_heartbeat(self) -> float | None:
        """Read the last heartbeat timestamp from file or DB."""
        try:
            if self.db is not None:
                doc = self.db["system_meta"].find_one({"_id": "heartbeat"})
                if doc:
                    return doc.get("last_time")
                return None
            if os.path.exists(HEARTBEAT_PATH):
                with open(HEARTBEAT_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("last_time") if isinstance(data, dict) else None
        except Exception:
            pass
        return None

    def _write_heartbeat(self, ts: float) -> None:
        """Persist heartbeat timestamp."""
        try:
            if self.db is not None:
                self.db["system_meta"].update_one(
                    {"_id": "heartbeat"},
                    {"$set": {"last_time": int(ts)}},
                    upsert=True,
                )
                return
            tmp = HEARTBEAT_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_time": int(ts)}, f)
            os.replace(tmp, HEARTBEAT_PATH)
        except Exception:
            pass

    def _read_first_heartbeat(self) -> float | None:
        """Read the first-ever heartbeat (monitoring start) timestamp."""
        try:
            if self.db is not None:
                doc = self.db["system_meta"].find_one({"_id": "first_heartbeat"})
                if doc:
                    return doc.get("ts")
                return None
            if os.path.exists(HEARTBEAT_PATH):
                with open(HEARTBEAT_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("first_time") if isinstance(data, dict) else None
        except Exception:
            pass
        return None

    def _write_first_heartbeat(self, ts: float) -> None:
        """Persist the first-ever heartbeat timestamp."""
        try:
            if self.db is not None:
                self.db["system_meta"].update_one(
                    {"_id": "first_heartbeat"},
                    {"$set": {"ts": int(ts)}},
                    upsert=True,
                )
                return
            # Merge into the heartbeat file
            data = {}
            if os.path.exists(HEARTBEAT_PATH):
                try:
                    with open(HEARTBEAT_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            data["first_time"] = int(ts)
            tmp = HEARTBEAT_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, HEARTBEAT_PATH)
        except Exception:
            pass

    def _record_unknown_gap(self, gap_start: int, gap_end: int) -> None:
        """Record a system-off gap as 'unknown' event for every known NVR IP."""
        ips = [nvr.get("ip") for nvr in self.nvrs if nvr.get("ip")]
        for ip in ips:
            try:
                if self.db is not None:
                    self.db["nvr_events"].insert_one({
                        "ip": ip,
                        "type": "unknown",
                        "start": gap_start,
                        "end": gap_end,
                    })
                else:
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
                    arr.append({"type": "unknown", "start": gap_start, "end": gap_end})
                    events[ip] = arr
                    tmp = EVENTS_PATH + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(events, f, ensure_ascii=False, separators=(",", ":"))
                    os.replace(tmp, EVENTS_PATH)
            except Exception:
                pass

    def get_calendar_meta(self) -> dict:
        """Return metadata for the calendar page."""
        return {
            "status": "ok",
            "baseline_ts": self._first_heartbeat_ts,
        }

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
            start_time = time.time()
            try:
                self._refresh()
            except Exception:
                # Keep loop alive
                pass
            end_time = time.time()
            with self.lock:
                self.last_refresh_duration = end_time - start_time
                self.refresh_count += 1
                self.last_refresh_finish = end_time
            sleep_time = max(0, self.poll_interval - (time.time() - start_time))
            time.sleep(sleep_time)

    def _refresh(self):
        now_ts = time.time()
        with self.lock:
            if not self.nvrs:
                self.load()
            # Copy basic data to avoid holding the lock during network operations
            # Shallow dict copy is sufficient because we only read/update top-level keys
            nvrs_to_check = [dict(nvr) for nvr in self.nvrs if nvr.get("ip")]

        def check_nvr(nvr_copy):
            ip = nvr_copy.get("ip")
            online = ping_ip(ip)
            if online:
                # Get vendor-specific stats
                # The _update_vendor_stats function uses 'type', 'username', 'password', 'ip'
                # which are all present in the shallow copy.
                self._update_vendor_stats(nvr_copy)
                nvr_copy["_ping_online"] = True
            else:
                nvr_copy["_ping_online"] = False
                nvr_copy["nvr_time"] = "Offline"
                nvr_copy["camera_count"] = "Offline"
                nvr_copy["recording_count"] = "Offline"
                nvr_copy["disk_status"] = "Offline"
                nvr_copy["channel_statuses"] = []
            return nvr_copy

        updated_nvrs = []
        if nvrs_to_check:
            # Maximum 32 concurrent workers to avoid huge bursts
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(nvrs_to_check))) as executor:
                updated_nvrs = list(executor.map(check_nvr, nvrs_to_check))

        # Re-apply updated stats under the lock
        with self.lock:
            nvr_dict = {nvr.get("ip"): nvr for nvr in self.nvrs if nvr.get("ip")}
            for updated in updated_nvrs:
                ip = updated.get("ip")
                target = nvr_dict.get(ip)
                if not target:
                    continue
                
                if updated.get("_ping_online"):
                    if target.get("status") == "Offline":
                        # Recovery: finalize offline interval
                        off_start = target.get("offline_since")
                        if off_start:
                            self._record_offline_interval(ip, off_start, now_ts)
                        target["offline_since"] = None
                    target["status"] = "Online"
                    target["last_online"] = now_ts
                    
                    target["nvr_time"] = updated.get("nvr_time")
                    target["camera_count"] = updated.get("camera_count")
                    target["recording_count"] = updated.get("recording_count")
                    target["disk_status"] = updated.get("disk_status")
                    target["channel_statuses"] = updated.get("channel_statuses", [])
                else:
                    if target.get("status") != "Offline":
                        target["offline_since"] = now_ts
                    target["status"] = "Offline"
                    target["nvr_time"] = "Offline"
                    target["camera_count"] = "Offline"
                    target["recording_count"] = "Offline"
                    target["disk_status"] = "Offline"
                    target["channel_statuses"] = []

        # Persist fast to separate state file (avoid rewriting config.json each refresh)
        self._write_state()
        # Update heartbeat so we can detect system-off gaps
        self._write_heartbeat(now_ts)
        self._notify_listeners()

    def _notify_listeners(self):
        with self._listeners_lock:
            for event in self._listeners:
                event.set()
            self._listeners.clear()

    def wait_for_change(self, timeout: float = 30.0):
        event = threading.Event()
        with self._listeners_lock:
            self._listeners.append(event)
        return event.wait(timeout)

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
                # Fetch both offline and unknown events
                cur = self.db["nvr_events"].find({
                    "type": {"$in": ["offline", "unknown"]},
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
                    ev_type = doc.get("type", "offline")
                    arr.append({"type": ev_type, "start": int(doc.get("start", 0)), "end": int(doc.get("end", to_ts))})
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
                                ev_type = ev.get("type")
                                if ev_type not in ("offline", "unknown"):
                                    continue
                                s = int(ev.get("start", 0))
                                e = int(ev.get("end", to_ts))
                                if s <= to_ts and e >= from_ts:
                                    lst = out.get(ip)
                                    if not isinstance(lst, list):
                                        lst = []
                                    lst.append({"type": ev_type, "start": s, "end": e})
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
                        "disk_status": n.get("disk_status"),
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
                    "disk_status": n.get("disk_status"),
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
                "recording_expected": data.get("recording_expected"),
                "status": "Unknown",
                "last_online": None,
                "offline_since": None,
                "camera_count": "Unknown",
                "recording_count": "Unknown",
                "disk_status": "Unknown",
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
            # Reset dynamic stats each poll so partial failures do not keep stale values.
            nvr["nvr_time"] = "Unknown"
            nvr["camera_count"] = "Unknown"
            nvr["recording_count"] = "Unknown"
            nvr["disk_status"] = "Unknown"
            nvr["channel_statuses"] = []
            session = requests.Session()
            if vendor in ("Hikvision", "Uniview"):
                session.auth = HTTPDigestAuth(username, password)
            else:
                session.auth = (username, password)

            if vendor == "Milesight":
                auth_failed = False
                web_auth_ok = self._milesight_web_login(session, ip, username, password, timeout=5)

                def milesight_sdk_get(action_query: str, timeout: int = 5):
                    if web_auth_ok:
                        return self._milesight_web_get(session, ip, username, password, "/sdk.cgi", f"action={action_query}", timeout=timeout)
                    return session.get(f"http://{ip}/sdk.cgi?action={action_query}", timeout=timeout)

                # Time (robust: JSON or key=value formats)
                r = milesight_sdk_get("get.system.time", timeout=5)
                if r.status_code == 200:
                    time_str = self._parse_milesight_time_response(r.text)
                    nvr["nvr_time"] = time_str or "Unknown"
                elif r.status_code == 401:
                    auth_failed = True
                    nvr["nvr_time"] = "Auth failed"
                else:
                    nvr["nvr_time"] = f"Time failed: {r.status_code}"

                # Cameras (prefer ipclist JSON) and build connected set
                connected_ids: set[int] | None = None
                recording_ids: set[int] = set()
                motion_ids: set[int] = set()
                rdisk = milesight_sdk_get("get.disk.storage_info&format=json", timeout=5)
                if rdisk.status_code == 200 and rdisk.text:
                    disk_mode = self._parse_milesight_disk_activity(rdisk.text)
                    if disk_mode == "working":
                        nvr["disk_status"] = "Working"
                    elif disk_mode == "idle":
                        nvr["disk_status"] = "Idle"
                elif rdisk.status_code == 401:
                    auth_failed = True
                rc = milesight_sdk_get("get.camera.ipclist&format=json", timeout=5)
                if rc.status_code == 200 and rc.text:
                    ids = self._parse_milesight_ipclist_connected_ids(rc.text)
                    if ids is not None:
                        connected_ids = ids
                        nvr["camera_count"] = len(ids)
                    else:
                        cnt = self._parse_milesight_camera_ipclist_connected_count(rc.text)
                        if cnt is not None:
                            nvr["camera_count"] = cnt
                elif rc.status_code == 401:
                    auth_failed = True

                # Browser UI path for camera status: /cgi/main/2001
                if web_auth_ok:
                    try:
                        r2001 = self._milesight_web_post_json(session, ip, username, password, "/cgi/main/2001", payload={}, timeout=5)
                        if r2001.status_code == 200 and r2001.text:
                            ids2001 = self._parse_milesight_cgi_camera_list_connected_ids(r2001.text)
                            if ids2001 is not None:
                                connected_ids = ids2001
                                nvr["camera_count"] = len(ids2001)
                    except Exception:
                        pass

                # Browser UI recording policy path: /cgi/main/6040 per channel
                if web_auth_ok:
                    try:
                        candidate_ids: list[int] = []
                        if connected_ids:
                            candidate_ids = sorted(int(x) for x in connected_ids)
                        else:
                            cc = nvr.get("camera_count")
                            if isinstance(cc, int) and cc > 0:
                                candidate_ids = list(range(cc))

                        if candidate_ids:
                            policy_motion_ids: set[int] = set()
                            policy_record_ids: set[int] = set()
                            for cid in candidate_ids:
                                r6040 = self._milesight_web_post_json(
                                    session,
                                    ip,
                                    username,
                                    password,
                                    "/cgi/main/6040",
                                    payload=cid,
                                    timeout=5,
                                )
                                if r6040.status_code != 200 or not r6040.text:
                                    continue
                                mode = self._parse_milesight_record_schedule_mode(r6040.text)
                                if mode == "recording":
                                    policy_record_ids.add(cid)
                                elif mode == "motion":
                                    policy_record_ids.add(cid)
                                    policy_motion_ids.add(cid)

                            if policy_record_ids:
                                recording_ids = set(policy_record_ids)
                                motion_ids = set(policy_motion_ids)
                                nvr["recording_count"] = len(recording_ids)
                                channel_total = self._coerce_channel_total(nvr.get("camera_count"), candidate_ids, recording_ids, motion_ids)
                                nvr["channel_statuses"] = self._build_channel_statuses(
                                    channel_total,
                                    recording_ids=recording_ids,
                                    motion_ids=motion_ids,
                                    connected_ids=(set(int(x) for x in connected_ids) if connected_ids else None),
                                    zero_based=True,
                                )
                    except Exception:
                        pass

                rcl = milesight_sdk_get("get.camera.list", timeout=5)
                rec_cfg_ids: set[int] = set()
                if rcl.status_code == 200 and rcl.text:
                    cam_count, rec_cfg_count = self._parse_milesight_camera_list(rcl.text)
                    if cam_count is not None and nvr.get("camera_count") in (None, "Unknown"):
                        nvr["camera_count"] = cam_count
                    motion_ids = self._parse_milesight_motion_config_indices(rcl.text) or set()
                    rec_cfg_ids = self._parse_milesight_record_config_indices(rcl.text) or set()
                elif rcl.status_code == 401:
                    auth_failed = True

                # Fallback to system status for camera count (multiple patterns)
                rs = milesight_sdk_get("get.system.status", timeout=5)
                if rs.status_code == 200 and rs.text:
                    cam_count = self._parse_milesight_system_status_camera_count(rs.text)
                    if cam_count is not None:
                        nvr["camera_count"] = cam_count
                elif rs.status_code == 401:
                    auth_failed = True

                # Baseline refresh from connected/configured info, even if ipcstatus fails later.
                curr_conn_ids = connected_ids if connected_ids is not None else set()
                configured_ids_raw = rec_cfg_ids | motion_ids
                configured_ids = self._align_channel_id_set(curr_conn_ids, configured_ids_raw)
                motion_ids = self._align_channel_id_set(curr_conn_ids, motion_ids)
                if configured_ids:
                    recording_ids = curr_conn_ids & configured_ids if curr_conn_ids else configured_ids
                    nvr["recording_count"] = len(recording_ids)
                    channel_total = self._coerce_channel_total(nvr.get("camera_count"), curr_conn_ids, recording_ids, motion_ids)
                    nvr["channel_statuses"] = self._build_channel_statuses(
                        channel_total,
                        recording_ids=recording_ids,
                        motion_ids=motion_ids,
                        connected_ids=(curr_conn_ids if curr_conn_ids else None),
                        zero_based=True,
                    )

                # Recording count: Combine connected channels with explicit recording configuration
                try:
                    ir = milesight_sdk_get("get.status.ipcstatus", timeout=5)
                    if ir.status_code == 200 and ir.text:
                        ipc_conn, ipc_rec = self._parse_milesight_ipcstatus_details(ir.text)
                        
                        curr_conn_ids = connected_ids if connected_ids is not None else (ipc_conn if ipc_conn else set())
                        configured_ids_raw = rec_cfg_ids | motion_ids
                        configured_ids = self._align_channel_id_set(curr_conn_ids, configured_ids_raw)
                        motion_ids = self._align_channel_id_set(curr_conn_ids, motion_ids)
                        
                        if curr_conn_ids and configured_ids:
                            recording_ids = curr_conn_ids & configured_ids
                        elif configured_ids:
                            recording_ids = configured_ids
                        else:
                            if ipc_rec:
                                recording_ids = curr_conn_ids & ipc_rec if curr_conn_ids else ipc_rec
                            else:
                                recording_ids = curr_conn_ids
                                
                        nvr["recording_count"] = len(recording_ids)

                        channel_total = self._coerce_channel_total(nvr.get("camera_count"), curr_conn_ids, recording_ids, motion_ids)
                        nvr["channel_statuses"] = self._build_channel_statuses(
                            channel_total,
                            recording_ids=recording_ids,
                            motion_ids=motion_ids,
                            connected_ids=(curr_conn_ids if curr_conn_ids else None),
                            zero_based=True,
                        )
                    elif ir.status_code == 401:
                        auth_failed = True
                except Exception:
                    pass

                if auth_failed and nvr.get("recording_count") in (None, "Unknown"):
                    nvr["camera_count"] = "Auth failed"
                    nvr["recording_count"] = "Auth failed"
                    nvr["channel_statuses"] = []

            elif vendor == "Milesight Old":
                auth_failed = False
                web_auth_ok = self._milesight_web_login(session, ip, username, password, timeout=6)

                def milesight_old_sdk_get(action_query: str, timeout: int = 6):
                    if web_auth_ok:
                        return self._milesight_web_get(session, ip, username, password, "/sdk.cgi", f"action={action_query}", timeout=timeout)
                    return session.get(f"http://{ip}/sdk.cgi?action={action_query}", timeout=timeout)

                r = milesight_old_sdk_get("get.system.time", timeout=6)
                if r.status_code == 200 and r.text:
                    time_str = self._parse_milesight_time_response(r.text)
                    nvr["nvr_time"] = time_str or "Unknown"
                elif r.status_code == 401:
                    auth_failed = True
                    nvr["nvr_time"] = "Auth failed"
                else:
                    nvr["nvr_time"] = f"Time failed: {r.status_code}"
                motion_ids: set[int] = set()
                rec_cfg_ids: set[int] = set()
                rdisk = milesight_old_sdk_get("get.disk.storage_info&format=json", timeout=6)
                if rdisk.status_code == 200 and rdisk.text:
                    disk_mode = self._parse_milesight_disk_activity(rdisk.text)
                    if disk_mode == "working":
                        nvr["disk_status"] = "Working"
                    elif disk_mode == "idle":
                        nvr["disk_status"] = "Idle"
                elif rdisk.status_code == 401:
                    auth_failed = True

                if web_auth_ok:
                    try:
                        r2001 = self._milesight_web_post_json(session, ip, username, password, "/cgi/main/2001", payload={}, timeout=6)
                        if r2001.status_code == 200 and r2001.text:
                            ids2001 = self._parse_milesight_cgi_camera_list_connected_ids(r2001.text)
                            if ids2001 is not None:
                                nvr["camera_count"] = len(ids2001)
                    except Exception:
                        pass

                try:
                    rcl = milesight_old_sdk_get("get.camera.list", timeout=6)
                    if rcl.status_code == 200 and rcl.text:
                        motion_ids = self._parse_milesight_motion_config_indices(rcl.text) or set()
                        rec_cfg_ids = self._parse_milesight_record_config_indices(rcl.text) or set()
                    elif rcl.status_code == 401:
                        auth_failed = True
                except Exception:
                    pass

                # Prefer policy-mode truth when web schedule endpoint is available.
                if web_auth_ok:
                    try:
                        candidate_ids: list[int] = []
                        cc = nvr.get("camera_count")
                        if isinstance(cc, int) and cc > 0:
                            candidate_ids = list(range(cc))

                        policy_motion_ids: set[int] = set()
                        policy_record_ids: set[int] = set()
                        for cid in candidate_ids:
                            r6040 = self._milesight_web_post_json(
                                session,
                                ip,
                                username,
                                password,
                                "/cgi/main/6040",
                                payload=cid,
                                timeout=6,
                            )
                            if r6040.status_code != 200 or not r6040.text:
                                continue
                            mode = self._parse_milesight_record_schedule_mode(r6040.text)
                            if mode == "recording":
                                policy_record_ids.add(cid)
                            elif mode == "motion":
                                policy_record_ids.add(cid)
                                policy_motion_ids.add(cid)

                        if policy_record_ids:
                            recording_ids = set(policy_record_ids)
                            motion_ids = set(policy_motion_ids)
                            nvr["recording_count"] = len(recording_ids)
                            channel_total = self._coerce_channel_total(nvr.get("camera_count"), candidate_ids, recording_ids, motion_ids)
                            nvr["channel_statuses"] = self._build_channel_statuses(
                                channel_total,
                                recording_ids=recording_ids,
                                motion_ids=motion_ids,
                                connected_ids=(set(candidate_ids) if candidate_ids else None),
                                zero_based=True,
                            )
                    except Exception:
                        pass

                # Baseline refresh from configured channels when camera.list is available.
                configured_ids_raw = rec_cfg_ids | motion_ids
                if configured_ids_raw:
                    recording_ids = set(configured_ids_raw)
                    nvr["recording_count"] = len(recording_ids)
                    channel_total = self._coerce_channel_total(nvr.get("camera_count"), recording_ids, motion_ids)
                    nvr["channel_statuses"] = self._build_channel_statuses(
                        channel_total,
                        recording_ids=recording_ids,
                        motion_ids=motion_ids,
                        connected_ids=None,
                        zero_based=True,
                    )

                ir = milesight_old_sdk_get("get.status.ipcstatus", timeout=6)
                if ir.status_code == 200 and ir.text:
                    ipc_conn, ipc_rec = self._parse_milesight_ipcstatus_details(ir.text)
                    if ipc_conn:
                        nvr["camera_count"] = len(ipc_conn)
                    else:
                        cc = self._parse_milesight_ipcstatus_channel_count(ir.text)
                        if cc is not None:
                            nvr["camera_count"] = cc
                    curr_conn_ids = ipc_conn if ipc_conn else set()
                    if not curr_conn_ids:
                        cam_count = nvr.get("camera_count")
                        if isinstance(cam_count, int):
                            curr_conn_ids = set(range(cam_count))

                    configured_ids_raw = rec_cfg_ids | motion_ids
                    configured_ids = self._align_channel_id_set(curr_conn_ids, configured_ids_raw)
                    motion_ids = self._align_channel_id_set(curr_conn_ids, motion_ids)
                    
                    if curr_conn_ids and configured_ids:
                        recording_ids = curr_conn_ids & configured_ids
                    elif configured_ids:
                        recording_ids = configured_ids
                    else:
                        if ipc_rec:
                            recording_ids = curr_conn_ids & ipc_rec if curr_conn_ids else ipc_rec
                        else:
                            recording_ids = curr_conn_ids

                    nvr["recording_count"] = len(recording_ids)
                    
                    channel_total = self._coerce_channel_total(nvr.get("camera_count"), curr_conn_ids, recording_ids, motion_ids)
                    nvr["channel_statuses"] = self._build_channel_statuses(
                        channel_total,
                        recording_ids=recording_ids,
                        motion_ids=motion_ids,
                        connected_ids=(curr_conn_ids if curr_conn_ids else None),
                        zero_based=True,
                    )
                elif ir.status_code == 401:
                    auth_failed = True

                if auth_failed and nvr.get("recording_count") in (None, "Unknown"):
                    nvr["camera_count"] = "Auth failed"
                    nvr["recording_count"] = "Auth failed"
                    nvr["channel_statuses"] = []

            elif vendor == "Hikvision":
                disk_states: list[str] = []
                storage_url = f"http://{ip}/ISAPI/ContentMgmt/Storage"
                storage_resp = session.get(storage_url, timeout=5)
                if storage_resp.status_code == 200 and storage_resp.text:
                    storage_state = self._parse_hikvision_storage_activity(storage_resp.text)
                    if storage_state in {"working", "idle"}:
                        disk_states.append(storage_state)
                    hdd_ids = self._parse_hikvision_storage_hdd_ids(storage_resp.text)
                    for hdd_id in hdd_ids:
                        sync_url = f"http://{ip}/ISAPI/ContentMgmt/Storage/hdd/{hdd_id}/syncStatus?format=json"
                        try:
                            sync_resp = session.get(sync_url, timeout=5)
                        except Exception:
                            continue
                        if sync_resp.status_code != 200 or not sync_resp.text:
                            continue
                        sync_state = self._parse_hikvision_sync_status_activity(sync_resp.text)
                        if sync_state in {"working", "idle"}:
                            disk_states.append(sync_state)
                merged_disk = self._merge_disk_activity_states(disk_states)
                if merged_disk == "working":
                    nvr["disk_status"] = "Working"
                elif merged_disk == "normal":
                    nvr["disk_status"] = "Normal"
                elif merged_disk == "idle":
                    nvr["disk_status"] = "Idle"

                # Time (namespace-agnostic)
                time_url = f"http://{ip}/ISAPI/System/time"
                r = session.get(time_url, timeout=5)
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
                        if time_str:
                            nvr["nvr_time"] = self._format_hikvision_time_value(time_str) or time_str
                        else:
                            nvr["nvr_time"] = "Unknown"
                    except Exception:
                        nvr["nvr_time"] = "Parse error"
                else:
                    # Fallback: device status currentDeviceTime
                    status_url = f"http://{ip}/ISAPI/System/status"
                    rs = session.get(status_url, timeout=5)
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
                            if time_str:
                                nvr["nvr_time"] = self._format_hikvision_time_value(time_str) or time_str
                            else:
                                nvr["nvr_time"] = f"Time failed: {rs.status_code}"
                        except Exception:
                            nvr["nvr_time"] = f"Time failed: {rs.status_code}"
                    else:
                        nvr["nvr_time"] = f"Time failed: {rs.status_code}"

                # --- Camera count: try inputs/channels, then InputProxy/channels/status, then Streaming ---
                connected_ids = None
                ch_url = f"http://{ip}/ISAPI/System/Video/inputs/channels"
                rc = session.get(ch_url, timeout=5)
                if rc.status_code == 200 and rc.text:
                    connected_ids = self._parse_hikvision_inputs_connected_ids(rc.text)
                    if connected_ids:
                        nvr["camera_count"] = len(connected_ids)
                if not connected_ids:
                    # Fallback: InputProxy/channels/status (many Hikvision NVRs use this)
                    proxy_url = f"http://{ip}/ISAPI/ContentMgmt/InputProxy/channels/status"
                    ps = session.get(proxy_url, timeout=5)
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
                    sc = session.get(stream_url, timeout=5)
                    if sc.status_code == 200 and sc.text:
                        count = self._parse_hikvision_streaming_channels_physical_count(sc.text)
                        if count is not None:
                            nvr["camera_count"] = count

                # --- Recording config: check DefaultRecordingMode per track ---
                tracks_url = f"http://{ip}/ISAPI/ContentMgmt/record/tracks"
                tr = session.get(tracks_url, timeout=5)
                if tr.status_code == 200 and tr.text:
                    channel_modes = self._parse_hikvision_record_tracks_channel_modes(tr.text)
                    rec_configured = self._parse_hikvision_record_tracks_configured_channels(tr.text)

                    motion_enabled_channels: set[int] = set()
                    motion_probe_ok = False
                    probe_channels = set()
                    if connected_ids:
                        probe_channels |= {int(c) for c in connected_ids}
                    if rec_configured:
                        probe_channels |= {int(c) for c in rec_configured}

                    for ch in sorted(probe_channels):
                        md_url = f"http://{ip}/ISAPI/System/Video/inputs/channels/{ch}/motionDetection"
                        try:
                            mr = session.get(md_url, timeout=5)
                        except Exception:
                            continue
                        if mr.status_code != 200 or not mr.text:
                            continue
                        motion_probe_ok = True
                        enabled = self._parse_hikvision_motion_detection_enabled(mr.text)
                        if enabled:
                            motion_enabled_channels.add(ch)

                    if motion_probe_ok:
                        merged_record_configured = set(rec_configured or set()) | motion_enabled_channels
                        if connected_ids and merged_record_configured:
                            nvr["recording_count"] = len(set(int(c) for c in connected_ids) & merged_record_configured)
                        elif merged_record_configured:
                            nvr["recording_count"] = len(merged_record_configured)

                        # Align display with configured policy for connected channels.
                        # Disconnected channels stay not-recording.
                        if connected_ids:
                            conn_set = set(int(c) for c in connected_ids)
                        else:
                            conn_set = set(int(c) for c in merged_record_configured)
                        for ch in conn_set:
                            if ch in motion_enabled_channels:
                                channel_modes[ch] = "motion"
                            elif ch in merged_record_configured:
                                channel_modes[ch] = "recording"
                            else:
                                channel_modes[ch] = "not-recording"

                    else:
                        if connected_ids and rec_configured:
                            nvr["recording_count"] = len(connected_ids & rec_configured)
                        elif rec_configured:
                            nvr["recording_count"] = len(rec_configured)

                    channel_total = self._coerce_channel_total(
                        nvr.get("camera_count"),
                        connected_ids,
                        rec_configured,
                        set(channel_modes.keys()),
                        one_based=True,
                    )
                    nvr["channel_statuses"] = self._build_channel_statuses_from_modes(
                        channel_total,
                        channel_modes,
                        connected_ids=(set(int(c) for c in connected_ids) if connected_ids else None),
                    )
            elif vendor == "Uniview":
                auth_failed = False

                def uniview_get(path: str, timeout: int = 6):
                    url = f"http://{ip}{path}"
                    best_resp = None
                    # Try digest-session first, then explicit basic auth fallback.
                    for mode in ("digest", "basic"):
                        try:
                            if mode == "digest":
                                resp = session.get(url, timeout=timeout)
                            else:
                                resp = requests.get(url, auth=(username, password), timeout=timeout)
                        except Exception:
                            continue
                        best_resp = resp
                        if resp.status_code == 200 and (resp.text or ""):
                            return resp
                    return best_resp

                # Time: probe multiple endpoints and keep the first that yields a parseable value.
                time_status = None
                preferred_time_path = nvr.get("_uniview_time_path")
                time_paths = [
                    "/ISAPI/System/time",
                    "/ISAPI/System/status",
                    "/LAPI/V1.0/System/Time",
                    "/ISAPI/System/deviceInfo",
                ]
                if isinstance(preferred_time_path, str) and preferred_time_path:
                    time_paths = [preferred_time_path] + [p for p in time_paths if p != preferred_time_path]

                for path in time_paths:
                    tr = uniview_get(path, timeout=6)
                    if tr is None:
                        continue
                    time_status = tr.status_code
                    if tr.status_code == 401:
                        auth_failed = True
                        continue
                    if tr.status_code != 200 or not tr.text:
                        continue
                    parsed_time = self._parse_uniview_time_value(tr.text)
                    if parsed_time:
                        nvr["nvr_time"] = parsed_time
                        nvr["_uniview_time_path"] = path
                        break

                if nvr.get("nvr_time") == "Unknown":
                    if auth_failed:
                        nvr["nvr_time"] = "Auth failed"
                    elif time_status is not None:
                        nvr["nvr_time"] = f"Time failed: {time_status}"

                # Camera count and connected set.
                cam_count_val = None
                connected_ids: set[int] | None = None
                lr = uniview_get("/LAPI/V1.0/Channels/System/ChannelDetailInfos", timeout=6)
                if lr and lr.status_code == 200 and lr.text:
                    ids = self._parse_uniview_channel_detail_infos_connected_ids(lr.text)
                    if ids:
                        connected_ids = ids
                        cam_count_val = len(ids)
                    cc = self._parse_uniview_channel_detail_infos_camera_count(lr.text)
                    if cc is not None:
                        cam_count_val = cc
                elif lr and lr.status_code == 401:
                    auth_failed = True

                if connected_ids is None:
                    rc = uniview_get("/ISAPI/System/Video/inputs/channels", timeout=5)
                    if rc and rc.status_code == 200 and rc.text:
                        connected_ids = self._parse_hikvision_inputs_connected_ids(rc.text)
                        if connected_ids:
                            cam_count_val = len(connected_ids)
                    elif rc and rc.status_code == 401:
                        auth_failed = True

                if cam_count_val is None:
                    sc = uniview_get("/ISAPI/Streaming/channels", timeout=5)
                    if sc and sc.status_code == 200 and sc.text:
                        cam_count_val = self._parse_hikvision_streaming_channels_physical_count(sc.text)
                    elif sc and sc.status_code == 401:
                        auth_failed = True
                if cam_count_val is not None:
                    nvr["camera_count"] = cam_count_val

                # Recording: probe known endpoints and cache the first one that works.
                recording_set = False

                # Primary Uniview firmware path discovered from web UI traffic:
                # /LAPI/V1.0/Channels/{id}/Storage/Private/Schedule/Record/
                schedule_modes: dict[int, str] = {}
                schedule_record_ids: set[int] = set()
                candidate_channel_ids: list[int] = []
                if connected_ids:
                    candidate_channel_ids = sorted(int(c) for c in connected_ids)
                else:
                    cc = nvr.get("camera_count")
                    if isinstance(cc, int) and cc > 0:
                        candidate_channel_ids = list(range(1, cc + 1))

                for cid in candidate_channel_ids:
                    schedule_path = f"/LAPI/V1.0/Channels/{cid}/Storage/Private/Schedule/Record/"
                    sr = uniview_get(schedule_path, timeout=6)
                    if sr is None:
                        continue
                    if sr.status_code == 401:
                        auth_failed = True
                        continue
                    if sr.status_code != 200 or not sr.text:
                        continue
                    mode = self._parse_uniview_lapi_record_schedule_mode(sr.text)
                    if mode is None:
                        continue
                    schedule_modes[int(cid)] = mode
                    if mode in ("recording", "motion"):
                        schedule_record_ids.add(int(cid))

                if schedule_modes:
                    if connected_ids is not None:
                        connected_int = set(int(c) for c in connected_ids)
                        nvr["recording_count"] = len(connected_int & schedule_record_ids)
                    else:
                        nvr["recording_count"] = len(schedule_record_ids)

                    channel_total = self._coerce_channel_total(
                        nvr.get("camera_count"),
                        connected_ids,
                        set(schedule_modes.keys()),
                        schedule_record_ids,
                        one_based=True,
                    )
                    nvr["channel_statuses"] = self._build_channel_statuses_from_modes(
                        channel_total,
                        schedule_modes,
                        connected_ids=(set(int(c) for c in connected_ids) if connected_ids else None),
                    )
                    nvr["_uniview_record_path"] = "/LAPI/V1.0/Channels/{id}/Storage/Private/Schedule/Record/"
                    recording_set = True

                preferred_record_path = nvr.get("_uniview_record_path")
                record_paths = [
                    "/ISAPI/ContentMgmt/record/tracks",
                    "/ISAPI/ContentMgmt/record/status",
                    "/ISAPI/ContentMgmt/InputProxy/channels/status",
                    "/ISAPI/ContentMgmt/InputProxy/channels",
                ]
                if isinstance(preferred_record_path, str) and preferred_record_path:
                    record_paths = [preferred_record_path] + [p for p in record_paths if p != preferred_record_path]

                for record_path in ([] if recording_set else record_paths):
                    rr = uniview_get(record_path, timeout=6)
                    if rr is None:
                        continue
                    if rr.status_code == 401:
                        auth_failed = True
                        continue
                    if rr.status_code != 200 or not rr.text:
                        continue

                    if record_path.endswith("/record/tracks"):
                        channel_modes = self._parse_hikvision_record_tracks_channel_modes(rr.text)
                        rec_configured = self._parse_hikvision_record_tracks_configured_channels(rr.text)

                        if connected_ids and rec_configured:
                            nvr["recording_count"] = len(set(int(c) for c in connected_ids) & set(int(c) for c in rec_configured))
                            recording_set = True
                        elif rec_configured:
                            nvr["recording_count"] = len(rec_configured)
                            recording_set = True

                        if channel_modes:
                            channel_total = self._coerce_channel_total(
                                nvr.get("camera_count"),
                                connected_ids,
                                rec_configured,
                                set(channel_modes.keys()),
                                one_based=True,
                            )
                            nvr["channel_statuses"] = self._build_channel_statuses_from_modes(
                                channel_total,
                                channel_modes,
                                connected_ids=(set(int(c) for c in connected_ids) if connected_ids else None),
                            )

                    elif record_path.endswith("/record/status"):
                        rec = self._parse_hikvision_record_status_unique(rr.text)
                        if rec is not None:
                            nvr["recording_count"] = rec
                            recording_set = True

                    elif record_path.endswith("/InputProxy/channels/status"):
                        proxy_ids_raw = self._parse_hikvision_inputproxy_channels_status_connected_ids(rr.text)
                        proxy_connected_ids: set[int] = set()
                        if proxy_ids_raw:
                            for cid in proxy_ids_raw:
                                try:
                                    proxy_connected_ids.add(int(cid))
                                except Exception:
                                    pass
                        if proxy_connected_ids and not connected_ids:
                            connected_ids = proxy_connected_ids
                            if cam_count_val is None:
                                nvr["camera_count"] = len(proxy_connected_ids)

                        proxy_modes = self._parse_hikvision_inputproxy_channels_status_channel_modes(rr.text)
                        if proxy_modes:
                            rec_ids = {int(cid) for cid, mode in proxy_modes.items() if mode == "recording"}
                            if connected_ids and rec_ids:
                                nvr["recording_count"] = len(set(int(c) for c in connected_ids) & rec_ids)
                                recording_set = True
                            elif rec_ids:
                                nvr["recording_count"] = len(rec_ids)
                                recording_set = True

                            channel_total = self._coerce_channel_total(
                                nvr.get("camera_count"),
                                connected_ids,
                                set(proxy_modes.keys()),
                                rec_ids,
                                one_based=True,
                            )
                            nvr["channel_statuses"] = self._build_channel_statuses_from_modes(
                                channel_total,
                                proxy_modes,
                                connected_ids=(set(int(c) for c in connected_ids) if connected_ids else None),
                            )

                        if not recording_set:
                            proxy_rec = self._parse_hikvision_inputproxy_channels_status_recording_count(rr.text)
                            if proxy_rec is not None:
                                nvr["recording_count"] = proxy_rec
                                recording_set = True

                    else:
                        cfg_ids = self._parse_hikvision_inputproxy_record_config_ids(rr.text)
                        if cfg_ids:
                            cfg_int = set()
                            for cid in cfg_ids:
                                try:
                                    cfg_int.add(int(cid))
                                except Exception:
                                    pass
                            if connected_ids and cfg_int:
                                nvr["recording_count"] = len(set(int(c) for c in connected_ids) & cfg_int)
                                recording_set = True
                            elif cfg_int:
                                nvr["recording_count"] = len(cfg_int)
                                recording_set = True

                    if recording_set:
                        nvr["_uniview_record_path"] = record_path
                        break

                if auth_failed and nvr.get("recording_count") in (None, "Unknown"):
                    nvr["camera_count"] = "Auth failed"
                    nvr["recording_count"] = "Auth failed"
                    nvr["channel_statuses"] = []
        except Exception:
            # Keep refresh robust; do not crash loop
            pass
        finally:
            if 'session' in locals():
                session.close()

    def _coerce_channel_total(self, channel_count: Any, *channel_sets: Any, one_based: bool = False) -> int | None:
        base_total = None
        try:
            if isinstance(channel_count, str) and channel_count.isdigit():
                base_total = int(channel_count)
            if isinstance(channel_count, int) and channel_count > 0:
                base_total = channel_count
        except Exception:
            pass
        max_channel = -1
        for values in channel_sets:
            if not values:
                continue
            try:
                current_max = max(int(value) for value in values)
            except Exception:
                continue
            if current_max > max_channel:
                max_channel = current_max
        inferred_total = (max_channel if one_based else (max_channel + 1)) if max_channel >= 0 else None
        if base_total is None:
            return inferred_total
        if inferred_total is None:
            return base_total
        return max(base_total, inferred_total)

    def _build_channel_statuses(
        self,
        channel_total: int | None,
        recording_ids: set[int] | None = None,
        motion_ids: set[int] | None = None,
        connected_ids: set[int] | None = None,
        zero_based: bool = False,
    ) -> list[dict[str, Any]]:
        if channel_total is None or channel_total <= 0:
            return []
        recording_ids = recording_ids or set()
        motion_ids = motion_ids or set()
        connected_ids = connected_ids or set()
        out: list[dict[str, Any]] = []
        for display_channel in range(1, channel_total + 1):
            channel_id = display_channel - 1 if zero_based else display_channel
            if connected_ids and channel_id not in connected_ids:
                status = "no-camera"
            elif channel_id in motion_ids and channel_id in recording_ids:
                status = "motion"
            elif channel_id in recording_ids:
                status = "recording"
            else:
                status = "not-recording"
            out.append({"channel": display_channel, "status": status})
        return out

    def _build_channel_statuses_from_modes(
        self,
        channel_total: int | None,
        channel_modes: dict[int, str] | None,
        connected_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        if channel_total is None or channel_total <= 0:
            return []
        channel_modes = channel_modes or {}
        connected_ids = connected_ids or set()
        out: list[dict[str, Any]] = []
        for display_channel in range(1, channel_total + 1):
            if connected_ids and display_channel not in connected_ids:
                mode = "no-camera"
            else:
                mode = channel_modes.get(display_channel, "not-recording")
            out.append({"channel": display_channel, "status": mode})
        return out

    def _align_channel_id_set(self, connected_ids: set[int] | None, configured_ids: set[int] | None) -> set[int]:
        """Align configured IDs to connected IDs when vendor responses mix 0/1-based numbering."""
        if not configured_ids:
            return set()
        if not connected_ids:
            return set(configured_ids)

        candidates = [
            set(configured_ids),
            {idx + 1 for idx in configured_ids},
            {idx - 1 for idx in configured_ids if idx > 0},
        ]
        best = candidates[0]
        best_overlap = len(connected_ids & best)
        for candidate in candidates[1:]:
            overlap = len(connected_ids & candidate)
            if overlap > best_overlap:
                best = candidate
                best_overlap = overlap
        return best

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

    def _parse_milesight_cgi_camera_list_connected_ids(self, text: str) -> set[int] | None:
        """Parse /cgi/main/2001 response ({"IPC": [...]}) and return connected channel IDs."""
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            ipc_list = data.get("IPC")
            if not isinstance(ipc_list, list):
                return None
            ids: set[int] = set()
            for i, item in enumerate(ipc_list):
                if not isinstance(item, dict):
                    continue
                vals = {k.lower(): str(v).strip().lower() for k, v in item.items()}
                enabled = vals.get("enable") in {"1", "true"}
                connected = vals.get("connectstate") in {"1", "true"} or vals.get("state") in {"2"}
                if enabled and connected:
                    cid = item.get("id") if isinstance(item.get("id"), int) else i
                    ids.add(int(cid))
            return ids if ids else None
        except Exception:
            return None

    def _milesight_md5(self, text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def _milesight_digest_auth_header(self, username: str, password: str, uri: str, method: str) -> str:
        realm = "MSHN"
        nonce = self._milesight_md5(f"time-stamp :{int(time.time() * 1000)}")
        nc = "00000001"
        qop = "auth"
        cnonce = self._milesight_md5(f"cnonce:{int(time.time() * 1000)}")
        ha1 = self._milesight_md5(f"{username}:{realm}:{password}")
        ha2 = self._milesight_md5(f"{method}:{uri}")
        response = self._milesight_md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        return (
            f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}", qop={qop}, nc={nc}, cnonce={cnonce}'
        )

    def _milesight_web_login(self, session: requests.Session, ip: str, username: str, password: str, timeout: int = 5) -> bool:
        """Perform Milesight web login flow used by browser scripts."""
        try:
            # Optional pre-check endpoint used by UI; ignore failures/lock hints and attempt login anyway.
            check_pwd = self._milesight_md5(password)
            check_url = f"http://{ip}/checkUser?user={username}&password={check_pwd}&type=1"
            try:
                session.get(check_url, timeout=timeout)
            except Exception:
                pass

            login_uri = "/cgi/main/1000"
            login_url = f"http://{ip}{login_uri}"
            headers = {
                "Authorization": self._milesight_digest_auth_header(username, password, login_uri, "POST"),
                "Content-Type": "application/json",
            }
            payload = {"userName": username, "md5": self._milesight_md5(password)}
            r = session.post(login_url, headers=headers, json=payload, timeout=timeout)
            if r.status_code != 200:
                return False
            try:
                data = r.json()
                if isinstance(data, dict) and int(data.get("type", -1)) == 0:
                    session_id = data.get("sessionId")
                    if session_id is None:
                        session_id = data.get("sessionid")
                    if session_id is not None:
                        session.headers["X-Milesight-SessionId"] = str(session_id)
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def _milesight_web_get(self, session: requests.Session, ip: str, username: str, password: str, uri: str, query: str = "", timeout: int = 5) -> requests.Response:
        url = f"http://{ip}{uri}"
        if query:
            url = f"{url}?{query}"
        headers = {"Authorization": self._milesight_digest_auth_header(username, password, uri, "GET")}
        return session.get(url, headers=headers, timeout=timeout)

    def _milesight_web_post_json(self, session: requests.Session, ip: str, username: str, password: str, uri: str, payload: Any = None, timeout: int = 5) -> requests.Response:
        headers = {
            "Authorization": self._milesight_digest_auth_header(username, password, uri, "POST"),
            "Content-Type": "application/json",
        }
        return session.post(f"http://{ip}{uri}", headers=headers, json=({} if payload is None else payload), timeout=timeout)

    def _parse_milesight_record_schedule_mode(self, text: str) -> str | None:
        """Parse /cgi/main/6040 response and return channel mode: motion/recording/not-recording."""
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            schedule = data.get("schedule")
            if not isinstance(schedule, list):
                return None

            # Milesight action types seen in UI scripts:
            # 1=TIMING_RECORD, 2=MOTION_RECORD, others >0 are event-based recording.
            has_timing = False
            has_event_or_motion = False

            for day in schedule:
                if not isinstance(day, dict):
                    continue
                if int(day.get("wholedayEnable", 0)) == 1:
                    whole_type = int(day.get("wholedayActionType", 0) or 0)
                    if whole_type == 1:
                        has_timing = True
                    elif whole_type > 0:
                        has_event_or_motion = True
                plans = day.get("plans")
                if isinstance(plans, list):
                    for plan in plans:
                        ptype = None
                        if isinstance(plan, dict):
                            ptype = int(plan.get("actionType", 0) or 0)
                        elif isinstance(plan, list) and len(plan) >= 3:
                            try:
                                ptype = int(plan[2] or 0)
                            except Exception:
                                ptype = 0
                        if ptype is None:
                            continue
                        if ptype == 1:
                            has_timing = True
                        elif ptype > 0:
                            has_event_or_motion = True

            if has_event_or_motion:
                return "motion"
            if has_timing:
                return "recording"
            return "not-recording"
        except Exception:
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
            try:
                data = json.loads(text)
                ids = set()
                for key in ("cameras", "camera", "channels"):
                    arr = data.get(key)
                    if isinstance(arr, list):
                        for i, item in enumerate(arr):
                            if not isinstance(item, dict):
                                continue
                            vals = {k.lower(): str(v).strip().lower() for k, v in item.items()}
                            if any(vals.get(k, "") in {"1", "true", "on", "enabled", "enable", "start"} for k in ("record", "recording", "record_enable", "recordenabled", "recordstatus")):
                                cid = item.get("id") if isinstance(item.get("id"), int) else i
                                ids.add(int(cid))
                        if ids:
                            return ids
            except Exception:
                pass

            lines = [l.strip() for l in text.splitlines() if l.strip()]
            rec_keys = {"record", "recording", "record_enable", "recordenabled", "recordstatus", "isrecording"}
            true_vals = {"1", "true", "on", "enabled", "enable", "start"}
            pat1 = re.compile(r"^[A-Za-z_]+\[(\d+)\]\.[A-Za-z_]+=(.+)")
            pat2 = re.compile(r"^[A-Za-z_]+\[(\d+)\]\[(\d+)\]\.[A-Za-z_]+=(.+)")
            pat_simple = re.compile(r"^([A-Za-z_]+)\[(\d+)\]=(.*)")
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
                    continue
                ms = pat_simple.match(l)
                if ms:
                    key = ms.group(1).lower()
                    ch = int(ms.group(2))
                    val = ms.group(3).strip().lower()
                    if any(k in key for k in rec_keys) and val in true_vals:
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

    def _parse_milesight_motion_config_indices(self, text: str) -> set[int] | None:
        try:
            try:
                data = json.loads(text)
                ids = set()
                for key in ("cameras", "camera", "channels"):
                    arr = data.get(key)
                    if isinstance(arr, list):
                        for i, item in enumerate(arr):
                            if not isinstance(item, dict):
                                continue
                            vals = {k.lower(): str(v).strip().lower() for k, v in item.items()}
                            if any(vals.get(k, "") in {"1", "true", "on", "enabled", "enable", "start"} for k in ("motion", "md", "motion_enable", "motionenable")):
                                cid = item.get("id") if isinstance(item.get("id"), int) else i
                                ids.add(int(cid))
                        if ids:
                            return ids
            except Exception:
                pass

            lines = [l.strip().lower() for l in text.splitlines() if l.strip()]
            motion_indices = set()
            pat = re.compile(r"([a-zA-Z]+)\[(\d+)\]\.[a-zA-Z_]+=(.+)")
            pat_simple = re.compile(r"^([a-zA-Z_]+)\[(\d+)\]=(.*)")
            for l in lines:
                m = pat.match(l)
                if not m:
                    ms = pat_simple.match(l)
                    if not ms:
                        continue
                    key = ms.group(1).lower()
                    idx = int(ms.group(2))
                    val = ms.group(3).strip().lower()
                    if ("motion" in key or "md" in key) and val in {"1", "true", "on", "enabled", "enable", "start"}:
                        motion_indices.add(idx)
                    continue
                idx = int(m.group(2))
                val = m.group(3).strip().lower()
                if ("motion" in l or "md" in l) and val in {"1", "true", "on", "enabled", "enable", "start"}:
                    motion_indices.add(idx)
            return motion_indices if motion_indices else None
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

    def _format_hikvision_time_value(self, text: str) -> str | None:
        """Normalize Hikvision date/time text for UI display."""
        if not text:
            return None
        raw = text.strip()
        if not raw:
            return None

        normalized = raw
        # Handle UTC shorthand that fromisoformat does not accept directly.
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"

        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        m = re.search(r"(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2})", raw)
        if m:
            return f"{m.group(1)} {m.group(2)}"
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
                if chan is None and track_id is not None and track_id >= 100:
                    chan = track_id // 100
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

    def _parse_hikvision_record_tracks_channel_modes(self, xml_text: str) -> dict[int, str]:
        try:
            root = ET.fromstring(xml_text)
            channel_modes: dict[int, str] = {}
            for elem in root.iter():
                tag = elem.tag
                if not (isinstance(tag, str) and tag.endswith('Track')):
                    continue
                chan = None
                track_id = None
                rec_mode = None
                schedule_modes = set()
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
                    elif lct == 'enable' or lct.endswith('trackenable'):
                        enable_flag = tx.lower() in ('true', '1')
                for node in elem.iter():
                    nt = node.tag
                    if not isinstance(nt, str):
                        continue
                    lnt = nt.lower()
                    if (
                        lnt.endswith('actionrecordingmode')
                        or lnt.endswith('scheduleactionrecordingmode')
                        or (lnt.endswith('recordingmode') and not lnt.endswith('defaultrecordingmode'))
                    ):
                        mode = (node.text or '').strip().upper()
                        if mode:
                            schedule_modes.add(mode)
                if chan is None and track_id is not None and track_id >= 100:
                    chan = track_id // 100
                if chan is None:
                    continue
                status = 'not-recording'
                if schedule_modes:
                    if any(mode in {'MR', 'MOTION'} or 'MOTION' in mode for mode in schedule_modes):
                        status = 'motion'
                    elif any(mode not in {'OFF', 'NONE', ''} for mode in schedule_modes):
                        status = 'recording'
                elif rec_mode is not None:
                    if rec_mode == 'MR':
                        status = 'motion'
                    elif rec_mode and rec_mode not in ('OFF', 'NONE', ''):
                        status = 'recording'
                elif enable_flag:
                    status = 'recording'
                channel_modes[chan] = status
            return channel_modes
        except Exception:
            return {}

    def _parse_hikvision_motion_detection_enabled(self, xml_text: str) -> bool | None:
        """Parse Hikvision motionDetection endpoint and return whether motion is enabled."""
        try:
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                tag = elem.tag
                if not isinstance(tag, str):
                    continue
                if tag.lower().endswith("enabled"):
                    txt = (elem.text or "").strip().lower()
                    if txt in ("true", "1"):
                        return True
                    if txt in ("false", "0"):
                        return False
            return None
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

    def _parse_hikvision_inputproxy_channels_status_channel_modes(self, xml_text: str) -> dict[int, str]:
        """Parse InputProxy channel status XML into per-channel recording modes."""
        try:
            root = ET.fromstring(xml_text)
            modes: dict[int, str] = {}
            for elem in root.iter():
                tag = elem.tag
                if not (isinstance(tag, str) and (tag.endswith('InputProxyChannelStatus') or tag.endswith('inputProxyChannelStatus') or tag.endswith('ChannelStatus'))):
                    continue
                chan_id = None
                is_connected = False
                is_recording = False
                for ch in elem:
                    ct = ch.tag
                    if not isinstance(ct, str):
                        continue
                    tx = (ch.text or '').strip().lower()
                    lct = ct.lower()
                    if lct.endswith('id') or lct.endswith('channelid') or lct.endswith('videoinputid') or lct.endswith('dynvideoinputchannelid'):
                        raw = (ch.text or '').strip()
                        if raw.isdigit():
                            chan_id = int(raw)
                    elif lct.endswith('online') or lct.endswith('connectstatus'):
                        if tx in {'online', 'connected', 'true', '1'}:
                            is_connected = True
                    elif lct.endswith('recording'):
                        if tx in {'started', 'on', 'true', '1', 'recording'}:
                            is_recording = True
                if chan_id is None:
                    continue
                if not is_connected:
                    modes[chan_id] = 'no-camera'
                elif is_recording:
                    modes[chan_id] = 'recording'
                else:
                    modes[chan_id] = 'not-recording'
            return modes
        except Exception:
            return {}

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

    def _merge_disk_activity_states(self, states: list[str]) -> str | None:
        clean = [str(s).strip().lower() for s in states if isinstance(s, str) and s.strip()]
        if not clean:
            return None
        if any(s == "working" for s in clean):
            return "working"
        if any(s == "normal" for s in clean):
            return "normal"
        if any(s == "idle" for s in clean):
            return "idle"
        return None

    def _classify_disk_activity_text(self, value: Any) -> str | None:
        text = str(value or "").strip().lower()
        if not text:
            return None
        active_tokens = (
            "working", "busy", "record", "write", "read", "format", "repair",
            "rebuild", "syncing", "initialize", "inprogress", "in_progress",
        )
        idle_tokens = (
            "idle", "standby", "sleep",
        )
        normal_tokens = (
            "normal", "ready", "online", "ok",
        )
        if any(tok in text for tok in active_tokens):
            return "working"
        if any(tok in text for tok in normal_tokens):
            return "normal"
        if any(tok in text for tok in idle_tokens):
            return "idle"
        return None

    def _classify_disk_activity_keyed(self, key: str, value: Any) -> str | None:
        lk = str(key or "").strip().lower()
        if not lk:
            return self._classify_disk_activity_text(value)

        # Boolean and numeric activity hints frequently used by NVR APIs.
        if isinstance(value, bool):
            if any(tok in lk for tok in ("busy", "work", "record", "write", "read", "sync", "format")):
                return "working" if value else "idle"
            if "idle" in lk:
                return "idle" if value else "normal"

        if isinstance(value, (int, float)):
            iv = int(value)
            if any(tok in lk for tok in ("busy", "work", "record", "write", "read", "sync", "format")):
                return "working" if iv > 0 else "idle"
            if "idle" in lk:
                return "idle" if iv > 0 else "normal"
            if "status" in lk or "state" in lk:
                # Common status coding in embedded APIs: 0=normal, 1=busy/working, 2=idle.
                if iv == 0:
                    return "normal"
                if iv == 1:
                    return "working"
                if iv == 2:
                    return "idle"

        return self._classify_disk_activity_text(value)

    def _collect_disk_activity_states_from_json(self, node: Any) -> list[str]:
        states: list[str] = []

        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                for key, val in obj.items():
                    lk = str(key).strip().lower()
                    if isinstance(val, (str, int, float, bool)):
                        if any(tok in lk for tok in ("status", "state", "busy", "work", "sync", "record", "rw", "idle", "read", "write", "format")):
                            s = self._classify_disk_activity_keyed(lk, val)
                            if s:
                                states.append(s)
                    if isinstance(val, (dict, list)):
                        walk(val)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(node)
        return states

    def _parse_milesight_disk_activity(self, text: str) -> str | None:
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if data is not None:
            states = self._collect_disk_activity_states_from_json(data)
            merged = self._merge_disk_activity_states(states)
            if merged:
                return merged

        # Fallback for key=value responses.
        lower_text = (text or "").lower()
        if not lower_text:
            return None
        if any(tok in lower_text for tok in ("working", "busy", "record", "write", "read", "format", "syncing")):
            return "working"
        if any(tok in lower_text for tok in ("normal", "ready", "online", "ok")):
            return "normal"
        if "idle" in lower_text:
            return "idle"
        return None

    def _parse_hikvision_storage_hdd_ids(self, xml_text: str) -> list[str]:
        ids: list[str] = []
        try:
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                tag = elem.tag
                if not (isinstance(tag, str) and tag.lower().endswith("hdd")):
                    continue
                for child in elem:
                    ctag = child.tag
                    if isinstance(ctag, str) and ctag.lower().endswith("id"):
                        val = (child.text or "").strip()
                        if val:
                            ids.append(val)
                            break
        except Exception:
            return []

        # Keep order but drop duplicates.
        out: list[str] = []
        seen: set[str] = set()
        for x in ids:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out

    def _parse_hikvision_storage_activity(self, xml_text: str) -> str | None:
        states: list[str] = []
        try:
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                tag = elem.tag
                if not isinstance(tag, str):
                    continue
                lt = tag.lower()
                if any(tok in lt for tok in ("status", "state", "sync", "work", "busy", "rw", "record")):
                    s = self._classify_disk_activity_keyed(lt, (elem.text or "").strip())
                    if s:
                        states.append(s)
        except Exception:
            return None
        return self._merge_disk_activity_states(states)

    def _parse_hikvision_sync_status_activity(self, text: str) -> str | None:
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if data is not None:
            states = self._collect_disk_activity_states_from_json(data)
            merged = self._merge_disk_activity_states(states)
            if merged:
                return merged
        return self._classify_disk_activity_text(text)

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

    def _parse_uniview_channel_detail_infos_connected_ids(self, text: str) -> set[int] | None:
        try:
            data = json.loads(text)
            obj = data
            if isinstance(obj, dict) and "Response" in obj and isinstance(obj["Response"], dict):
                obj = obj["Response"]
            if isinstance(obj, dict) and "Data" in obj and isinstance(obj["Data"], dict):
                obj = obj["Data"]
            detail = obj.get("DetailInfos") if isinstance(obj, dict) else None
            if not isinstance(detail, list) or not detail:
                return None

            ids: set[int] = set()
            for idx, item in enumerate(detail, start=1):
                if not isinstance(item, dict):
                    continue
                vals = {str(k).lower(): str(v).strip().lower() for k, v in item.items()}
                online = (
                    vals.get("status") in {"1", "2", "online", "connected", "true"}
                    or vals.get("online") in {"1", "true", "online", "connected"}
                    or vals.get("connectstatus") in {"1", "true", "online", "connected"}
                )
                if not online:
                    continue

                cid = None
                for key in ("id", "channelid", "channel", "chid"):
                    raw = item.get(key)
                    if isinstance(raw, int):
                        cid = raw
                        break
                    if isinstance(raw, str) and raw.strip().isdigit():
                        cid = int(raw.strip())
                        break
                if cid is None:
                    cid = idx
                ids.add(int(cid))
            return ids if ids else None
        except Exception:
            return None

    def _parse_uniview_time_value(self, text: str) -> str | None:
        """Parse Uniview time from XML or JSON bodies used by ISAPI/LAPI."""
        # XML first
        try:
            root = ET.fromstring(text)
            for elem in root.iter():
                tag = elem.tag
                if not isinstance(tag, str):
                    continue
                lt = tag.lower()
                if lt.endswith("localtime") or lt.endswith("time") or lt.endswith("devicetime") or lt.endswith("currentdevicetime"):
                    if elem.text and elem.text.strip():
                        return elem.text.strip()
        except Exception:
            pass

        # JSON fallback
        try:
            data = json.loads(text)

            # Special handling for LAPI time payload where DeviceTime is epoch seconds.
            try:
                resp = data.get("Response") if isinstance(data, dict) else None
                dnode = resp.get("Data") if isinstance(resp, dict) else None
                if isinstance(dnode, dict) and isinstance(dnode.get("DeviceTime"), (int, float)):
                    ts = int(dnode.get("DeviceTime"))
                    tz_text = dnode.get("TimeZone")
                    tzinfo = None
                    if isinstance(tz_text, str):
                        m = re.match(r"^GMT([+-])(\d{1,2}):(\d{2})$", tz_text.strip(), flags=re.IGNORECASE)
                        if m:
                            sign = 1 if m.group(1) == "+" else -1
                            hh = int(m.group(2))
                            mm = int(m.group(3))
                            tzinfo = timezone(sign * timedelta(hours=hh, minutes=mm))
                    if tzinfo is not None:
                        return datetime.fromtimestamp(ts, tz=tzinfo).strftime("%Y-%m-%d %H:%M:%S")
                    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    for key, value in node.items():
                        k = str(key).lower()
                        if isinstance(value, str) and value.strip() and (
                            k.endswith("localtime") or k.endswith("time") or k.endswith("devicetime") or k.endswith("currentdevicetime")
                        ):
                            return value.strip()
                        if isinstance(value, (dict, list)):
                            stack.append(value)
                elif isinstance(node, list):
                    for value in node:
                        if isinstance(value, (dict, list)):
                            stack.append(value)
        except Exception:
            pass
        return None

    def _parse_uniview_lapi_record_schedule_mode(self, text: str) -> str | None:
        """Parse Uniview LAPI record schedule response into channel mode.

        Endpoint shape: /LAPI/V1.0/Channels/{id}/Storage/Private/Schedule/Record/
        """
        try:
            data = json.loads(text)
            resp = data.get("Response") if isinstance(data, dict) else None
            dnode = resp.get("Data") if isinstance(resp, dict) else None
            if not isinstance(dnode, dict):
                return None

            enabled = int(dnode.get("Enabled", 0) or 0)
            if enabled != 1:
                return "not-recording"

            week = dnode.get("WeekPlan")
            days = week.get("Days") if isinstance(week, dict) else None
            if not isinstance(days, list):
                return "recording"

            has_recording = False
            has_motion = False
            for day in days:
                if not isinstance(day, dict):
                    continue
                sections = day.get("TimeSectionInfos")
                if not isinstance(sections, list):
                    continue
                for section in sections:
                    if not isinstance(section, dict):
                        continue
                    begin = str(section.get("Begin", "")).strip()
                    end = str(section.get("End", "")).strip()
                    if not begin or not end or begin == end:
                        continue
                    arming_type = int(section.get("ArmingType", 0) or 0)
                    if arming_type == 0:
                        has_recording = True
                    elif arming_type > 0:
                        has_motion = True

            if has_motion:
                return "motion"
            if has_recording:
                return "recording"
            return "not-recording"
        except Exception:
            return None
