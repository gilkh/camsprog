"""Read device accounts without changing recorder configuration."""

import base64
import hashlib
import os
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from .monitor import MonitorState


class AccountReadError(Exception):
    pass


def _name(element):
    return element.tag.rsplit("}", 1)[-1]


def parse_accounts(text, source):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise AccountReadError("The recorder returned an unreadable account list.") from None
    if any(_name(node) == "Fault" for node in root.iter()):
        raise AccountReadError("Account lookup was refused. Check the account permissions, ONVIF settings, and recorder clock.")
    if source == "ISAPI":
        if _name(root) != "UserList":
            raise AccountReadError("This recorder does not expose its device account list through ISAPI.")
        container = root
    else:
        container = next((node for node in root.iter() if _name(node) == "GetUsersResponse"), None)
        if container is None:
            raise AccountReadError("This recorder does not expose an ONVIF account list.")
    accounts = []
    seen = set()
    for node in container:
        if _name(node) != "User":
            continue
        # Explicitly select display fields; never forward device passwords/hashes.
        fields = {_name(child): child.text or "" for child in node}
        username = fields.get("userName", fields.get("Username", "")).strip()
        if not username or username in seen:
            continue
        seen.add(username)
        role = fields.get("userLevel", fields.get("UserLevel", "Unknown"))
        accounts.append({"username": username, "role": role,
                         "is_admin": role.lower() in ("administrator", "admin") or username.lower() == "admin"})
    return sorted(accounts, key=lambda item: (item["is_admin"], item["username"].lower()))


def _onvif_envelope(username, password, operation="GetUsers", content=None, created=None):
    soap = "http://www.w3.org/2003/05/soap-envelope"
    wsse = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
    wsu = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
    token_profile = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0"
    nonce = os.urandom(16)
    created = created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    envelope = ET.Element(f"{{{soap}}}Envelope")
    security = ET.SubElement(ET.SubElement(envelope, f"{{{soap}}}Header"), f"{{{wsse}}}Security")
    security.set(f"{{{soap}}}mustUnderstand", "true")
    token = ET.SubElement(security, f"{{{wsse}}}UsernameToken")
    ET.SubElement(token, f"{{{wsse}}}Username").text = username
    ET.SubElement(token, f"{{{wsse}}}Password", Type=token_profile + "#PasswordDigest").text = digest
    ET.SubElement(token, f"{{{wsse}}}Nonce", EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary").text = base64.b64encode(nonce).decode()
    ET.SubElement(token, f"{{{wsu}}}Created").text = created
    body = ET.SubElement(envelope, f"{{{soap}}}Body")
    command = ET.SubElement(body, "{http://www.onvif.org/ver10/device/wsdl}" + operation)
    if content is not None:
        command.extend(content)
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def read_accounts(nvr):
    username, password = nvr.get("username"), nvr.get("password")
    if not username or not password:
        raise AccountReadError("Save the recorder's login in Edit NVR before loading accounts.")
    host = nvr["ip"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    base = f"http://{host}"
    auth = HTTPDigestAuth(username, password)
    try:
        if (nvr.get("type") or "").lower() in ("milesight", "milesight old"):
            helper = MonitorState()
            with requests.Session() as session:
                if helper._milesight_web_login(session, nvr["ip"], username, password, timeout=6):
                    response = helper._milesight_web_post_json(session, nvr["ip"], username, password, "/cgi/main/6070", payload="", timeout=8)
                    _check_response(response)
                    rows = response.json()
                    if isinstance(rows, list):
                        accounts = []
                        for row in rows:
                            if not isinstance(row, dict) or str(row.get("enable", 0)) == "0" or not row.get("name"):
                                continue
                            role = {"0": "Administrator", "1": "Operator", "2": "Viewer"}.get(str(row.get("type")), "Unknown")
                            accounts.append({"username": str(row["name"]), "role": role, "is_admin": role == "Administrator"})
                        return {"source": "Milesight native", "scope": "Device accounts", "accounts": sorted(accounts, key=lambda item: (item["is_admin"], item["username"].lower()))}
        if (nvr.get("type") or "").lower() == "hikvision":
            response = requests.get(base + "/ISAPI/Security/users", auth=auth, timeout=8, allow_redirects=False)
            if response.status_code == 401:
                response = requests.get(base + "/ISAPI/Security/users", auth=(username, password), timeout=8, allow_redirects=False)
            _check_response(response)
            return {"source": "ISAPI", "scope": "Device accounts", "accounts": parse_accounts(response.text, "ISAPI")}
        response = requests.post(
            base + "/onvif/device_service", auth=auth,
            data=_onvif_envelope(username, password),
            headers={"Content-Type": 'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver10/device/wsdl/GetUsers"'},
            timeout=8, allow_redirects=False,
        )
        _check_response(response)
        return {"source": "ONVIF", "scope": "ONVIF accounts (may differ from local recorder accounts)",
                "accounts": parse_accounts(response.text, "ONVIF")}
    except requests.Timeout:
        raise AccountReadError("The recorder timed out. Check that it is online and its account service is enabled.") from None
    except requests.RequestException:
        raise AccountReadError("Could not connect to the recorder's account service.") from None


def _check_response(response):
    if response.status_code in (401, 403):
        raise AccountReadError("The recorder refused access. Its saved login needs permission to read accounts; ONVIF may require a separate login.")
    if response.status_code != 200:
        raise AccountReadError("The recorder's account service is unavailable or unsupported on this firmware.")
