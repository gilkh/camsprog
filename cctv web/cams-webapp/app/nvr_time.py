"""Verified ONVIF clock sync for recorders without a working native time API."""
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

from .nvr_accounts import _onvif_envelope
from .monitor import MonitorState


def read_device_clock(nvr):
    """Read a fresh clock for drift decisions; never use a cached monitoring value."""
    username, password = nvr.get("username"), nvr.get("password")
    if not username or not password:
        raise ValueError("Save the recorder login in Edit NVR first.")
    host = nvr["ip"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    base, auth = f"http://{host}", HTTPDigestAuth(username, password)
    vendor = (nvr.get("type") or "").strip().lower()
    if vendor in ("milesight", "milesight old"):
        helper = MonitorState()
        with requests.Session() as session:
            if not helper._milesight_web_login(session, nvr["ip"], username, password, timeout=6):
                raise ValueError("Recorder login failed.")
            response = helper._milesight_web_post_json(session, nvr["ip"], username, password, "/cgi/main/7000", payload="", timeout=6)
            if response.status_code != 200:
                raise ValueError("The recorder refused clock lookup.")
            value = response.json().get("serverTime")
    elif vendor in ("hikvision", "hik", "hickvision", "uniview", "unv"):
        path = "/LAPI/V1.0/System/TimeNTP" if vendor in ("uniview", "unv") else "/ISAPI/System/time"
        response = requests.get(base + path, auth=auth, timeout=6, allow_redirects=False)
        if response.status_code == 401:
            response = requests.get(base + path, auth=(username, password), timeout=6, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError("The recorder refused clock lookup.")
        if vendor in ("uniview", "unv"):
            epoch = response.json()["Response"]["Data"]["DeviceTime"]
            value = datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
        else:
            root = ET.fromstring(response.text)
            value = next((node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "localTime"), None)
    else:
        envelope = ET.fromstring(_onvif_envelope(username, password, "GetSystemDateAndTime"))
        envelope.remove(envelope.find("{http://www.w3.org/2003/05/soap-envelope}Header"))
        response = requests.post(base + "/onvif/device_service", data=ET.tostring(envelope), auth=auth, timeout=6,
                                 headers={"Content-Type": 'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime"'}, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError("ONVIF clock lookup is unavailable.")
        root = ET.fromstring(response.text)
        utc = root.find(".//{http://www.onvif.org/ver10/schema}UTCDateTime")
        if utc is None:
            raise ValueError("The recorder did not return a UTC clock.")
        def number(section, key):
            return int(utc.findtext(f"{{http://www.onvif.org/ver10/schema}}{section}/{{http://www.onvif.org/ver10/schema}}{key}"))
        value = datetime(number("Date", "Year"), number("Date", "Month"), number("Date", "Day"), number("Time", "Hour"), number("Time", "Minute"), number("Time", "Second"), tzinfo=timezone.utc).isoformat()
    if not isinstance(value, str) or clock_offset(value, datetime.now().astimezone()) is None:
        raise ValueError("The recorder returned an unreadable clock.")
    return value


def clock_offset(value, target):
    try:
        actual = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if actual.tzinfo is None:
            actual = actual.replace(tzinfo=target.tzinfo)
        return round((actual - target).total_seconds(), 3)
    except (TypeError, ValueError, AttributeError):
        return None


def clock_close(value, target, tolerance=15):
    offset = clock_offset(value, target)
    return offset is not None and abs(offset) <= tolerance


def sync_onvif_time(nvr):
    username, password = nvr.get("username"), nvr.get("password")
    if not username or not password:
        return False, "Save the recorder login in Edit NVR first."
    host = nvr["ip"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    url = f"http://{host}/onvif/device_service"
    tds = "http://www.onvif.org/ver10/device/wsdl"
    tt = "http://www.onvif.org/ver10/schema"
    auth = HTTPDigestAuth(username, password)
    device_created = None

    def call(operation, content=None):
        envelope = _onvif_envelope(username, password, operation, content, created=device_created)
        if operation == "GetSystemDateAndTime" and device_created is None:
            # ONVIF permits clock lookup before WS-Security time validation.
            root = ET.fromstring(envelope)
            header = root.find("{http://www.w3.org/2003/05/soap-envelope}Header")
            root.remove(header)
            envelope = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        response = requests.post(
            url, data=envelope,
            headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{tds}/{operation}"'},
            auth=auth, timeout=8, allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise ValueError("ONVIF refused access. Check the saved login and time-setting permission.")
        if response.status_code != 200:
            raise ValueError("ONVIF time service is unavailable or disabled on this recorder.")
        root = ET.fromstring(response.text)
        if root.find(".//{http://www.w3.org/2003/05/soap-envelope}Fault") is not None:
            raise ValueError("ONVIF rejected the time request. Check the recorder clock and account permissions.")
        result = root.find(f".//{{{tds}}}{operation}Response")
        if result is None:
            raise ValueError("The recorder returned an unexpected ONVIF time response.")
        return result

    def utc_time(root):
        utc = root.find(f".//{{{tt}}}UTCDateTime")
        if utc is None:
            raise ValueError("The recorder did not return its UTC clock for verification.")
        def number(section, key):
            return int(utc.findtext(f"{{{tt}}}{section}/{{{tt}}}{key}"))
        return datetime(number("Date", "Year"), number("Date", "Month"), number("Date", "Day"),
                        number("Time", "Hour"), number("Time", "Minute"), number("Time", "Second"), tzinfo=timezone.utc)

    try:
        current = call("GetSystemDateAndTime")
        # Align WS-Security with a drifting recorder before asking it to set UTC.
        device_created = utc_time(current).strftime("%Y-%m-%dT%H:%M:%SZ")
        now = datetime.now(timezone.utc)
        content = ET.Element("content")
        ET.SubElement(content, f"{{{tds}}}DateTimeType").text = "Manual"
        ET.SubElement(content, f"{{{tds}}}DaylightSavings").text = current.findtext(f".//{{{tt}}}DaylightSavings", "false")
        utc = ET.SubElement(content, f"{{{tds}}}UTCDateTime")
        for section, values in (("Time", {"Hour": now.hour, "Minute": now.minute, "Second": now.second}),
                                ("Date", {"Year": now.year, "Month": now.month, "Day": now.day})):
            node = ET.SubElement(utc, f"{{{tt}}}{section}")
            for key, value in values.items():
                ET.SubElement(node, f"{{{tt}}}{key}").text = str(value)
        call("SetSystemDateAndTime", content)
        device_created = None
        actual = utc_time(call("GetSystemDateAndTime"))
        offset = clock_offset(actual.isoformat(), datetime.now(timezone.utc))
        if abs(offset) > 15:
            return False, "The recorder accepted the request, but its clock is still incorrect."
        return True, {"device_time": actual.isoformat(), "offset_seconds": offset}
    except requests.Timeout:
        return False, "The recorder's ONVIF time service timed out."
    except ValueError as exc:
        return False, str(exc)
    except (requests.RequestException, ET.ParseError, TypeError):
        return False, "ONVIF time sync could not be verified. Check connectivity, ONVIF access, and administrator permissions."
