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


def check_credentials(nvr, username, password):
    """Verify one supplied login without writing recorder settings or reading users."""
    def result(verified, message):
        return {"verified": verified, "message": message}

    host = nvr['ip']
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    base = f'http://{host}'
    vendor = (nvr.get('type') or '').lower()
    try:
        if vendor in ('milesight', 'milesight old'):
            helper = MonitorState()
            uri = '/cgi/main/1000'
            with requests.Session() as session:
                reply = session.post(base + uri, headers={
                    'Authorization': helper._milesight_digest_auth_header(username, password, uri, 'POST'),
                    'Content-Type': 'application/json'},
                    json={'userName': username, 'md5': helper._milesight_md5(password)},
                    timeout=8, allow_redirects=False)
                if reply.status_code == 200:
                    data = reply.json()
                    if isinstance(data, dict) and str(data.get('type')) == '0':
                        return result(True, 'Credentials verified.')
                    if isinstance(data, dict) and int(data.get('blankTime') or 0) > 0:
                        return result(False, 'Account temporarily locked. Try again later.')
                return result(False, 'Login not accepted. Check the password, account status, and recorder access.')

        if vendor == 'hikvision':
            url = base + '/ISAPI/System/deviceInfo'
            anonymous = requests.get(url, timeout=8, allow_redirects=False)
            if anonymous.status_code != 401:
                return result(False, 'Could not verify: the recorder did not require authentication for this check.')
            reply = requests.get(url, auth=HTTPDigestAuth(username, password), timeout=8, allow_redirects=False)
            if reply.status_code == 401:
                return result(False, 'Login rejected. Check the username and password.')
            if reply.status_code == 403:
                return result(False, 'Could not verify: this account lacks permission for the check.')
            if reply.status_code == 200 and _name(ET.fromstring(reply.text)) == 'DeviceInfo':
                return result(True, 'Credentials verified.')
        else:
            url = base + '/onvif/device_service'
            headers = {'Content-Type': 'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation"'}
            anonymous = requests.post(url, data=b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>', headers=headers, timeout=8, allow_redirects=False)
            if anonymous.status_code == 200:
                root = ET.fromstring(anonymous.text)
                if any(_name(node) == 'GetDeviceInformationResponse' for node in root.iter()):
                    return result(False, 'Could not verify: ONVIF allows this check without a login.')
            elif anonymous.status_code not in (401, 403, 500):
                return result(False, 'Could not verify: ONVIF is unavailable or unsupported.')
            reply = requests.post(url, auth=HTTPDigestAuth(username, password),
                                  data=_onvif_envelope(username, password, 'GetDeviceInformation'),
                                  headers=headers, timeout=8, allow_redirects=False)
            if reply.status_code == 200:
                root = ET.fromstring(reply.text)
                if not any(_name(node) == 'Fault' for node in root.iter()) and any(_name(node) == 'GetDeviceInformationResponse' for node in root.iter()):
                    return result(True, 'ONVIF credentials verified.')
            return result(False, 'ONVIF login not verified. Check credentials, permissions, ONVIF access, and recorder time.')
        return result(False, 'Could not verify credentials on this recorder.')
    except requests.Timeout:
        return result(False, 'Recorder timed out. Try again when it is reachable.')
    except requests.RequestException:
        return result(False, 'Could not connect to the recorder.')
    except (ValueError, ET.ParseError, TypeError):
        return result(False, 'Could not verify: the recorder returned an unexpected response.')
