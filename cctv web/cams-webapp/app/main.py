import os
import json
import html
import csv
import io
import threading
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .monitor import MonitorState, ping_ip
from pydantic import BaseModel, Field
from typing import Optional
from pymongo import MongoClient
from bson import ObjectId
import requests
from requests.auth import HTTPDigestAuth
from datetime import datetime, timezone
import time
import xml.etree.ElementTree as ET
import re
import copy

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

monitor = MonitorState(poll_interval=15)


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
    # Try utf-8-sig first (handles BOM produced by some editors/old code),
    # fall back to plain utf-8, then latin-1 as last resort.
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(CONFIG_PATH, "r", encoding=enc) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            cfg = data.get("config")
            if isinstance(cfg, dict):
                return cfg
            return {}
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception:
            return {}
    return {}


def _save_settings_to_file(update: dict):
    try:
        data = {}
        if os.path.exists(CONFIG_PATH):
            # Read with BOM-tolerant encoding
            for enc in ("utf-8-sig", "utf-8", "latin-1"):
                try:
                    with open(CONFIG_PATH, "r", encoding=enc) as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
                except Exception:
                    break
        cfg = data.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
        for key, value in update.items():
            cfg[key] = value
        data["config"] = cfg
        tmp_path = CONFIG_PATH + ".tmp"
        # Write without BOM so new files are plain utf-8
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
        # Do not let null DB fields erase valid values loaded from file.
        for k, v in db_s.items():
            if k == "_id":
                continue
            if v is None and k in merged and merged.get(k) is not None:
                continue
            merged[k] = v
    merged.pop("_id", None)

    # Backward compatibility for older config files.
    if not merged.get("smtp_host") and merged.get("smtp_server"):
        merged["smtp_host"] = merged.get("smtp_server")
    if not merged.get("smtp_host_2") and merged.get("smtp_server_2"):
        merged["smtp_host_2"] = merged.get("smtp_server_2")

    merged["smtp_to"] = _normalize_smtp_to(merged.get("smtp_to"))
    return merged


def _parse_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _get_smtp_targets(settings: dict, mode: str = "auto") -> list[tuple[str, int]]:
    targets = []
    primary_host = (settings.get("smtp_host") or "").strip()
    primary_port = _parse_int(settings.get("smtp_port"))
    secondary_host = (settings.get("smtp_host_2") or "").strip()
    secondary_port = _parse_int(settings.get("smtp_port_2"))

    primary = (primary_host, primary_port) if primary_host and primary_port else None
    secondary = (secondary_host, secondary_port) if secondary_host and secondary_port else None

    selected_mode = (mode or "auto").strip().lower()
    if selected_mode == "primary":
        if primary:
            targets.append(primary)
        return targets
    if selected_mode == "secondary":
        if secondary:
            targets.append(secondary)
        return targets

    if primary:
        targets.append(primary)
    if secondary and secondary not in targets:
        targets.append(secondary)

    # both: attempt both configured targets; auto: ordered failover list.
    return targets


def _smtp_probe_target(settings: dict, host: str, port: int) -> tuple[bool, str | None]:
    try:
        import smtplib

        username = settings.get("smtp_username") or None
        password = settings.get("smtp_password") or None
        use_tls = bool(settings.get("smtp_tls") or False)

        client = smtplib.SMTP(host, port, timeout=8)
        try:
            if use_tls:
                client.starttls()
            if username and password:
                client.login(username, password)
        finally:
            try:
                client.quit()
            except Exception:
                pass
        return True, None
    except Exception as e:
        return False, str(e)


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


def _format_local_datetime(epoch: int | None) -> str:
        if epoch is None:
                epoch = int(time.time())
        try:
                return datetime.fromtimestamp(int(epoch)).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
                return str(epoch)


def _friendly_alert_type(alert_type: str | None) -> str:
        mapping = {
                "nvr_offline": "NVR Offline",
                "recording_expected_mismatch": "Recording Count Mismatch",
                "nvr_time_drift": "NVR Time Drift",
                "channel_not_recording": "Channel Not Recording",
        }
        key = (alert_type or "").strip()
        return mapping.get(key, key.replace("_", " ").title() or "Alert")


WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


def _find_nvr_by_ip(ip: str) -> dict | None:
    for item in monitor.get_snapshot():
        if item.get("ip") == ip:
            return item
    return None


