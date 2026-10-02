"""Adobe .acsm fulfillment (ADEPT) — redeem .acsm into a DRM'd epub, then hand it to adept_drm for decryption.

Independently rewritten as MIT from the DeDRM_tools (GPL) libadobeFulfill/fulfill protocol, reusing adobe_crypto (signing),
adept_drm (content decryption) and the adobe_auth activation state. Parsing user pkcs12 credentials needs `cryptography` (already in requirements).

Flow: parse_acsm → sign fulfill request (user private key) → operatorAuth (Auth) → POST /Fulfill
→ fulfillmentResult (src/licenseToken) → fetchLicenseService → buildRights → download(src)
→ epub with rights.xml → adept_drm.decrypt_epub.

⚠️ Message construction is locally unit-testable (build_auth/build_fulfill/build_rights); the full chain needs live operator/Adobe testing.
"""
import base64
import io
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12 as _pkcs12

from .adobe_crypto import sign_node, add_nonce
from mmdl.core.http import HttpClient, HttpConfig, split_url

_ADE = "http://ns.adobe.com/adept"


def _ad(tag):
    return "{%s}%s" % (_ADE, tag)


def _http():
    return HttpClient(HttpConfig(user_agent="book2png",
                                content_type="application/vnd.adobe.adept+xml"))


def _read_text(account_dir, name):
    p = Path(account_dir) / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _text(node, *tags):
    cur = node
    for tag in tags:
        cur = cur.find(_ad(tag))
        if cur is None:
            return ""
    return (cur.text or "").strip()


def parse_acsm(acsm_bytes) -> dict:
    """Parse .acsm → dict (operatorURL/transaction/resource/title)."""
    root = ET.fromstring(acsm_bytes)
    title = ""
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "title" and el.text and el.text.strip():
            title = el.text.strip()
            break
    return {
        "operatorURL": _text(root, "operatorURL"),
        "transaction": _text(root, "transaction"),
        "resource": _text(root, "resourceItemInfo", "resource"),
        "title": title,
    }


# ---- user credentials ----
def load_user_credentials(account_dir):
    """activation.xml credentials/pkcs12 (devkey-decrypted) → (priv_der, cert_der). cryptography parses pkcs12."""
    act = ET.fromstring(_read_text(account_dir, "activation.xml"))
    p12 = act.find(f".//{_ad('credentials')}/{_ad('pkcs12')}")
    if p12 is None or not p12.text:
        raise RuntimeError("adobe: activation.xml 无 credentials/pkcs12")
    devsalt = (Path(account_dir) / "devicesalt").read_bytes()
    # pkcs12 password = base64(devsalt) (libadobe: parse_pkcs12(pkcs12, b64encode(devkey))), not raw devsalt
    loaded = _pkcs12.load_pkcs12(base64.b64decode(p12.text), base64.b64encode(devsalt))
    priv_der = loaded.key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
    return priv_der, loaded.cert.certificate.public_bytes(serialization.Encoding.DER)


def export_user_key(account_dir) -> bytes:
    """activation.xml credentials/privateLicenseKey, strip 26B PKCS#8 header → user RSA private key (for adept_drm decryption)."""
    act = ET.fromstring(_read_text(account_dir, "activation.xml"))
    elem = act.find(f".//{_ad('credentials')}/{_ad('privateLicenseKey')}")
    if elem is None:
        raise RuntimeError("adobe: activation.xml 无 credentials/privateLicenseKey")
    return base64.b64decode(elem.text)[26:]


# ---- message construction (locally unit-testable) ----
def build_auth_request(account_dir, cert_der) -> str:
    act = ET.fromstring(_read_text(account_dir, "activation.xml"))
    user = act.findtext(f".//{_ad('credentials')}/{_ad('user')}")
    lic_cert = act.findtext(f".//{_ad('credentials')}/{_ad('licenseCertificate')}")
    auth_cert = act.findtext(f".//{_ad('credentials')}/{_ad('authenticationCertificate')}")
    return "".join([
        '<?xml version="1.0"?>',
        f'<adept:credentials xmlns:adept="{_ADE}">',
        f"<adept:user>{user}</adept:user>",
        f"<adept:certificate>{base64.b64encode(cert_der).decode()}</adept:certificate>",
        f"<adept:licenseCertificate>{lic_cert}</adept:licenseCertificate>",
        f"<adept:authenticationCertificate>{auth_cert}</adept:authenticationCertificate>",
        "</adept:credentials>",
    ])


