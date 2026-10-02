"""Adobe anonymous device activation -- prerequisite for .acsm fulfillment (ADE device identity).

MIT-independent rewrite of the libadobeAccount protocol from DeDRM_tools (GPL), reusing adobe_crypto primitives.
Plain HTTP POST to Adobe ACS (adeactivate.adobe.com); no ADE installation needed.

Flow (anonymous): create_device -> create_user -> sign_in -> activate_device -> export_private_key.
Message builders are locally unit-testable (cert encryption / signin structure / activate signature verifiable /
private key PKCS8 header strip); **the full chain requires live testing against adeactivate.adobe.com**
(an older endpoint, possibly migrated or rate-limited, cannot be validated offline).
Version constants default to ADE 3.0 values.
"""
import base64
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.asn1 import DerSequence
from cryptography.hazmat.primitives import serialization as _ser
from cryptography.hazmat.primitives.serialization import pkcs12 as _p12

from .adobe_crypto import (
    add_nonce, decrypt_with_device_key, encrypt_with_device_key,
    make_device_keypair, make_fingerprint, make_serial, sign_node,
)
from mmdl.core.http import HttpClient, HttpConfig, split_url

_ADEPT_NS = "http://ns.adobe.com/adept"
_ACS = "https://adeactivate.adobe.com/adept"
_MEDIA = "application/vnd.adobe.adept+xml"

# ADE 3.0 version constants (buildActivateReq / buildSignInRequest)
_VER_CLIENT = "3.0.1.91394"
_VER_HOBBES = "4.38.21971"
_VER_OS = "Windows"
_VER_LOCALE = "en-US"
_VER_DEVICE_TYPE = "standalone"   # ADE device type (libadobe uses standalone)


def _http():
    # ADEPT protocol requires UA="book2png" (Adobe distinguishes clients by UA).
    return HttpClient(HttpConfig(user_agent="book2png", content_type=_MEDIA))


def _ad(tag):
    return "{%s}%s" % (_ADEPT_NS, tag)


# ---- state persistence ----
def _write_text(account_dir, name, text):
    p = Path(account_dir) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _write_bytes(account_dir, name, data):
    p = Path(account_dir) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _read_text(account_dir, name):
    p = Path(account_dir) / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _read_bytes(account_dir, name):
    p = Path(account_dir) / name
    return p.read_bytes() if p.exists() else None


def _send_docu(doc, host, path):
    st, body = _http().request(host, "POST", path, body=doc.encode("utf-8"), content_type=_MEDIA)
    if st != 200:
        raise RuntimeError(f"adobe {path} HTTP {st}: {body.decode('utf-8', 'ignore')[:200]}")
    return body.decode("utf-8")


def _load_user_priv(account_dir) -> bytes:
    """Unwrap the user RSA private key from activation.xml credentials/pkcs12 (used to sign activate/fulfill)."""
    act = ET.fromstring(_read_text(account_dir, "activation.xml"))
    p12 = act.find(f".//{_ad('credentials')}/{_ad('pkcs12')}")
    if p12 is None or not p12.text:
        raise RuntimeError("adobe: activation.xml 无 credentials/pkcs12")
    devsalt = (Path(account_dir) / "devicesalt").read_bytes()
    loaded = _p12.load_pkcs12(base64.b64decode(p12.text), base64.b64encode(devsalt))
    return loaded.key.private_bytes(_ser.Encoding.DER, _ser.PrivateFormat.PKCS8, _ser.NoEncryption())


# ---- message builders (locally unit-testable) ----
def encrypt_login_credentials(devsalt, username, password, auth_cert) -> bytes:
    """devsalt || len_user || username || len_pwd || password, encrypted with the Adobe public cert via PKCS1_v1_5."""
    buf = bytearray(devsalt)
    ub, pb = username.encode("utf-8"), password.encode("utf-8")
    buf += bytes([len(ub)]) + ub
    buf += bytes([len(pb)]) + pb
    try:
        # Adobe authenticationCertificate may be a full X.509 (tbsCertificate[6]=SPKI) or a bare SPKI
        cert = DerSequence()
        cert.decode(base64.b64decode(auth_cert))
        tbs = DerSequence()
        tbs.decode(cert[0])
        rsa = RSA.importKey(tbs[6])
    except (ValueError, TypeError, IndexError):
        rsa = RSA.importKey(base64.b64decode(auth_cert))
    return PKCS1_v1_5.new(rsa).encrypt(bytes(buf))