def _safe_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def _normalize_hhmm(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    txt = value.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", txt)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 24 or mm < 0 or mm > 59:
        return None
    if hh == 24 and mm != 0:
        return None
    return f"{hh:02d}:{mm:02d}"


def _weekday_index_from_text(value: str | None) -> int | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    aliases = {
        "monday": 0,
        "mon": 0,
        "tuesday": 1,
        "tue": 1,
        "tues": 1,
        "wednesday": 2,
        "wed": 2,
        "thursday": 3,
        "thu": 3,
        "thur": 3,
        "thurs": 3,
        "friday": 4,
        "fri": 4,
        "saturday": 5,
        "sat": 5,
        "sunday": 6,
        "sun": 6,
    }
    return aliases.get(key)


def _milesight_action_to_mode(action_type: int | None) -> str:
    val = _safe_int(action_type, 0) or 0
    if val == 1:
        return "recording"
    if val == 2:
        return "motion"
    if val > 2:
        return "event"
    return "off"


def _mode_to_milesight_action(mode: str | None) -> int:
    m = (mode or "off").strip().lower()
    if m == "recording":
        return 1
    if m == "motion":
        return 2
    if m == "event":
        return 3
    return 0


def _parse_milesight_week(schedule_payload: dict) -> list[dict]:
    week: list[dict] = []
    schedule = schedule_payload.get("schedule") if isinstance(schedule_payload, dict) else None
    if not isinstance(schedule, list):
        return week

    for idx, day_item in enumerate(schedule):
        day_name = WEEKDAY_NAMES[idx] if idx < len(WEEKDAY_NAMES) else f"Day {idx + 1}"
        entries: list[dict] = []
        if isinstance(day_item, dict):
            if _safe_int(day_item.get("wholedayEnable"), 0) == 1:
                mode = _milesight_action_to_mode(_safe_int(day_item.get("wholedayActionType"), 0))
                entries.append({"start": "00:00", "end": "24:00", "mode": mode})

            plans = day_item.get("plans")
            if isinstance(plans, list):
                for p in plans:
                    start_val = None
                    end_val = None
                    action_val = None
                    if isinstance(p, dict):
                        start_val = (
                            p.get("start")
                            or p.get("startTime")
                            or p.get("beginTime")
                            or p.get("begin")
                        )
                        end_val = (
                            p.get("end")
                            or p.get("endTime")
                            or p.get("stopTime")
                            or p.get("stop")
                        )
                        action_val = (
                            p.get("actionType")
                            or p.get("type")
                            or p.get("recordType")
                            or p.get("mode")
                        )
                    elif isinstance(p, list) and len(p) >= 3:
                        start_val = p[0]
                        end_val = p[1]
                        action_val = p[2]

                    start_hhmm = _normalize_hhmm(str(start_val)) if start_val is not None else None
                    end_hhmm = _normalize_hhmm(str(end_val)) if end_val is not None else None
                    if not start_hhmm or not end_hhmm:
                        continue
                    entries.append(
                        {
                            "start": start_hhmm,
                            "end": end_hhmm,
                            "mode": _milesight_action_to_mode(_safe_int(action_val, 0)),
                        }
                    )

        week.append({"day_index": idx, "day": day_name, "entries": entries})
    return week


def _build_milesight_schedule_payload(base_payload: dict, week: list[dict]) -> dict:
    out = dict(base_payload if isinstance(base_payload, dict) else {})
    src_schedule = out.get("schedule")
    if not isinstance(src_schedule, list):
        src_schedule = [{} for _ in range(7)]

    while len(src_schedule) < 7:
        src_schedule.append({})

    by_day: dict[int, list[dict]] = {}
    for day_row in week or []:
        if not isinstance(day_row, dict):
            continue
        d = _safe_int(day_row.get("day_index"), None)
        if d is None:
            d = _weekday_index_from_text(day_row.get("day"))
        if d is None or d < 0 or d > 6:
            continue
        entries = day_row.get("entries")
        if isinstance(entries, list):
            by_day[d] = entries

    for idx in range(7):
        existing = src_schedule[idx]
        if not isinstance(existing, dict):
            existing = {}

        entries = by_day.get(idx, [])
        normalized_entries: list[dict] = []
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            start_hhmm = _normalize_hhmm(ent.get("start"))
            end_hhmm = _normalize_hhmm(ent.get("end"))
            if not start_hhmm or not end_hhmm:
                continue
            normalized_entries.append(
                {
                    "start": f"{start_hhmm}:00",
                    "end": f"{end_hhmm}:00",
                    "actionType": _mode_to_milesight_action(ent.get("mode")),
                }
            )

        existing["wholedayEnable"] = 0
        existing["wholedayActionType"] = 0
        existing["plans"] = normalized_entries
        src_schedule[idx] = existing

    out["schedule"] = src_schedule
    return out


def _xml_local_name(tag: str) -> str:
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _extract_hikvision_track_ids(tracks_xml: str, channel: int) -> list[int]:
    ids: list[int] = []
    try:
        root = ET.fromstring(tracks_xml)
        all_track_ids: list[int] = []
        for elem in root.iter():
            if _xml_local_name(elem.tag).lower() != "track":
                continue
            track_id = None
            src_channel = None
            for child in elem:
                name = _xml_local_name(child.tag).lower()
                txt = (child.text or "").strip()
                if name == "id":
                    track_id = _safe_int(txt, None)
                elif name == "srcchannel":
                    src_channel = _safe_int(txt, None)
            if track_id is None:
                continue
            all_track_ids.append(track_id)
            if src_channel == channel:
                ids.append(track_id)
                continue
            # Many Hikvision firmwares expose track IDs as <channel><stream>, e.g. 101/102 for channel 1.
            if track_id >= 100 and (track_id // 100) == channel:
                ids.append(track_id)
    except Exception:
        return []
    dedup = sorted(set(ids))
    if dedup:
        return dedup

    # Last-resort fallback for variants that return one track without explicit mapping fields.
    all_dedup = sorted(set(all_track_ids))
    if len(all_dedup) == 1:
        return all_dedup
    return []


def _hikvision_mode_to_ui(mode_text: str | None) -> str:
    m = (mode_text or "").strip().upper()
    if m in {"CMR", "CONTINUOUS", "TIMING", "TIMER"}:
        return "recording"
    if m in {"MR", "MOTION"}:
        return "motion"
    if m in {"ER", "AR", "EVENT", "ALARM", "ALLEVENT", "ALL_EVENT"}:
        return "event"
    if m in {"OFF", "NONE", ""}:
        return "off"
    return m.lower()


def _ui_mode_to_hikvision(mode_text: str | None) -> str:
    m = (mode_text or "off").strip().lower()
    if m == "recording":
        return "CMR"
    if m == "motion":
        return "MOTION"
    if m == "event":
        return "AllEvent"
    return "OFF"


def _entries_to_week(entries: list[dict]) -> list[dict]:
    grouped: dict[int, list[dict]] = {i: [] for i in range(7)}
    for row in entries or []:
        if not isinstance(row, dict):
            continue
        d = _safe_int(row.get("day_index"), None)
        if d is None:
            d = _weekday_index_from_text(row.get("day"))
        if d is None or d < 0 or d > 6:
            continue
        start = _normalize_hhmm(row.get("start")) or "00:00"
        end = _normalize_hhmm(row.get("end")) or "24:00"
        mode = (row.get("mode") or "off").strip().lower()
        grouped[d].append({"start": start, "end": end, "mode": mode})

    week: list[dict] = []
    for d in range(7):
        week.append({"day_index": d, "day": WEEKDAY_NAMES[d], "entries": grouped[d]})
    return week


def _apply_hikvision_week_to_track_xml(base_xml: str, week: list[dict]) -> str:
    root = ET.fromstring(base_xml)

    schedule_parent = None
    schedule_blocks = []
    for parent in root.iter():
        kids = list(parent)
        blocks = [k for k in kids if _xml_local_name(k.tag).lower() == "scheduleblock"]
        if blocks:
            schedule_parent = parent
            schedule_blocks = blocks
            break

    if schedule_parent is None or not schedule_blocks:
        raise ValueError("Unable to locate Hikvision schedule blocks in track XML")

    template = schedule_blocks[0]
    template_children = list(template)

    day_tag = None
    start_tag = None
    end_tag = None
    mode_tag = None
    for child in template_children:
        lname = _xml_local_name(child.tag).lower()
        if day_tag is None and lname in {"dayofweek", "weekday", "day"}:
            day_tag = child.tag
        if start_tag is None and lname in {"starttime", "begintime", "start"}:
            start_tag = child.tag
        if end_tag is None and lname in {"endtime", "stoptime", "stop", "end"}:
            end_tag = child.tag
        if mode_tag is None and (lname in {"scheduleactionrecordingmode", "defaultrecordingmode", "recordingmode", "mode"} or "recordingmode" in lname):
            mode_tag = child.tag

    if day_tag is None:
        day_tag = "DayOfWeek"
    if start_tag is None:
        start_tag = "StartTime"
    if end_tag is None:
        end_tag = "EndTime"
    if mode_tag is None:
        mode_tag = "ScheduleActionRecordingMode"

    normalized: list[dict] = []
    for day_row in week or []:
        if not isinstance(day_row, dict):
            continue
        d = _safe_int(day_row.get("day_index"), None)
        if d is None:
            d = _weekday_index_from_text(day_row.get("day"))
        if d is None or d < 0 or d > 6:
            continue
        entries = day_row.get("entries")
        if not isinstance(entries, list):
            continue
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            start = _normalize_hhmm(ent.get("start"))
            end = _normalize_hhmm(ent.get("end"))
            if not start or not end:
                continue
            normalized.append(
                {
                    "day": WEEKDAY_NAMES[d],
                    "start": f"{start}:00",
                    "end": f"{end}:00",
                    "mode": _ui_mode_to_hikvision(ent.get("mode")),
                }
            )

    if not normalized:
        for d in range(7):
            normalized.append(
                {
                    "day": WEEKDAY_NAMES[d],
                    "start": "00:00:00",
                    "end": "24:00:00",
                    "mode": "OFF",
                }
            )

    for old in schedule_blocks:
        schedule_parent.remove(old)

    for item in normalized:
        block = copy.deepcopy(template)

        def _set_first(tag_name: str, value: str) -> bool:
            for node in block.iter():
                if _xml_local_name(node.tag).lower() == _xml_local_name(tag_name).lower():
                    node.text = value
                    return True
            return False

        if not _set_first(day_tag, item["day"]):
            ET.SubElement(block, day_tag).text = item["day"]
        if not _set_first(start_tag, item["start"]):
            ET.SubElement(block, start_tag).text = item["start"]
        if not _set_first(end_tag, item["end"]):
            ET.SubElement(block, end_tag).text = item["end"]
        if not _set_first(mode_tag, item["mode"]):
            ET.SubElement(block, mode_tag).text = item["mode"]

        schedule_parent.append(block)

    return ET.tostring(root, encoding="unicode")


def _parse_hikvision_track_schedule(track_xml: str) -> list[dict]:
    rows: list[dict] = []
    try:
        root = ET.fromstring(track_xml)
    except Exception:
        return rows

    # Track-level fallback mode for firmwares that do not expose explicit schedule blocks.
    track_default_mode = None
    for elem in root.iter():
        if _xml_local_name(elem.tag).lower() != "track":
            continue
        for child in elem:
            lname = _xml_local_name(child.tag).lower()
            if lname in {"defaultrecordingmode", "recordingmode", "scheduleactionrecordingmode"} or "recordingmode" in lname:
                track_default_mode = _hikvision_mode_to_ui((child.text or "").strip())
                break
        if track_default_mode:
            break

    for elem in root.iter():
        if _xml_local_name(elem.tag).lower() != "scheduleblock":
            continue

        day_index = None
        start = None
        end = None
        mode = None

        for child in elem.iter():
            lname = _xml_local_name(child.tag).lower()
            txt = (child.text or "").strip()
            if not txt:
                continue
            if day_index is None and lname in {"dayofweek", "weekday", "day"}:
                day_index = _weekday_index_from_text(txt)
            if start is None and lname in {"starttime", "begintime", "start"}:
                start = _normalize_hhmm(txt)
            if end is None and lname in {"endtime", "stoptime", "stop", "end"}:
                end = _normalize_hhmm(txt)
            if mode is None and (
                lname in {"scheduleactionrecordingmode", "defaultrecordingmode", "recordingmode", "mode"}
                or "recordingmode" in lname
            ):
                mode = _hikvision_mode_to_ui(txt)

        if day_index is None:
            continue
        if not start:
            start = "00:00"
        if not end:
            end = "24:00"
        rows.append(
            {
                "day_index": day_index,
                "day": WEEKDAY_NAMES[day_index],
                "start": start,
                "end": end,
                "mode": mode or "recording",
            }
        )

    if rows:
        return rows

    # If no schedule blocks exist, represent the track default as full-day schedule.
    fallback_mode = (track_default_mode or "off").strip().lower()
    return [
        {
            "day_index": d,
            "day": WEEKDAY_NAMES[d],
            "start": "00:00",
            "end": "24:00",
            "mode": fallback_mode,
        }
        for d in range(7)
    ]


def _severity_order(severity: str | None) -> int:
        sev = (severity or "warning").lower()
        if sev == "critical":
                return 0
        if sev == "warning":
                return 1
        if sev == "non-critical":
                return 2
        return 3


def _build_alert_email_content(due_alerts: list[dict], generated_at_ts: int) -> tuple[str, str, str]:
        total = len(due_alerts)
        critical_count = sum(1 for a in due_alerts if (a.get("severity") or "").lower() == "critical")
        warning_count = sum(1 for a in due_alerts if (a.get("severity") or "").lower() == "warning")
        non_critical_count = sum(1 for a in due_alerts if (a.get("severity") or "").lower() == "non-critical")

        ordered_alerts = sorted(
                due_alerts,
                key=lambda d: (
                        _severity_order(d.get("severity")),
                        str(d.get("nvr_name") or d.get("nvr_ip") or ""),
                        str(d.get("channel") or ""),
                        str(d.get("alert_type") or ""),
                ),
        )

        subject = f"Cams Alerts: {total} active"

        text_lines = [
                f"Cams WebApp Alert Summary ({_format_local_datetime(generated_at_ts)})",
                "",
                f"Total active alerts in this email: {total}",
                f"Critical: {critical_count}",
                f"Warning: {warning_count}",
                f"Non-critical: {non_critical_count}",
                "",
                "Situation overview:",
                "The monitoring system detected active conditions that may affect camera health, recording continuity, or time accuracy.",
                "Please review the incidents below and acknowledge them in the Logs page after verification.",
                "",
                "Active incidents:",
        ]

        row_html = []
        for idx, a in enumerate(ordered_alerts, start=1):
                sev = (a.get("severity") or "warning").lower()
                sev_label = sev.upper()
                nvr_name = str(a.get("nvr_name") or a.get("nvr_ip") or "Unknown")
                nvr_ip = str(a.get("nvr_ip") or "-")
                alert_type = _friendly_alert_type(a.get("alert_type"))
                channel = a.get("channel")
                channel_label = str(channel) if channel is not None else "-"
                message = str(a.get("message") or alert_type)

                text_lines.append(
                        f"{idx}. [{sev_label}] {nvr_name} ({nvr_ip}) | Type: {alert_type} | Channel: {channel_label} | {message}"
                )

                sev_bg = "#fef2f2"
                sev_fg = "#991b1b"
                if sev == "warning":
                        sev_bg = "#fff7ed"
                        sev_fg = "#9a3412"
                elif sev == "non-critical":
                        sev_bg = "#f1f5f9"
                        sev_fg = "#334155"

                row_html.append(
                        """
                        <tr>
                            <td style=\"padding:12px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;font-size:13px;color:#0f172a;\">{idx}</td>
                            <td style=\"padding:12px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;\"><span style=\"display:inline-block;padding:4px 8px;border-radius:999px;font-weight:700;font-size:11px;letter-spacing:0.03em;background:{sev_bg};color:{sev_fg};\">{sev_label}</span></td>
                            <td style=\"padding:12px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;font-size:13px;color:#0f172a;\">{nvr_name}<div style=\"color:#64748b;font-size:12px;margin-top:2px;\">{nvr_ip}</div></td>
                            <td style=\"padding:12px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;font-size:13px;color:#0f172a;\">{alert_type}</td>
                            <td style=\"padding:12px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;font-size:13px;color:#0f172a;\">{channel_label}</td>
                            <td style=\"padding:12px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;font-size:13px;color:#0f172a;\">{message}</td>
                        </tr>
                        """.format(
                                idx=idx,
                                sev_bg=sev_bg,
                                sev_fg=sev_fg,
                                sev_label=html.escape(sev_label),
                                nvr_name=html.escape(nvr_name),
                                nvr_ip=html.escape(nvr_ip),
                                alert_type=html.escape(alert_type),
                                channel_label=html.escape(channel_label),
                                message=html.escape(message),
                        ).strip()
                )

        text_lines.extend(
                [
                        "",
                        "Recommended next actions:",
                        "1) Check NVR reachability and power/network status for offline devices.",
                        "2) Validate recording schedules, disk state, and channel input health.",
                        "3) Confirm NVR time synchronization and timezone settings.",
                        "",
                        "This message was generated automatically by Cams WebApp.",
                ]
        )
        text_body = "\n".join(text_lines)

        html_body = """
<!DOCTYPE html>
<html>
    <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>{subject}</title>
    </head>
    <body style=\"margin:0;padding:0;background:#f8fafc;font-family:Segoe UI,Arial,sans-serif;color:#0f172a;\">
        <table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" width=\"100%\" style=\"background:#f8fafc;padding:24px 12px;\">
            <tr>
                <td align=\"center\">
                    <table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" width=\"760\" style=\"width:100%;max-width:760px;background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;\">
                        <tr>
                            <td style=\"padding:24px 24px 18px;background:linear-gradient(120deg,#0f172a,#1d4ed8);color:#ffffff;\">
                                <div style=\"font-size:12px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.9;\">Cams WebApp</div>
                                <h1 style=\"margin:8px 0 0;font-size:24px;line-height:1.2;\">Active Alert Summary</h1>
                                <p style=\"margin:10px 0 0;font-size:14px;opacity:0.95;\">Generated at {generated_at}</p>
                            </td>
                        </tr>
                        <tr>
                            <td style=\"padding:20px 24px 8px;\">
                                <p style=\"margin:0 0 14px;font-size:14px;line-height:1.6;color:#1e293b;\">
                                    The monitoring system detected active conditions that may affect camera health, recording continuity, or time accuracy.
                                    Review the incident list below and acknowledge alerts after investigation.
                                </p>
                            </td>
                        </tr>
                        <tr>
                            <td style=\"padding:0 24px 16px;\">
                                <table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" width=\"100%\">
                                    <tr>
                                        <td style=\"padding:10px;border:1px solid #e2e8f0;border-radius:10px;background:#f8fafc;text-align:center;\">
                                            <div style=\"font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.06em;\">Total</div>
                                            <div style=\"font-size:24px;font-weight:700;color:#0f172a;margin-top:4px;\">{total}</div>
                                        </td>
                                        <td style=\"width:8px;\"></td>
                                        <td style=\"padding:10px;border:1px solid #fee2e2;border-radius:10px;background:#fef2f2;text-align:center;\">
                                            <div style=\"font-size:11px;color:#991b1b;text-transform:uppercase;letter-spacing:0.06em;\">Critical</div>
                                            <div style=\"font-size:24px;font-weight:700;color:#7f1d1d;margin-top:4px;\">{critical_count}</div>
                                        </td>
                                        <td style=\"width:8px;\"></td>
                                        <td style=\"padding:10px;border:1px solid #ffedd5;border-radius:10px;background:#fff7ed;text-align:center;\">
                                            <div style=\"font-size:11px;color:#9a3412;text-transform:uppercase;letter-spacing:0.06em;\">Warning</div>
                                            <div style=\"font-size:24px;font-weight:700;color:#9a3412;margin-top:4px;\">{warning_count}</div>
                                        </td>
                                        <td style=\"width:8px;\"></td>
                                        <td style=\"padding:10px;border:1px solid #e2e8f0;border-radius:10px;background:#f8fafc;text-align:center;\">
                                            <div style=\"font-size:11px;color:#334155;text-transform:uppercase;letter-spacing:0.06em;\">Non-critical</div>
                                            <div style=\"font-size:24px;font-weight:700;color:#334155;margin-top:4px;\">{non_critical_count}</div>
                                        </td>
                                    </tr>
                                </table>
                            </td>
                        </tr>
                        <tr>
                            <td style=\"padding:0 24px 4px;\">
                                <h2 style=\"margin:0;font-size:16px;color:#0f172a;\">Incident Details</h2>
                            </td>
                        </tr>
                        <tr>
                            <td style=\"padding:10px 24px 20px;\">
                                <table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" width=\"100%\" style=\"border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;\">
                                    <thead>
                                        <tr style=\"background:#f1f5f9;\">
                                            <th align=\"left\" style=\"padding:10px;border-bottom:1px solid #cbd5e1;font-size:12px;text-transform:uppercase;color:#334155;letter-spacing:0.04em;\">#</th>
                                            <th align=\"left\" style=\"padding:10px;border-bottom:1px solid #cbd5e1;font-size:12px;text-transform:uppercase;color:#334155;letter-spacing:0.04em;\">Severity</th>
                                            <th align=\"left\" style=\"padding:10px;border-bottom:1px solid #cbd5e1;font-size:12px;text-transform:uppercase;color:#334155;letter-spacing:0.04em;\">NVR</th>
                                            <th align=\"left\" style=\"padding:10px;border-bottom:1px solid #cbd5e1;font-size:12px;text-transform:uppercase;color:#334155;letter-spacing:0.04em;\">Type</th>
                                            <th align=\"left\" style=\"padding:10px;border-bottom:1px solid #cbd5e1;font-size:12px;text-transform:uppercase;color:#334155;letter-spacing:0.04em;\">Channel</th>
                                            <th align=\"left\" style=\"padding:10px;border-bottom:1px solid #cbd5e1;font-size:12px;text-transform:uppercase;color:#334155;letter-spacing:0.04em;\">What Happened</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {rows}
                                    </tbody>
                                </table>
                            </td>
                        </tr>
                        <tr>
                            <td style=\"padding:0 24px 24px;\">
                                <div style=\"padding:14px 16px;border:1px solid #dbeafe;background:#eff6ff;border-radius:10px;color:#1e3a8a;font-size:13px;line-height:1.6;\">
                                    <strong>Recommended actions:</strong><br />
                                    1) Check NVR connectivity and power/network condition.<br />
                                    2) Validate recording schedule, disk health, and channel input status.<br />
                                    3) Confirm NVR clock synchronization and timezone settings.
                                </div>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
</html>
        """.format(
                subject=html.escape(subject),
                generated_at=html.escape(_format_local_datetime(generated_at_ts)),
                total=total,
                critical_count=critical_count,
                warning_count=warning_count,
                non_critical_count=non_critical_count,
                rows="\n".join(row_html),
        )

        return subject, text_body, html_body


def _build_test_email_content(generated_at_ts: int, smtp_mode: str, recipient_count: int) -> tuple[str, str, str]:
        subject = "Cams WebApp SMTP Test"
        mode_label = (smtp_mode or "auto").strip().lower()
        if mode_label not in {"auto", "primary", "secondary", "both"}:
                mode_label = "auto"

        text_body = "\n".join(
                [
                        "Cams WebApp SMTP Test Message",
                        "",
                        f"Generated at: {_format_local_datetime(generated_at_ts)}",
                        f"SMTP mode: {mode_label}",
                        f"Recipient count: {recipient_count}",
                        "",
                        "This is a test notification to confirm that email delivery is working.",
                        "No production alert is active for this message.",
                ]
        )

        html_body = """
<!DOCTYPE html>
<html>
    <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>{subject}</title>
    </head>
    <body style=\"margin:0;padding:0;background:#f8fafc;font-family:Segoe UI,Arial,sans-serif;color:#0f172a;\">
        <table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" width=\"100%\" style=\"background:#f8fafc;padding:24px 12px;\">
            <tr>
                <td align=\"center\">
                    <table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" width=\"680\" style=\"width:100%;max-width:680px;background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;\">
                        <tr>
                            <td style=\"padding:24px;background:linear-gradient(120deg,#0b3b2e,#0e7490);color:#ffffff;\">
                                <div style=\"font-size:12px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.9;\">Cams WebApp</div>
                                <h1 style=\"margin:8px 0 0;font-size:24px;line-height:1.2;\">SMTP Test Successful</h1>
                                <p style=\"margin:10px 0 0;font-size:14px;opacity:0.95;\">Generated at {generated_at}</p>
                            </td>
                        </tr>
                        <tr>
                            <td style=\"padding:20px 24px;\">
                                <p style=\"margin:0 0 14px;font-size:14px;line-height:1.6;color:#1e293b;\">
                                    This is a test notification to confirm your SMTP configuration is working from Cams WebApp.
                                    No incident is active for this email.
                                </p>
                                <table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" width=\"100%\" style=\"border:1px solid #e2e8f0;border-radius:10px;\">
                                    <tr>
                                        <td style=\"padding:10px 12px;border-bottom:1px solid #e2e8f0;font-size:13px;color:#334155;width:45%;\">SMTP mode</td>
                                        <td style=\"padding:10px 12px;border-bottom:1px solid #e2e8f0;font-size:13px;font-weight:600;color:#0f172a;\">{smtp_mode}</td>
                                    </tr>
                                    <tr>
                                        <td style=\"padding:10px 12px;font-size:13px;color:#334155;\">Recipient count</td>
                                        <td style=\"padding:10px 12px;font-size:13px;font-weight:600;color:#0f172a;\">{recipient_count}</td>
                                    </tr>
                                </table>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
</html>
        """.format(
                subject=html.escape(subject),
                generated_at=html.escape(_format_local_datetime(generated_at_ts)),
                smtp_mode=html.escape(mode_label),
                recipient_count=int(recipient_count),
        )

        return subject, text_body, html_body


def _build_current_alerts(snapshot: list, settings: dict) -> dict:
    now_ts = int(time.time())
    tolerance_sec = _parse_int(settings.get("time_tolerance"))
    if tolerance_sec is None or tolerance_sec < 30:
        # Fall back to config file directly in case merged settings missed it
        try:
            file_s = _load_settings_from_file()
            tolerance_sec = _parse_int(file_s.get("time_tolerance"))
        except Exception:
            tolerance_sec = None
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
            # Only alert if drift is more than the time tolerance threshold
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


def _send_alert_email(settings: dict, recipients: list[str], subject: str, body: str, mode: str = "auto", html_body: str | None = None):
    selected_mode = (mode or "auto").strip().lower()
    targets = _get_smtp_targets(settings, selected_mode)
    username = settings.get("smtp_username") or None
    password = settings.get("smtp_password") or None
    auth_provided = bool(username and password)
    use_tls = bool(settings.get("smtp_tls") or False)
    from_addr = settings.get("smtp_from") or None
    if not targets or not from_addr or not recipients:
        return False, "SMTP target(s), from, or recipients missing", {
            "mode": selected_mode,
            "auth_provided": auth_provided,
            "auth_used": False,
            "targets": [{"host": h, "port": p} for (h, p) in targets],
        }

    try:
        import smtplib
        from email.message import EmailMessage

        errors = []
        successes = []
        auth_used = False
        for host, port in targets:
            client = None
            try:
                client = smtplib.SMTP(host, port, timeout=12)
                client.ehlo_or_helo_if_needed()
                if use_tls:
                    client.starttls()
                    client.ehlo_or_helo_if_needed()
                if username and password:
                    client.login(username, password)
                    auth_used = True
                target_deliveries = []
                for to_addr in recipients:
                    msg = EmailMessage()
                    msg["Subject"] = subject
                    msg["From"] = from_addr
                    msg["To"] = to_addr
                    msg.set_content(body)
                    if html_body:
                        msg.add_alternative(html_body, subtype="html")
                    raw = msg.as_string()

                    mail_code, mail_resp = client.mail(from_addr)
                    if int(mail_code) != 250:
                        raise RuntimeError(f"MAIL FROM rejected ({mail_code}): {mail_resp}")

                    rcpt_code, rcpt_resp = client.rcpt(to_addr)
                    if int(rcpt_code) not in (250, 251):
                        raise RuntimeError(f"RCPT TO {to_addr} rejected ({rcpt_code}): {rcpt_resp}")

                    data_code, data_resp = client.data(raw)
                    if int(data_code) != 250:
                        raise RuntimeError(f"DATA rejected ({data_code}): {data_resp}")

                    target_deliveries.append(
                        {
                            "recipient": to_addr,
                            "mail_code": int(mail_code),
                            "rcpt_code": int(rcpt_code),
                            "data_code": int(data_code),
                            "data_response": str(data_resp.decode("utf-8", errors="replace") if isinstance(data_resp, (bytes, bytearray)) else data_resp),
                        }
                    )

                successes.append({"host": host, "port": port, "deliveries": target_deliveries})
                if selected_mode != "both":
                    return True, None, {
                        "mode": selected_mode,
                        "host": host,
                        "port": port,
                        "deliveries": target_deliveries,
                        "auth_provided": auth_provided,
                        "auth_used": auth_used,
                        "targets": [{"host": h, "port": p} for (h, p) in targets],
                    }
            except Exception as e:
                errors.append(f"{host}:{port} -> {e}")
            finally:
                if client is not None:
                    try:
                        client.quit()
                    except Exception:
                        pass
        if selected_mode == "both" and successes:
            return True, None, {
                "mode": "both",
                "targets": successes,
                "auth_provided": auth_provided,
                "auth_used": auth_used,
            }
        return False, " | ".join(errors), {
            "mode": selected_mode,
            "targets": [{"host": h, "port": p} for (h, p) in targets],
            "auth_provided": auth_provided,
            "auth_used": auth_used,
        }
    except Exception as e:
        return False, str(e), {
            "mode": selected_mode,
            "auth_provided": auth_provided,
            "auth_used": False,
            "targets": [{"host": h, "port": p} for (h, p) in targets],
        }


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
            interval = 600
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

        email_enabled = settings.get("email_enabled") != False
        email_nvr_offline = settings.get("email_nvr_offline") != False
        email_nvr_time_drift = settings.get("email_nvr_time_drift") != False
        email_recording_mismatch = settings.get("email_recording_mismatch") != False
        email_channel_not_recording = settings.get("email_channel_not_recording") != False

        to_send = []
        to_skip = []

        for alert in due_alerts:
            t = alert.get("alert_type")
            is_enabled = email_enabled
            if is_enabled:
                if t == "nvr_offline":
                    is_enabled = email_nvr_offline
                elif t == "nvr_time_drift":
                    is_enabled = email_nvr_time_drift
                elif t == "recording_expected_mismatch":
                    is_enabled = email_recording_mismatch
                elif t == "channel_not_recording":
                    is_enabled = email_channel_not_recording
            
            if is_enabled:
                to_send.append(alert)
            else:
                to_skip.append(alert)

        if to_skip:
            skip_ids = [x.get("_id") for x in to_skip if x.get("_id")]
            alerts_col.update_many(
                {"_id": {"$in": skip_ids}},
                {"$set": {"last_emailed_at": now_ts, "last_email_status": "skipped"}},
            )

        if not to_send:
            return

        subject, text_body, html_body = _build_alert_email_content(to_send, now_ts)

        ok, err, smtp_used = _send_alert_email(
            settings,
            recipients,
            subject,
            text_body,
            html_body=html_body,
        )
        send_ids = [x.get("_id") for x in to_send if x.get("_id")]
        alerts_col.update_many(
            {"_id": {"$in": send_ids}},
            {"$set": {"last_emailed_at": now_ts, "last_email_status": "success" if ok else "failed"}},
        )
        email_col.insert_one(
            {
                "created_at": now_ts,
                "subject": subject,
                "to": recipients,
                "alert_ids": send_ids,
                "count": len(send_ids),
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
        ri = int(s.get("refresh_interval") or 15)
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
    return templates.TemplateResponse(request, "settings.html")


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    return templates.TemplateResponse(request, "calendar.html")


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    return templates.TemplateResponse(request, "logs.html")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            # Initial render; client script fetches data
            "nvrs": [],
        },
    )


@app.get("/api/nvrs")
def api_nvrs():
    return JSONResponse(monitor.get_snapshot())


@app.get("/api/events/stream")
async def api_events(request: Request):
    """
    Server-Sent Events endpoint that streams NVR data whenever it updates.
    """
    async def event_generator():
        # Send initial state
        data = monitor.get_snapshot()
        yield f"data: {json.dumps(data)}\n\n"
        
        while True:
            if await request.is_disconnected():
                break
            
            # Wait for the monitor to finish a refresh cycle
            changed = await asyncio.to_thread(monitor.wait_for_change, timeout=30.0)
            
            if await request.is_disconnected():
                break
                
            if changed:
                data = monitor.get_snapshot()
                yield f"data: {json.dumps(data)}\n\n"
            else:
                # Heartbeat to keep connection alive
                yield ": heartbeat\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/diagnostics")
def api_get_diagnostics():
    """Returns monitoring performance metrics."""
    with monitor.lock:
        # Use getattr with defaults in case the server hasn't restarted with the new MonitorState attributes
        duration = getattr(monitor, "last_refresh_duration", 0.0)
        count = getattr(monitor, "refresh_count", 0)
        finish = getattr(monitor, "last_refresh_finish", 0.0)
        
        return {
            "last_refresh_duration": round(duration, 2),
            "refresh_count": count,
            "last_refresh_finish": finish,
            "poll_interval": getattr(monitor, "poll_interval", 15),
            "nvr_count": len(getattr(monitor, "nvrs", [])),
            "is_overrun": duration > getattr(monitor, "poll_interval", 15),
            "timestamp": time.time()
        }


@app.get("/api/nvrs/export")
def api_nvrs_export():
    rows = monitor.get_snapshot()

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["name", "ip", "type"])

    for nvr in rows:
        if not isinstance(nvr, dict):
            continue
        writer.writerow([
            str(nvr.get("name") or ""),
            str(nvr.get("ip") or ""),
            str(nvr.get("type") or "Unknown"),
        ])

    filename = f"nvr_list_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )

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