def build_fulfill_request(account_dir, acsm_root) -> str:
    dev = ET.fromstring(_read_text(account_dir, "device.xml"))
    act = ET.fromstring(_read_text(account_dir, "activation.xml"))
    user = act.findtext(f".//{_ad('credentials')}/{_ad('user')}")
    device = act.findtext(f".//{_ad('activationToken')}/{_ad('device')}")
    fp = dev.findtext(_ad("fingerprint"))
    dtype = dev.findtext(_ad("deviceType"))
    hobbes = dev.findtext(_ad("version") + "[@name='hobbes']") or ""
    client_os = dev.findtext(_ad("version") + "[@name='clientOS']") or ""
    locale = dev.findtext(_ad("version") + "[@name='clientLocale']") or ""
    acsm_str = ET.tostring(acsm_root, encoding="unicode", xml_declaration=False)
    return "".join([
        '<?xml version="1.0"?>',
        f'<adept:fulfill xmlns:adept="{_ADE}">',
        f"<adept:user>{user}</adept:user>",
        f"<adept:device>{device}</adept:device>",
        f"<adept:deviceType>{dtype}</adept:deviceType>",
        acsm_str,
        "<adept:targetDevice>",
        f"<adept:softwareVersion>{hobbes}</adept:softwareVersion>",
        f"<adept:clientOS>{client_os}</adept:clientOS>",
        f"<adept:clientLocale>{locale}</adept:clientLocale>",
        f"<adept:deviceType>{dtype}</adept:deviceType>",
        "<adept:productName>ADOBE Digitial Editions</adept:productName>",
        f"<adept:fingerprint>{fp}</adept:fingerprint>",
        f"<adept:activationToken><adept:user>{user}</adept:user>"
        f"<adept:device>{device}</adept:device></adept:activationToken>",
        "</adept:targetDevice>",
        "</adept:fulfill>",
    ])


def build_init_license_service_request(account_dir, operator_url, priv_der) -> str:
    """<adept:licenseServiceRequest> message for InitLicenseService after operator Auth."""
    act = ET.fromstring(_read_text(account_dir, "activation.xml"))
    user = act.findtext(f".//{_ad('credentials')}/{_ad('user')}")
    nonce, exp = add_nonce()
    parts = [
        '<?xml version="1.0"?>',
        f'<adept:licenseServiceRequest xmlns:adept="{_ADE}" identity="user">',
        f"<adept:operatorURL>{operator_url}</adept:operatorURL>",
        f"<adept:nonce>{nonce}</adept:nonce>",
        f"<adept:expiration>{exp}</adept:expiration>",
        f"<adept:user>{user}</adept:user>",
    ]
    body = "".join(parts) + "</adept:licenseServiceRequest>"
    sig = sign_node(ET.fromstring(body), priv_der)
    return body.replace("</adept:licenseServiceRequest>",
                        f"<adept:signature>{sig}</adept:signature></adept:licenseServiceRequest>")


def build_rights(license_token_node, account_dir) -> str:
    act = ET.fromstring(_read_text(account_dir, "activation.xml"))
    lic_url = _text(license_token_node, "licenseURL")
    cert = ""
    for info in act.findall(f".//{_ad('licenseServices')}/{_ad('licenseServiceInfo')}"):
        if info.findtext(_ad("licenseURL")) == lic_url:
            cert = info.findtext(_ad("certificate")) or ""
            break
    return "".join([
        '<?xml version="1.0"?>',
        f'<adept:rights xmlns:adept="{_ADE}">',
        ET.tostring(license_token_node, encoding="unicode", xml_declaration=False),
        "<adept:licenseServiceInfo>",
        f"<adept:licenseURL>{lic_url}</adept:licenseURL>",
        f"<adept:certificate>{cert}</adept:certificate>",
        "</adept:licenseServiceInfo>",
        "</adept:rights>",
    ])