def build_device_xml(serial, fingerprint):
    # mirrors libgourou Device::createDeviceFile: deviceInfo root + deviceClass/deviceSerial/deviceName
    return (
        '<?xml version="1.0"?>'
        f'<adept:deviceInfo xmlns:adept="{_ADEPT_NS}">'
        "<adept:deviceClass>Desktop</adept:deviceClass>"
        f"<adept:deviceSerial>{serial}</adept:deviceSerial>"
        "<adept:deviceName>desktop</adept:deviceName>"
        f"<adept:deviceType>{_VER_DEVICE_TYPE}</adept:deviceType>"
        f'<adept:version name="hobbes">{_VER_HOBBES}</adept:version>'
        f'<adept:version name="clientOS">{_VER_OS}</adept:version>'
        f'<adept:version name="clientLocale">{_VER_LOCALE}</adept:version>'
        f"<adept:fingerprint>{fingerprint}</adept:fingerprint>"
        "</adept:deviceInfo>"
    )


def build_activation_skeleton(auth_url, user_info, cert, auth_cert):
    return (
        '<?xml version="1.0"?>'
        f'<adept:activationInfo xmlns:adept="{_ADEPT_NS}">'
        "<adept:activationServiceInfo>"
        f"<adept:authURL>{auth_url}</adept:authURL>"
        f"<adept:userInfoURL>{user_info}</adept:userInfoURL>"
        f"<adept:activationURL>{_ACS}</adept:activationURL>"
        f"<adept:certificate>{cert}</adept:certificate>"
        f"<adept:authenticationCertificate>{auth_cert}</adept:authenticationCertificate>"
        "</adept:activationServiceInfo></adept:activationInfo>"
    )


def build_signin_request(account_type, username, password, auth_cert, devsalt) -> str:
    crypto = encrypt_login_credentials(devsalt, username, password, auth_cert)
    authkey = RSA.generate(1024)
    licensekey = RSA.generate(1024)
    return "".join([
        '<?xml version="1.0"?>',
        f'<adept:signIn xmlns:adept="{_ADEPT_NS}" method="{account_type}">',
        f"<adept:signInData>{base64.b64encode(crypto).decode()}</adept:signInData>",
        f"<adept:publicAuthKey>{base64.b64encode(authkey.publickey().exportKey('DER')).decode()}</adept:publicAuthKey>",
        f"<adept:encryptedPrivateAuthKey>{base64.b64encode(encrypt_with_device_key(authkey.exportKey('DER', pkcs=8), devsalt)).decode()}</adept:encryptedPrivateAuthKey>",
        f"<adept:publicLicenseKey>{base64.b64encode(licensekey.publickey().exportKey('DER')).decode()}</adept:publicLicenseKey>",
        f"<adept:encryptedPrivateLicenseKey>{base64.b64encode(encrypt_with_device_key(licensekey.exportKey('DER', pkcs=8), devsalt)).decode()}</adept:encryptedPrivateLicenseKey>",
        "</adept:signIn>",
    ])


def build_activate_request(account_dir, priv_der) -> str:
    """Build the <adept:activate> message with an Adobe signature over the node (sign_node)."""
    droot = ET.fromstring(_read_text(account_dir, "device.xml"))
    aroot = ET.fromstring(_read_text(account_dir, "activation.xml"))
    fingerprint = droot.findtext(_ad("fingerprint"))
    device_type = droot.findtext(_ad("deviceType"))
    device_class = droot.findtext(_ad("deviceClass")) or _VER_CLIENT   # clientVersion uses deviceClass (libgourou)
    user = aroot.findtext(f".//{_ad('credentials')}/{_ad('user')}")
    nonce, exp = add_nonce()
    parts = [
        '<?xml version="1.0"?>',
        f'<adept:activate xmlns:adept="{_ADEPT_NS}" requestType="initial">',
        f"<adept:fingerprint>{fingerprint}</adept:fingerprint>",
        f"<adept:deviceType>{device_type}</adept:deviceType>",
        f"<adept:clientOS>{_VER_OS}</adept:clientOS>",
        f"<adept:clientLocale>{_VER_LOCALE}</adept:clientLocale>",
        f"<adept:clientVersion>{device_class}</adept:clientVersion>",
        "<adept:targetDevice>",
        f"<adept:softwareVersion>{_VER_HOBBES}</adept:softwareVersion>",
        f"<adept:clientOS>{_VER_OS}</adept:clientOS>",
        f"<adept:clientLocale>{_VER_LOCALE}</adept:clientLocale>",
        f"<adept:clientVersion>{device_class}</adept:clientVersion>",
        f"<adept:deviceType>{device_type}</adept:deviceType>",
        f"<adept:fingerprint>{fingerprint}</adept:fingerprint>",
        "</adept:targetDevice>",
        f"<adept:nonce>{nonce}</adept:nonce>",
        f"<adept:expiration>{exp}</adept:expiration>",
        f"<adept:user>{user}</adept:user>",
    ]
    body = "".join(parts) + "</adept:activate>"
    sig = sign_node(ET.fromstring(body), priv_der)
    # keep the original adept prefix; insert signature before the closing tag
    return body.replace("</adept:activate>",
                        f"<adept:signature>{sig}</adept:signature></adept:activate>")