@app.get("/api/nvrs/{ip}/record_schedule")
def api_get_record_schedule(ip: str, channel: int = 1):
    nvr = _find_nvr_by_ip(ip)
    if not nvr:
        return JSONResponse({"status": "error", "message": "NVR not found"}, status_code=404)

    vendor = (nvr.get("type") or "").strip()
    username = nvr.get("username") or "admin"
    password = nvr.get("password") or "admin"
    channel_int = _safe_int(channel, None)
    if channel_int is None or channel_int < 1:
        return JSONResponse({"status": "error", "message": "channel must be >= 1"}, status_code=400)

    if vendor in ("Milesight", "Milesight Old"):
        try:
            session = requests.Session()
            web_auth_ok = monitor._milesight_web_login(session, ip, username, password, timeout=6)

            def post_milesight(path: str, body):
                nonlocal web_auth_ok
                last_resp = None

                def keepalive_online_user() -> None:
                    if not web_auth_ok:
                        return
                    sid = (session.headers.get("X-Milesight-SessionId") or "").strip()
                    keepalive_queries = ["action=set.user.online_user&action=1"]
                    if sid:
                        keepalive_queries.insert(0, f"action=set.user.online_user&action=1&sessionId={sid}")
                    for q in keepalive_queries:
                        try:
                            monitor._milesight_web_get(session, ip, username, password, "/sdk.cgi", q, timeout=5)
                        except Exception:
                            pass

                keepalive_online_user()
                # Prefer native Milesight digest header flow used by web UI scripts.
                try:
                    resp = monitor._milesight_web_post_json(session, ip, username, password, path, payload=body, timeout=8)
                    last_resp = resp
                    if resp.status_code == 200:
                        return resp
                    if resp.status_code == 401:
                        web_auth_ok = monitor._milesight_web_login(session, ip, username, password, timeout=6)
                        keepalive_online_user()
                        resp2 = monitor._milesight_web_post_json(session, ip, username, password, path, payload=body, timeout=8)
                        last_resp = resp2
                        if resp2.status_code == 200:
                            return resp2
                except Exception:
                    pass

                # Some Milesight Old firmwares reject /cgi/main/1000 login but still accept direct auth on 6040.
                for auth in ((username, password), HTTPDigestAuth(username, password)):
                    try:
                        resp = requests.post(f"http://{ip}{path}", auth=auth, json=body, timeout=8)
                        last_resp = resp
                        if resp.status_code == 200:
                            return resp
                    except Exception:
                        pass
                return last_resp

            zero_based_channel = channel_int - 1
            r = post_milesight("/cgi/main/6040", zero_based_channel)
            if r is None:
                return JSONResponse(
                    {"status": "error", "message": "Milesight schedule read request failed"},
                    status_code=502,
                )
            if r.status_code != 200:
                return JSONResponse(
                    {"status": "error", "message": f"Milesight schedule read failed ({r.status_code})"},
                    status_code=r.status_code,
                )

            try:
                body = r.json()
            except Exception:
                body = None
            if not isinstance(body, dict):
                return JSONResponse({"status": "error", "message": "Milesight schedule payload is not JSON"}, status_code=502)

            week = _parse_milesight_week(body)
            return {
                "status": "ok",
                "vendor": "Milesight",
                "channel": channel_int,
                "channel_device": zero_based_channel,
                "week": week,
                "raw": body,
            }
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    if vendor == "Hikvision":
        try:
            auth = HTTPDigestAuth(username, password)
            tracks_url = f"http://{ip}/ISAPI/ContentMgmt/record/tracks"
            tr = requests.get(tracks_url, auth=auth, timeout=8)
            if tr.status_code != 200:
                return JSONResponse(
                    {"status": "error", "message": f"Hikvision track list read failed ({tr.status_code})"},
                    status_code=tr.status_code,
                )

            track_ids = _extract_hikvision_track_ids(tr.text, channel_int)
            if not track_ids:
                return JSONResponse(
                    {
                        "status": "error",
                        "message": f"No recording track mapped to channel {channel_int}",
                        "channel": channel_int,
                    },
                    status_code=404,
                )

            track_id = track_ids[0]
            track_url = f"http://{ip}/ISAPI/ContentMgmt/record/tracks/{track_id}"
            rr = requests.get(track_url, auth=auth, timeout=8)
            if rr.status_code != 200:
                return JSONResponse(
                    {"status": "error", "message": f"Hikvision track read failed ({rr.status_code})"},
                    status_code=rr.status_code,
                )

            parsed = _parse_hikvision_track_schedule(rr.text)
            week = _entries_to_week(parsed)

            # Some Hikvision variants return a single full-day block; treat it as whole-week plan.
            non_empty_days = [row for row in week if isinstance(row, dict) and isinstance(row.get("entries"), list) and row.get("entries")]
            if len(non_empty_days) == 1:
                only = non_empty_days[0]
                ent = only.get("entries", [{}])[0] if only.get("entries") else {}
                if (
                    isinstance(ent, dict)
                    and str(ent.get("start") or "") == "00:00"
                    and str(ent.get("end") or "") == "24:00"
                ):
                    mode = str(ent.get("mode") or "off")
                    week = [
                        {
                            "day_index": i,
                            "day": WEEKDAY_NAMES[i],
                            "entries": [{"start": "00:00", "end": "24:00", "mode": mode}],
                        }
                        for i in range(7)
                    ]

            return {
                "status": "ok",
                "vendor": "Hikvision",
                "channel": channel_int,
                "track_id": track_id,
                "track_ids": track_ids,
                "entries": parsed,
                "week": week,
                "raw_xml": rr.text,
            }
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    return JSONResponse(
        {"status": "error", "message": f"Schedule editing not supported for vendor '{vendor or 'Unknown'}'"},
        status_code=400,
    )


