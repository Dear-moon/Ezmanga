"""Import an already-authorized Adobe Digital Editions activation identity from this machine
(independent MIT implementation of DeDRM importADEactivation).

ADE stores activation data under registry `HKCU\\Software\\Adobe\\Adept\\Activation``, with privateLicenseKey/device key
encrypted by Windows DPAPI plus CPUID/disk-serial entropy. This module uses standard Windows APIs (ctypes + winreg)
to extract the master key (master_key = devkey/devicesalt) and rebuild activation.xml/device.xml/devicesalt -- so no
script-side anonymous activation is needed (blocked by E_AUTH_USER_AUTH); the ADE-verified device+user binding is
reused directly to fulfill .acsm.

cpuid core from flababah/cpuid.py (MIT). Windows x64 only.
"""
import base64
import ctypes
import os
import struct
import sys
import winreg
from ctypes import (Structure, CFUNCTYPE, POINTER, c_void_p, c_size_t, c_long,
                    c_uint32, c_uint, c_ulong, c_wchar_p, byref, cast, create_string_buffer,
                    create_unicode_buffer, string_at, windll)

from Crypto.Cipher import AES

_ADE = "http://ns.adobe.com/adept"
_ACT_KEY = r"Software\Adobe\Adept\Activation"
_DEV_KEY = r"Software\Adobe\Adept\Device"


# ---- Windows x64 CPUID (MIT, flababah/cpuid.py) ----
_WIN64_OPC = [
    0x53, 0x89, 0xd0, 0x49, 0x89, 0xc9, 0x44, 0x89, 0xc1,
    0x0f, 0xa2, 0x41, 0x89, 0x01, 0x41, 0x89, 0x59, 0x04,
    0x41, 0x89, 0x49, 0x08, 0x41, 0x89, 0x51, 0x0c, 0x5b, 0xc3,
]


class _CPUIDReg(Structure):
    _fields_ = [(n, c_uint32) for n in ("eax", "ebx", "ecx", "edx")]


class _CPUID:
    def __init__(self):
        self.win = ctypes.CDLL("kernel32.dll")
        self.win.VirtualAlloc.restype = c_void_p
        self.win.VirtualAlloc.argtypes = [c_void_p, c_size_t, c_ulong, c_ulong]
        size = len(_WIN64_OPC)
        code = (ctypes.c_ubyte * size)(*_WIN64_OPC)
        self.addr = self.win.VirtualAlloc(None, size, 0x1000, 0x40)
        if not self.addr:
            raise MemoryError("VirtualAlloc RWX failed")
        ctypes.memmove(self.addr, code, size)
        # Clean up the temporary `code` list. (kept inline for clarity)
        self.func = CFUNCTYPE(None, POINTER(_CPUIDReg), c_uint32, c_uint32)(self.addr)

    def __call__(self, eax, ecx=0):
        r = _CPUIDReg()
        self.func(byref(r), eax, ecx)
        return r.eax, r.ebx, r.ecx, r.edx


def _get_serial():
    kernel32 = windll.kernel32
    buf = create_unicode_buffer(256)
    kernel32.GetSystemDirectoryW(buf, len(buf))
    root = buf.value.split("\\")[0] + "\\"
    vsn = c_uint(0)
    kernel32.GetVolumeInformationW(c_wchar_p(root), None, 0, byref(vsn), None, None, None, 0)
    return vsn.value


def _get_user_lowbytes():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Adobe\Adept\Device")
        user = winreg.QueryValueEx(k, "username")[0]
    except OSError:
        return None
    # take low bytes of UTF-16 (same as DeDRM)
    return user.encode("utf-16-le")[::2]


def _unprotect(data, entropy):
    class _Blob(Structure):
        _fields_ = [("cbData", c_uint), ("pbData", c_void_p)]
    crypt32 = windll.crypt32
    indata = _Blob(len(data), cast(create_string_buffer(data), c_void_p))
    ent = _Blob(len(entropy), cast(create_string_buffer(entropy), c_void_p))
    out = _Blob()
    crypt32.CryptUnprotectData.argtypes = [POINTER(_Blob), c_wchar_p, POINTER(_Blob),
                                           c_void_p, c_void_p, c_uint, POINTER(_Blob)]
    crypt32.CryptUnprotectData.restype = c_uint
    if not crypt32.CryptUnprotectData(byref(indata), None, byref(ent), None, None, 0, byref(out)):
        return None
    return string_at(out.pbData, out.cbData)


def _get_master_key():
    """Extract master_key (devkey/devicesalt) from the ADE Device key (DPAPI-encrypted)."""
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _DEV_KEY)
    device = winreg.QueryValueEx(k, "key")[0]
    if isinstance(device, str):
        device = device.encode("latin-1")
    serial = _get_serial()
    cpu = _CPUID()
    _, b, c, d = cpu(0)
    vendor = struct.pack("III", b, d, c)
    signature, _, _, _ = cpu(1)              # CPUID(1) eax = CPU signature (first return value)
    signature = struct.pack(">I", signature)[1:]
    user = _get_user_lowbytes()
    if user is None:
        raise RuntimeError("adobe: 读不到 Adept\\Device\\username")
    # DeDRM uses the user low bytes; WinAPI fallback when None (registry only here)
    entropy = struct.pack(">I12s3s13s", serial, vendor, signature, user)
    key = _unprotect(device, entropy)
    if not key:
        raise RuntimeError("adobe: DPAPI 解主密钥失败（可能需要 ADE 已授权 + 本机用户）")
    return key