def export_private_key(account_dir) -> bytes:
    """Strip the 26B PKCS#8 header from activation.xml credentials/privateLicenseKey -> user RSA private key DER."""
    root = ET.fromstring(_read_text(account_dir, "activation.xml"))
    elem = root.find(f".//{_ad('credentials')}/{_ad('privateLicenseKey')}")
    if elem is None:
        raise RuntimeError("adobe: activation.xml 无 credentials/privateLicenseKey")
    return base64.b64decode(elem.text)[26:]


# ---- network steps (require live testing) ----
def _get_activation_service_info():
    host, base = split_url(_ACS)
    st, body = _http().request(host, "GET", base + "/ActivationServiceInfo")
    if st != 200:
        raise RuntimeError(f"adobe ActivationServiceInfo HTTP {st}: {body.decode('utf-8','ignore')[:200]}")
    root = ET.fromstring(body)
    auth_url = root.findtext(_ad("authURL"))
    user_info = root.findtext(_ad("userInfoURL"))
    cert = root.findtext(_ad("certificate"))
    ah, ap = split_url(auth_url)
    st2, body2 = _http().request(ah, "GET", ap + "/AuthenticationServiceInfo")
    if st2 != 200:
        raise RuntimeError(f"adobe AuthenticationServiceInfo HTTP {st2}")
    return auth_url, user_info, cert, ET.fromstring(body2).findtext(_ad("certificate"))


def activate_anonymous(account_dir) -> dict:
    """Full anonymous activation (requires live Adobe ACS). Writes account_dir state files."""
    account_dir = Path(account_dir)
    account_dir.mkdir(parents=True, exist_ok=True)

    serial = make_serial()
    devsalt = os.urandom(16)   # ADE DEVICE_KEY is 16 bytes (createDeviceKeyFile); 32B misaligns the signInData field
    priv_der = make_device_keypair()
    (account_dir / "devicesalt").write_bytes(devsalt)
    (account_dir / "privkey.der").write_bytes(priv_der)
    fingerprint = make_fingerprint(serial, devsalt).decode()
    _write_text(account_dir, "device.xml", build_device_xml(serial, fingerprint))
    _write_text(account_dir, "activation.xml",
                build_activation_skeleton(*_get_activation_service_info()))

    # sign_in (anonymous)
    auth_url = ET.fromstring(_read_text(account_dir, "activation.xml")).findtext(
        f".//{_ad('activationServiceInfo')}/{_ad('authURL')}")
    auth_cert = ET.fromstring(_read_text(account_dir, "activation.xml")).findtext(
        f".//{_ad('activationServiceInfo')}/{_ad('authenticationCertificate')}")
    signin = build_signin_request("anonymous", "anonymous", "", auth_cert, devsalt)  # libgourou: username="anonymous", non-empty
    ah, ap = split_url(auth_url)
    cred = ET.fromstring(_send_docu(signin, ah, ap + "/SignInDirect"))
    if cred.tag == _ad("error"):
        raise RuntimeError(f"adobe SignInDirect error: {cred.get('data','')[:200]}")
    user = cred.findtext(_ad("user"))
    priv_lic = decrypt_with_device_key(
        base64.b64decode(cred.findtext(_ad("encryptedPrivateLicenseKey"))), devsalt)
    skeleton = _read_text(account_dir, "activation.xml")
    _write_text(account_dir, "activation.xml", skeleton.replace(
        "</adept:activationInfo>",
        f'<adept:credentials xmlns:adept="{_ADEPT_NS}">'
        f"<adept:user>{user}</adept:user>"
        f"<adept:pkcs12>{cred.findtext(_ad('pkcs12'))}</adept:pkcs12>"
        f"<adept:licenseCertificate>{cred.findtext(_ad('licenseCertificate'))}</adept:licenseCertificate>"
        f"<adept:authenticationCertificate>{ET.fromstring(skeleton).findtext(f'.//{_ad('activationServiceInfo')}/{_ad('authenticationCertificate')}')}</adept:authenticationCertificate>"
        f"<adept:privateLicenseKey>{base64.b64encode(priv_lic).decode()}</adept:privateLicenseKey>"
        "</adept:credentials></adept:activationInfo>"))

    # activate_device: write the activationToken from /Activate into activation.xml (includes device, needed by fulfill)
    activate = build_activate_request(account_dir, _load_user_priv(account_dir))
    host, base = split_url(_ACS)
    resp = ET.fromstring(_send_docu(activate, host, base + "/Activate"))
    if resp.tag == _ad("error"):
        raise RuntimeError(f"adobe Activate error: {resp.get('data','')[:200]}")
    at_str = ET.tostring(resp, encoding="unicode", xml_declaration=False)
    act = _read_text(account_dir, "activation.xml").replace(
        "</adept:activationInfo>", f"{at_str}</adept:activationInfo>")
    _write_text(account_dir, "activation.xml", act)
    return {"serial": serial, "fingerprint": fingerprint, "user": user}