@app.post("/api/nvrs/{ip}/record_schedule")
def api_set_record_schedule(ip: str, payload: dict):
    nvr = _find_nvr_by_ip(ip)
    if not nvr:
        return JSONResponse({"status": "error", "message": "NVR not found"}, status_code=404)

    vendor = (nvr.get("type") or "").strip()
    username = nvr.get("username") or "admin"
    password = nvr.get("password") or "admin"
    channel_int = _safe_int(payload.get("channel"), None)
    if channel_int is None or channel_int < 1:
        return JSONResponse({"status": "error", "message": "channel must be >= 1"}, status_code=400)

    if vendor in ("Milesight", "Milesight Old"):
        week = payload.get("week")
        if not isinstance(week, list):
            return JSONResponse({"status": "error", "message": "week must be a list"}, status_code=400)
        try:
            session = requests.Session()
            web_auth_ok = monitor._milesight_web_login(session, ip, username, password, timeout=6)

            def post_milesight(path: str, body):
                nonlocal web_auth_ok
                last_resp = None

                def keepalive_online_user() -> None:
                    if not web_auth_ok:
                        return
                    sid = (session.headers.get("X-Milesight-SessionId") or "").strip()
                    keepalive_queries = ["action=set.user.online_user&action=1"]
                    if sid:
                        keepalive_queries.insert(0, f"action=set.user.online_user&action=1&sessionId={sid}")
                    for q in keepalive_queries:
                        try:
                            monitor._milesight_web_get(session, ip, username, password, "/sdk.cgi", q, timeout=5)
                        except Exception:
                            pass

                keepalive_online_user()
                try:
                    resp = monitor._milesight_web_post_json(session, ip, username, password, path, payload=body, timeout=10)
                    last_resp = resp
                    if resp.status_code == 200:
                        return resp
                    if resp.status_code == 401:
                        web_auth_ok = monitor._milesight_web_login(session, ip, username, password, timeout=6)
                        keepalive_online_user()
                        resp2 = monitor._milesight_web_post_json(session, ip, username, password, path, payload=body, timeout=10)
                        last_resp = resp2
                        if resp2.status_code == 200:
                            return resp2
                except Exception:
                    pass
                for auth in ((username, password), HTTPDigestAuth(username, password)):
                    try:
                        resp = requests.post(f"http://{ip}{path}", auth=auth, json=body, timeout=10)
                        last_resp = resp
                        if resp.status_code == 200:
                            return resp
                    except Exception:
                        pass
                return last_resp

            zero_based_channel = channel_int - 1
            read_resp = post_milesight("/cgi/main/6040", zero_based_channel)
            if read_resp is None:
                return JSONResponse(
                    {"status": "error", "message": "Milesight schedule read request failed"},
                    status_code=502,
                )
            if read_resp.status_code != 200:
                return JSONResponse(
                    {"status": "error", "message": f"Milesight schedule read failed ({read_resp.status_code})"},
                    status_code=read_resp.status_code,
                )

            try:
                base_payload = read_resp.json()
            except Exception:
                base_payload = None
            if not isinstance(base_payload, dict):
                return JSONResponse({"status": "error", "message": "Milesight schedule payload is not JSON"}, status_code=502)

            write_payload = _build_milesight_schedule_payload(base_payload, week)
            write_resp = post_milesight("/cgi/main/6041", write_payload)
            if write_resp is None:
                return JSONResponse(
                    {"status": "error", "message": "Milesight schedule write request failed"},
                    status_code=502,
                )
            if write_resp.status_code != 200:
                return JSONResponse(
                    {"status": "error", "message": f"Milesight schedule write failed ({write_resp.status_code})"},
                    status_code=write_resp.status_code,
                )

            return {
                "status": "ok",
                "vendor": "Milesight",
                "channel": channel_int,
                "channel_device": zero_based_channel,
                "body_sample": (write_resp.text or "")[:300],
            }
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    if vendor == "Hikvision":
        raw_xml = payload.get("raw_xml")
        week = payload.get("week")

        track_id = _safe_int(payload.get("track_id"), None)
        if track_id is None:
            try:
                auth = HTTPDigestAuth(username, password)
                tracks_url = f"http://{ip}/ISAPI/ContentMgmt/record/tracks"
                tr = requests.get(tracks_url, auth=auth, timeout=8)
                if tr.status_code == 200:
                    ids = _extract_hikvision_track_ids(tr.text, channel_int)
                    if ids:
                        track_id = ids[0]
            except Exception:
                pass
        if track_id is None:
            return JSONResponse({"status": "error", "message": "Unable to resolve Hikvision track_id"}, status_code=400)

        try:
            auth = HTTPDigestAuth(username, password)
            track_url = f"http://{ip}/ISAPI/ContentMgmt/record/tracks/{track_id}"

            if isinstance(week, list):
                current_resp = requests.get(track_url, auth=auth, timeout=8)
                if current_resp.status_code != 200:
                    return JSONResponse(
                        {
                            "status": "error",
                            "message": f"Hikvision track read failed ({current_resp.status_code})",
                        },
                        status_code=current_resp.status_code,
                    )
                raw_xml = _apply_hikvision_week_to_track_xml(current_resp.text, week)

            if not isinstance(raw_xml, str) or not raw_xml.strip():
                return JSONResponse(
                    {
                        "status": "error",
                        "message": "week or raw_xml is required for Hikvision schedule updates",
                    },
                    status_code=400,
                )

            # Browser UI writes full TrackList to /ISAPI/ContentMgmt/record/tracks.
            save_xml = raw_xml.strip()
            if "<TrackList" not in save_xml:
                save_xml = f"<?xml version='1.0' encoding='utf-8'?><TrackList>{save_xml}</TrackList>"

            save_url = f"http://{ip}/ISAPI/ContentMgmt/record/tracks"
            put_resp = requests.put(
                save_url,
                auth=auth,
                data=save_xml.encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=10,
            )
            if put_resp.status_code not in (200, 201, 204):
                return JSONResponse(
                    {
                        "status": "error",
                        "message": f"Hikvision schedule write failed ({put_resp.status_code})",
                        "body": (put_resp.text or "")[:500],
                    },
                    status_code=put_resp.status_code,
                )
            return {
                "status": "ok",
                "vendor": "Hikvision",
                "channel": channel_int,
                "track_id": track_id,
            }
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    return JSONResponse(
        {"status": "error", "message": f"Schedule editing not supported for vendor '{vendor or 'Unknown'}'"},
        status_code=400,
    )


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
    # Build response field-by-field so a bad value in one field never blocks the others.
    out: dict = {}
    try:
        s = _get_merged_settings()
    except Exception:
        s = {}

    # Refresh interval
    try:
        out["refresh_interval"] = int(s.get("refresh_interval") or monitor.poll_interval or 60)
    except Exception:
        out["refresh_interval"] = int(monitor.poll_interval or 60)

    # SMTP fields
    for key in ("smtp_host", "smtp_host_2", "smtp_username", "smtp_password", "smtp_from"):
        try:
            out[key] = s.get(key)
        except Exception:
            out[key] = None
    for key in ("smtp_port", "smtp_port_2"):
        try:
            val = s.get(key)
            out[key] = int(val) if val is not None else None
        except Exception:
            out[key] = None
    try:
        out["smtp_tls"] = bool(s.get("smtp_tls") or False)
    except Exception:
        out["smtp_tls"] = False
    try:
        out["smtp_to"] = _normalize_smtp_to(s.get("smtp_to"))
    except Exception:
        out["smtp_to"] = []

    # Alert email interval
    try:
        out["alert_email_interval_seconds"] = int(s.get("alert_email_interval_seconds") or 600)
    except Exception:
        out["alert_email_interval_seconds"] = 600

    # Time drift threshold — read from merged settings (file + DB); fall back to file directly.
    try:
        raw_tol = s.get("time_tolerance")
        if raw_tol is None:
            # Not in merged (DB+file) — try file alone as last resort
            file_s = _load_settings_from_file()
            raw_tol = file_s.get("time_tolerance")
        tol = int(raw_tol) if raw_tol is not None else 120
        out["time_tolerance"] = max(30, tol)
    except Exception:
        out["time_tolerance"] = 120

    # Email notification toggles
    for key, default in (
        ("email_enabled", True),
        ("email_nvr_offline", True),
        ("email_nvr_time_drift", True),
        ("email_recording_mismatch", True),
        ("email_channel_not_recording", True),
    ):
        try:
            out[key] = bool(s[key] if key in s else default)
        except Exception:
            out[key] = default

    return JSONResponse(out)


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
        for key in ("email_enabled", "email_nvr_offline", "email_nvr_time_drift", "email_recording_mismatch", "email_channel_not_recording"):
            if key in payload:
                update[key] = bool(payload.get(key))
        if "alert_email_interval_seconds" in payload:
            try:
                v = int(payload.get("alert_email_interval_seconds"))
                if v >= 60:
                    update["alert_email_interval_seconds"] = v
            except Exception:
                pass
        if "time_tolerance" in payload:
            try:
                v = int(payload.get("time_tolerance"))
                if v >= 30:
                    update["time_tolerance"] = v
                    # Mirror to monitor so it takes effect immediately without restart.
                    try:
                        monitor.time_tolerance = v
                    except Exception:
                        pass
            except Exception:
                pass
        if update:
            try:
                app.state.db["settings"].update_one({"_id": "global"}, {"$set": update}, upsert=True)
            except Exception:
                pass
            # Keep legacy key names in file for backward compatibility.
            update_for_file = dict(update)
            if "smtp_host" in update:
                update_for_file["smtp_server"] = update.get("smtp_host")
            if "smtp_host_2" in update:
                update_for_file["smtp_server_2"] = update.get("smtp_host_2")
            _save_settings_to_file(update_for_file)
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


