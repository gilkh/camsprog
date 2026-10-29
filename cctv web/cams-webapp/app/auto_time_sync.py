"""Opt-in automatic correction of recorder clocks with fresh drift checks."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
import time

from .nvr_time import clock_offset, read_device_clock


DEFAULT_POLICY = {"enabled": False, "scope": "all", "nvr_ips": [],
                  "threshold_seconds": 180, "check_interval_seconds": 60}


def validate_policy(value):
    if not isinstance(value, dict):
        raise ValueError("Automatic time sync settings must be an object.")
    policy = {**DEFAULT_POLICY, **value}
    if type(policy["enabled"]) is not bool:
        raise ValueError("Automatic time sync enabled must be true or false.")
    if policy["scope"] not in ("all", "selected"):
        raise ValueError("Choose all NVRs or selected NVRs.")
    ips = policy["nvr_ips"]
    if not isinstance(ips, list) or any(not isinstance(ip, str) or not ip.strip() for ip in ips):
        raise ValueError("Selected NVRs must be a list of IP addresses.")
    policy["nvr_ips"] = list(dict.fromkeys(ip.strip() for ip in ips))
    if policy["enabled"] and policy["scope"] == "selected" and not policy["nvr_ips"]:
        raise ValueError("Select at least one NVR for automatic time sync.")
    for key, minimum, maximum in (("threshold_seconds", 30, 86400), ("check_interval_seconds", 60, 86400)):
        value = policy[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be a whole number between {minimum} and {maximum} seconds.")
    return {key: policy[key] for key in DEFAULT_POLICY}


class AutoTimeSync:
    def __init__(self, get_settings, get_nvrs, sync, record=None, read_clock=read_device_clock):
        self.get_settings, self.get_nvrs, self.sync = get_settings, get_nvrs, sync
        self.record, self.read_clock = record, read_clock
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.lock = threading.Lock()
        self.status = {"running": False, "last_checked_at": None, "next_check_at": None, "results": []}
        self.last_attempt = {}
        self.last_read_error = {}
        self.thread = None

    def policy(self):
        try:
            policy = validate_policy(self.get_settings().get("auto_time_sync", {}))
            with self.lock:
                self.status.pop("settings_error", None)
            return policy
        except Exception:
            with self.lock:
                self.status["settings_error"] = "Automatic sync is paused because its saved settings could not be read."
            return copy.deepcopy(DEFAULT_POLICY)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.status)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="auto-time-sync", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def check_one(self, nvr, policy):
        ip = nvr["ip"]
        result = {"ip": ip, "name": nvr.get("name") or ip, "checked_at": time.time()}
        if self.stop_event.is_set():
            return {**result, "state": "skipped", "message": "Automatic time sync stopped."}
        if time.monotonic() - self.last_read_error.get(ip, float('-inf')) < 300:
            return {**result, "state": "cooldown", "message": "Waiting five minutes before retrying clock lookup."}
        try:
            actual = self.read_clock(nvr)
            self.last_read_error.pop(ip, None)
            offset = clock_offset(actual, datetime.now().astimezone())
            if offset is None:
                raise ValueError("Unreadable clock")
            result.update(device_time=actual, offset_seconds=offset)
            # Re-read policy before any write, so switching off or deselecting takes effect mid-cycle.
            current = self.policy()
            if not current["enabled"] or (current["scope"] == "selected" and ip not in current["nvr_ips"]) or self.stop_event.is_set():
                return {**result, "state": "skipped", "message": "Automatic sync is off or this NVR is no longer selected."}
            if abs(offset) <= current["threshold_seconds"]:
                return {**result, "state": "within_threshold", "message": "Clock is within the configured threshold; no sync needed."}
            if time.monotonic() - self.last_attempt.get(ip, float('-inf')) < 300:
                return {**result, "state": "cooldown", "message": "Waiting five minutes before retrying this recorder."}
            self.last_attempt[ip] = time.monotonic()
            reply = self.sync(ip)
            if hasattr(reply, "body"):
                import json
                reply = json.loads(reply.body)
            verified = reply.get("verified") is True
            return {**result, "state": "synced" if verified else "failed", "verified": verified,
                    "after_offset_seconds": reply.get("offset_seconds"),
                    "message": reply.get("message") or "Clock correction could not be verified."}
        except Exception:
            self.last_read_error[ip] = time.monotonic()
            # Do not forward exceptions that could contain authentication or raw device payloads.
            return {**result, "state": "failed", "verified": False,
                    "message": "Could not read or sync this recorder. Check connectivity, login, and permissions."}

    def run_once(self):
        policy = self.policy()
        if not policy["enabled"] or self.stop_event.is_set():
            return []
        targets = [nvr for nvr in self.get_nvrs() if nvr.get("ip") and
                   (policy["scope"] == "all" or nvr["ip"] in policy["nvr_ips"])]
        with self.lock:
            self.status["running"] = True
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda nvr: self.check_one(nvr, policy), targets))
            checked = time.time()
            with self.lock:
                self.status.update(results=results, last_checked_at=checked,
                                   next_check_at=checked + policy["check_interval_seconds"])
            if self.record:
                for result in results:
                    try:
                        self.record(result)
                    except Exception:
                        pass
            return results
        finally:
            with self.lock:
                self.status["running"] = False

    def run(self):
        previous = None
        next_check = 0
        while not self.stop_event.is_set():
            policy = self.policy()
            if policy != previous:
                next_check = 0
                previous = policy
            if policy["enabled"] and time.monotonic() >= next_check:
                try:
                    self.run_once()
                except Exception:
                    pass
                next_check = time.monotonic() + policy["check_interval_seconds"]
            if not policy["enabled"]:
                with self.lock:
                    self.status["next_check_at"] = None
            self.wake_event.wait(5)
            self.wake_event.clear()