class FulfillError(RuntimeError):
    """Fulfillment failure (network/server/protocol)."""


def fulfill_acsm(account_dir, acsm_bytes) -> tuple[bytes, dict, str]:
    """Fulfill .acsm → (encrypted epub bytes with rights.xml, meta, resource_id). Needs live operator/Adobe testing."""
    meta = parse_acsm(acsm_bytes)
    acsm_root = ET.fromstring(acsm_bytes)
    priv_der, cert_der = load_user_credentials(account_dir)

    op_url = meta["operatorURL"]
    # operatorAuth = doOperatorAuth (libgourou): POST Auth + POST InitLicenseService
    auth_url = op_url[:-1] if op_url.endswith("/Fulfill") else op_url   # libgourou: strip trailing /Fulfill
    ah, ap = split_url(auth_url + "/Auth")
    req = build_auth_request(account_dir, cert_der)
    st, body = _http().request(ah, "POST", ap, body=req.encode("utf-8"),
                               content_type="application/vnd.adobe.adept+xml")
    if not (st == 200 and b"<success" in body):
        raise FulfillError(f"adobe operator Auth HTTP {st}: {body.decode('utf-8','ignore')[:200]}")

    # InitLicenseService (establishes license service auth; key to E_ADEPT_USER_AUTH)
    act = ET.fromstring(_read_text(account_dir, "activation.xml"))
    activation_url = act.findtext(f".//{_ad('activationServiceInfo')}/{_ad('activationURL')}")
    if activation_url:
        init_req = build_init_license_service_request(account_dir, auth_url, priv_der)
        ih, ip = split_url(activation_url)
        st3, body3 = _http().request(ih, "POST", ip + "/InitLicenseService", body=init_req.encode("utf-8"),
                                     content_type="application/vnd.adobe.adept+xml")
        if not (st3 == 200 and b"<success" in body3):
            raise FulfillError(f"adobe InitLicenseService HTTP {st3}: {body3.decode('utf-8','ignore')[:200]}")

    # sign + POST /Fulfill
    fulfill_req = build_fulfill_request(account_dir, acsm_root)
    node = ET.fromstring(fulfill_req)
    sig = sign_node(node, priv_der)
    node.append(ET.fromstring(f"<adept:signature xmlns:adept=\"{_ADE}\">{sig}</adept:signature>"))
    signed = ET.tostring(node, encoding="unicode")
    fh, fp = split_url(op_url + "/Fulfill")
    st2, body2 = _http().request(fh, "POST", fp, body=signed.encode("utf-8"),
                                 content_type="application/vnd.adobe.adept+xml")
    if st2 != 200 or b"<error" in body2:
        raise FulfillError(f"adobe fulfill HTTP {st2}: {body2.decode('utf-8','ignore')[:200]}")

    resp = ET.fromstring(body2)
    src = _text(resp, "fulfillmentResult", "resourceItemInfo", "src")
    res_id = _text(resp, "fulfillmentResult", "resourceItemInfo", "resource")
    lic_tok = resp.find(f".//{_ad('fulfillmentResult')}/{_ad('resourceItemInfo')}/{_ad('licenseToken')}")
    if lic_tok is None:
        raise FulfillError("adobe fulfill 响应缺 licenseToken")
    lic_url = _text(lic_tok, "licenseURL")

    # download → write rights.xml → return
    epub = _download(src)
    rights = build_rights(lic_tok, account_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(epub)) as zin:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("META-INF/rights.xml", rights.encode("utf-8"))
    return buf.getvalue(), meta, res_id


def _download(url) -> bytes:
    host, path = split_url(url)
    st, body = _http().request(host, "GET", path)
    if st != 200:
        raise FulfillError(f"adobe download HTTP {st}")
    return body