@app.get("/api/backup")
def api_backup():
    try:
        db = app.state.db
        data = {
            "settings": list(db["settings"].find({})),
            "nvrs": list(db["nvrs"].find({})),
            "alerts": list(db["alerts"].find({})),
            "emails": list(db["emails"].find({})),
            "exported_at": datetime.now(timezone.utc).isoformat()
        }

        def clean_data(obj):
            if isinstance(obj, list):
                return [clean_data(x) for x in obj]
            if isinstance(obj, dict):
                return {k: clean_data(v) for k, v in obj.items()}
            if isinstance(obj, ObjectId):
                return str(obj)
            return obj

        json_data = clean_data(data)

        # Ensure backup directory exists in the project root
        backup_dir = os.path.join(PROJECT_ROOT, "backup")
        os.makedirs(backup_dir, exist_ok=True)

        filename = f"cams_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(backup_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, separators=(",", ":"))

        return {
            "status": "ok",
            "message": f"Backup saved successfully to backup folder as {filename}",
            "filename": filename
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/restore")
async def api_restore(request: Request):
    try:
        data = await request.json()
        db = app.state.db

        if not isinstance(data, dict) or "nvrs" not in data or "settings" not in data:
            return JSONResponse({"status": "error", "message": "Invalid backup format"}, status_code=400)

        def restore_col(name, items):
            if not isinstance(items, list):
                return
            db[name].delete_many({})
            if items:
                prepared_items = []
                for item in items:
                    if "_id" in item and isinstance(item["_id"], str):
                        try:
                            if len(item["_id"]) == 24:
                                item["_id"] = ObjectId(item["_id"])
                        except:
                            pass
                    prepared_items.append(item)
                db[name].insert_many(prepared_items)

        restore_col("settings", data.get("settings"))
        restore_col("nvrs", data.get("nvrs"))
        restore_col("alerts", data.get("alerts"))
        restore_col("emails", data.get("emails"))

        # Reload monitor state from DB
        with monitor.lock:
            monitor.nvrs = list(db["nvrs"].find({}))
            monitor._write_back()

        return {"status": "ok", "message": "Database restored successfully"}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


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


@app.post("/api/nvrs/{ip}/shutdown")
def api_shutdown_nvr(ip: str):
    nvr = None
    for x in monitor.get_snapshot():
        if x.get("ip") == ip:
            nvr = x
            break
    if not nvr:
        return JSONResponse({"status": "error", "message": "NVR not found"}, status_code=404)

    vendor = (nvr.get("type") or "").strip()
    vendor_norm = vendor.lower()
    username = nvr.get("username") or "admin"
    password = nvr.get("password") or "admin"

    def _ok_status(code: int) -> bool:
        return 200 <= code < 300

    def _request_with_fallback(method: str, url: str, timeout: int = 8, headers: dict | None = None, data=None, json_body=None):
        auth_attempts = [HTTPDigestAuth(username, password), (username, password)]
        last_resp = None
        for auth in auth_attempts:
            try:
                resp = requests.request(
                    method=method,
                    url=url,
                    auth=auth,
                    headers=headers,
                    data=data,
                    json=json_body,
                    timeout=timeout,
                )
                last_resp = resp
                if _ok_status(resp.status_code):
                    return resp
            except Exception:
                continue
        return last_resp

    def _verify_stays_offline(target_ip: str, grace_seconds: int = 10, observe_seconds: int = 35, poll_seconds: int = 5) -> tuple[bool, str | None]:
        # Distinguish true shutdown from reboot: reboot usually returns online during this window.
        try:
            time.sleep(max(0, grace_seconds))
            checks = max(1, observe_seconds // max(1, poll_seconds))
            for _ in range(checks):
                if ping_ip(target_ip, timeout_ms=1200):
                    return False, "Device came back online. This model likely supports reboot only via API, while full shutdown is local-screen only."
                time.sleep(max(1, poll_seconds))
            return True, None
        except Exception:
            return False, "Unable to verify shutdown state"

    def _finalize_shutdown_success() -> tuple[bool, JSONResponse | dict]:
        off_ok, off_msg = _verify_stays_offline(ip)
        monitor.refresh_once()
        if off_ok:
            return True, {"status": "ok", "message": "Shutdown command sent and device stayed offline"}
        return False, JSONResponse({"status": "error", "message": off_msg or "Shutdown verification failed"}, status_code=409)

    try:
        attempts = []

        try_milesight = vendor_norm in ("milesight", "milesight old") or vendor_norm == ""
        try_hikvision = vendor_norm in ("hikvision", "hickvision", "hik") or vendor_norm == ""
        try_uniview = vendor_norm in ("uniview", "unv") or vendor_norm == ""

        if try_milesight:
            session = requests.Session()
            web_auth_ok = monitor._milesight_web_login(session, ip, username, password, timeout=6)
            if web_auth_ok:
                try:
                    r = monitor._milesight_web_get(session, ip, username, password, "/sdk.cgi", "action=set.system.poweroff", timeout=6)
                    attempts.append(("GET", "/sdk.cgi?action=set.system.poweroff", r.status_code))
                    if _ok_status(r.status_code):
                        ok, payload = _finalize_shutdown_success()
                        return payload
                except Exception:
                    pass

            for path in (
                "/sdk.cgi?action=set.system.poweroff",
                "/V1.0/sdk.cgi?action=set.system.poweroff",
            ):
                url = f"http://{ip}{path}"
                resp = _request_with_fallback("GET", url, timeout=6)
                if resp is not None:
                    attempts.append(("GET", path, resp.status_code))
                    if _ok_status(resp.status_code):
                        ok, payload = _finalize_shutdown_success()
                        return payload

        if try_hikvision:
            for method, path in (
                ("PUT", "/ISAPI/System/poweroff"),
                ("PUT", "/ISAPI/System/shutdown"),
                ("POST", "/ISAPI/System/poweroff"),
                ("POST", "/ISAPI/System/shutdown"),
            ):
                url = f"http://{ip}{path}"
                resp = _request_with_fallback(method, url, timeout=8)
                if resp is not None:
                    attempts.append((method, path, resp.status_code))
                    if _ok_status(resp.status_code):
                        ok, payload = _finalize_shutdown_success()
                        return payload

        if try_uniview:
            for method, path in (
                ("PUT", "/LAPI/V1.0/System/Shutdown"),
                ("PUT", "/LAPI/V1.0/System/Poweroff"),
                ("POST", "/LAPI/V1.0/System/Shutdown"),
                ("POST", "/LAPI/V1.0/System/Poweroff"),
                ("PUT", "/ISAPI/System/poweroff"),
                ("POST", "/ISAPI/System/poweroff"),
            ):
                url = f"http://{ip}{path}"
                resp = _request_with_fallback(method, url, timeout=8)
                if resp is not None:
                    attempts.append((method, path, resp.status_code))
                    if _ok_status(resp.status_code):
                        ok, payload = _finalize_shutdown_success()
                        return payload

        if attempts:
            if try_milesight:
                milesight_poweroff = [a for a in attempts if "set.system.poweroff" in a[1]]
                if milesight_poweroff and all(int(a[2]) == 400 for a in milesight_poweroff):
                    return JSONResponse(
                        {
                            "status": "error",
                            "message": "Milesight firmware rejected remote poweroff (HTTP 400). This model likely supports shutdown only from local screen/menu.",
                        },
                        status_code=409,
                    )
            attempt_text = "; ".join([f"{m} {p} -> {s}" for (m, p, s) in attempts])
            return JSONResponse({"status": "error", "message": f"Shutdown command failed ({attempt_text})"}, status_code=502)
        if vendor_norm:
            return JSONResponse({"status": "error", "message": f"No shutdown endpoint available for vendor '{vendor}'"}, status_code=502)
        return JSONResponse({"status": "error", "message": "No shutdown endpoint available for this NVR (type unknown)"}, status_code=502)
    except Exception:
        return JSONResponse({"status": "error", "message": "Shutdown request failed"}, status_code=500)


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


@app.get("/api/calendar/meta")
def api_calendar_meta():
    try:
        return JSONResponse(monitor.get_calendar_meta())
    except Exception:
        return JSONResponse({"status": "ok", "baseline_ts": None})


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


@app.post("/api/logs/unack")
def api_logs_unack(payload: dict):
    """Unacknowledge an alert by alert_id."""
    try:
        alert_id = (payload or {}).get("alert_id")
        if not alert_id:
            return JSONResponse({"status": "error", "message": "alert_id is required"}, status_code=400)
        db = app.state.db
        found = db["alerts"].find_one({"_id": alert_id, "status": "active"})
        if not found:
            return JSONResponse({"status": "error", "message": "Active alert not found"}, status_code=404)
        db["alerts"].update_one(
            {"_id": alert_id},
            {"$set": {"acknowledged": False, "acknowledged_at": None}},
        )
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to unacknowledge alert"}, status_code=500)


@app.post("/api/logs/email_history/clear")
def api_logs_email_history_clear(payload: dict):
    """Clear email history. If email_id provided, clears single entry. Otherwise clears all."""
    try:
        email_id = (payload or {}).get("email_id")
        db = app.state.db
        if email_id:
            # Delete single entry by _id (convert to ObjectId if needed)
            from bson.objectid import ObjectId
            try:
                obj_id = ObjectId(email_id) if len(str(email_id)) == 24 else email_id
            except Exception:
                obj_id = email_id
            result = db["email_events"].delete_one({"_id": obj_id})
            if result.deleted_count == 0:
                return JSONResponse({"status": "error", "message": "Email entry not found"}, status_code=404)
            return {"status": "ok", "deleted": 1}
        else:
            # Clear all email history
            result = db["email_events"].delete_many({})
            return {"status": "ok", "deleted": result.deleted_count}
    except Exception:
        return JSONResponse({"status": "error", "message": "Failed to clear email history"}, status_code=500)


@app.post("/api/smtp/check")
def api_smtp_check(payload: dict):
    try:
        payload = payload or {}
        settings = _get_merged_settings()
        for key in (
            "smtp_host",
            "smtp_port",
            "smtp_host_2",
            "smtp_port_2",
            "smtp_username",
            "smtp_password",
            "smtp_tls",
            "smtp_from",
        ):
            if key in payload:
                settings[key] = payload.get(key)

        def build_status(host_key: str, port_key: str):
            host = (settings.get(host_key) or "").strip()
            port = _parse_int(settings.get(port_key))
            if not host or not port:
                return {"state": "not-configured", "up": False, "host": host or None, "port": port}
            ok, err = _smtp_probe_target(settings, host, port)
            return {
                "state": "up" if ok else "down",
                "up": bool(ok),
                "host": host,
                "port": port,
                "error": err,
            }

        return {
            "status": "ok",
            "primary": build_status("smtp_host", "smtp_port"),
            "secondary": build_status("smtp_host_2", "smtp_port_2"),
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


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
        smtp_mode = (payload.get("smtp_mode") or "auto").strip().lower()
        if smtp_mode not in {"auto", "primary", "secondary", "both"}:
            smtp_mode = "auto"
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

        subject, text_body, html_body = _build_test_email_content(int(time.time()), smtp_mode, len(to_list))

        ok, err, smtp_used = _send_alert_email(
            s,
            to_list,
            subject,
            text_body,
            mode=smtp_mode,
            html_body=html_body,
        )
        if not ok:
            return JSONResponse({"status": "error", "message": err or "SMTP send failed", "smtp_used": smtp_used}, status_code=500)
        sent = list(to_list)
        try:
            app.state.db["email_events"].insert_one(
                {
                    "created_at": int(time.time()),
                    "subject": subject,
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
        return {"status": "ok", "sent": sent, "count": len(sent), "smtp_used": smtp_used}
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