# ---- read registry activation data (DeDRM semantics: parent Default=section name, child Default=field name, 'value'=value) ----
def _read_activation_reg():
    out = {}
    root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ACT_KEY)
    i = 0
    while True:
        try:
            parent = winreg.OpenKey(root, "%04d" % i)
            i += 1
        except OSError:
            break
        section = winreg.QueryValueEx(parent, None)[0]          # e.g. 'activationServiceInfo'
        out[section] = {}
        j = 0
        while True:
            try:
                child = winreg.OpenKey(parent, "%04d" % j)
                j += 1
            except OSError:
                break
            field = winreg.QueryValueEx(child, None)[0]          # e.g. 'authURL' / 'user' / 'device'
            val = winreg.QueryValueEx(child, "value")[0]         # actual value
            if isinstance(val, bytes):
                val = val.decode("utf-8", "ignore")
            out[section][field] = val
        parent.Close()
    root.Close()
    return out


def import_ade_activation(account_dir):
    """Rebuild the ADE activation state from the registry into account_dir. Returns info.

    Bypasses anonymous activation (E_AUTH_USER_AUTH); reuses ADE's verified device+user binding directly.
    """
    master_key = _get_master_key()
    data = _read_activation_reg()

    # Decrypt encryptedPrivateLicenseKey (AES-CBC, IV=fingerprint[:16], master_key).
    # PKCS7 padding must be stripped first: export_user_key's [26:] tolerates no trailing pad, else RSA.importKey fails.
    iv = base64.b64decode(data["activationToken"]["fingerprint"])[:16]
    enc = base64.b64decode(data["credentials"]["privateLicenseKey"])
    dec = AES.new(master_key, AES.MODE_CBC, iv).decrypt(enc)
    pad = dec[-1]
    if 0 < pad <= 16:
        dec = dec[:-pad]
    priv_lic = base64.b64encode(dec).decode("latin-1")
    data["credentials"]["privateLicenseKey"] = priv_lic

    si = data["activationServiceInfo"]
    cr = data["credentials"]
    at = data["activationToken"]
    act_xml = (
        '<?xml version="1.0"?>'
        f'<activationInfo xmlns="{_ADE}">'
        f'<adept:activationServiceInfo xmlns:adept="{_ADE}">'
        f"<adept:authURL>{si['authURL']}</adept:authURL>"
        f"<adept:userInfoURL>{si['userInfoURL']}</adept:userInfoURL>"
        f"<adept:activationURL>{si['activationURL']}</adept:activationURL>"
        f"<adept:certificate>{si['certificate']}</adept:certificate>"
        f"<adept:authenticationCertificate>{si['authenticationCertificate']}</adept:authenticationCertificate>"
        "</adept:activationServiceInfo>"
        f'<adept:credentials xmlns:adept="{_ADE}">'
        f"<adept:user>{cr['user']}</adept:user>"
        f"<adept:pkcs12>{cr['pkcs12']}</adept:pkcs12>"
        f"<adept:licenseCertificate>{cr['licenseCertificate']}</adept:licenseCertificate>"
        f"<adept:privateLicenseKey>{cr['privateLicenseKey']}</adept:privateLicenseKey>"
        f"<adept:authenticationCertificate>{cr['authenticationCertificate']}</adept:authenticationCertificate>"
        "</adept:credentials>"
        f'<activationToken xmlns="{_ADE}">'
        f"<device>{at['device']}</device><fingerprint>{at['fingerprint']}</fingerprint>"
        f"<deviceType>{at['deviceType']}</deviceType><activationURL>{at['activationURL']}</activationURL>"
        f"<user>{at['user']}</user><signature>{at['signature']}</signature>"
        "</activationToken></activationInfo>"
    )
    dev_xml = (
        '<?xml version="1.0"?>'
        f'<adept:deviceInfo xmlns:adept="{_ADE}">'
        f"<adept:deviceType>{at['deviceType']}</adept:deviceType>"
        "<adept:deviceClass>Desktop</adept:deviceClass>"
        "<adept:deviceSerial>00000000000000000000000000000000</adept:deviceSerial>"
        "<adept:deviceName>desktop</adept:deviceName>"
        '<adept:version name="hobbes" value="4.38.21971"/>'
        '<adept:version name="clientOS" value="Windows 10"/>'
        '<adept:version name="clientLocale" value="en-US"/>'
        f"<adept:fingerprint>{at['fingerprint']}</adept:fingerprint>"
        "</adept:deviceInfo>"
    )
    import pathlib
    d = pathlib.Path(account_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "activation.xml").write_text(act_xml, encoding="utf-8")
    (d / "device.xml").write_text(dev_xml, encoding="utf-8")
    (d / "devicesalt").write_bytes(master_key)
    return {"user": cr["user"], "device": at["device"], "fingerprint": at["fingerprint"]}
