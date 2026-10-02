"""Inline Kobo Obok decryption (two-layer AES-128-ECB + PKCS7 + gzip inflation).

Depends only on pycryptodome, no DeDRM_tools. Key derivation and per-file decryption
formulas per docs/kobo_source_design.md:

  deviceid = SHA256_hex(hash_key + serial)
  userkey  = unhex(SHA256_hex(deviceid + userid)[32:])        # last 16B = AES-128 key
  page_key = AES_ECB_decrypt(base64(elementkey), userkey)
  plain    = AES_ECB_decrypt(file_bytes, page_key), strip PKCS#7, gunzip if gzip magic

elementkey is read preferentially from the .kepub's META-INF/encryption.xml (per-file
CipherReference@EncryptionKey); falls back to caller-supplied content_keys (returned by the download endpoint, dict: zip name -> base64 elementkey).
"""
import base64
import gzip
import io
import zipfile
import xml.etree.ElementTree as ET
from hashlib import sha256

from Crypto.Cipher import AES

HASH_KEYS = ("88b3a2e13", "XzUhGYdFp", "NoCanLook", "QJhwzAtXL")

# elementkey is usually base64 of a 16B ciphertext; AES-128-ECB block size is 16B
_BLOCK = 16


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _unpad_pkcs7(data):
    pad = data[-1]
    if pad and pad <= _BLOCK and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def device_id(serial: str, hash_key: str = HASH_KEYS[0]) -> str:
    """Derive Kobo deviceid from device serial: hex of sha256(hash_key + serial)."""
    return sha256((hash_key + serial).encode("utf-8")).hexdigest()


def user_key(deviceid: str, userid: str) -> bytes:
    """Derive the 16B AES key from deviceid + userid (last 16B of the SHA256 hex)."""
    h = sha256((deviceid + userid).encode("utf-8")).hexdigest()
    return bytes.fromhex(h[32:])


def _load_encryption_keymap(src: zipfile.ZipFile, content_keys=None) -> dict:
    """Return {zip entry name: elementkey_bytes}. Prefers encryption.xml, falls back to content_keys."""
    keymap = {}
    try:
        raw = src.read("META-INF/encryption.xml")
    except KeyError:
        raw = b""
    if raw:
        root = ET.fromstring(raw)
        for cd in root.iter():
            if _local(cd.tag) != "CipherReference":
                continue
            uri, ek = None, None
            for key, val in cd.attrib.items():
                k = _local(key)
                if k == "URI":
                    uri = val
                elif k == "EncryptionKey":
                    ek = val
            if uri and ek:
                try:
                    keymap[uri] = base64.b64decode(ek)
                except (ValueError, TypeError):
                    pass
    if content_keys:
        for k, v in content_keys.items():
            keymap.setdefault(k, base64.b64decode(v) if isinstance(v, str) else v)
    return keymap


def _decrypt_entry(data: bytes, elementkey: bytes, userkey: bytes) -> bytes:
    """Single file: elementkey → page_key → AES-ECB decrypt → strip PKCS7 → gunzip if needed."""
    page_key = AES.new(userkey, AES.MODE_ECB).decrypt(elementkey)
    if len(data) % _BLOCK:
        # Kobo block-ciphered files should be a multiple of 16B
        data = data[: len(data) - len(data) % _BLOCK]
    plain = AES.new(page_key, AES.MODE_ECB).decrypt(data)
    plain = _unpad_pkcs7(plain)
    if plain[:2] == b"\x1f\x8b":
        try:
            plain = gzip.decompress(plain)
        except OSError:
            pass
    return plain


def decrypt_kepub(kepub_bytes: bytes, deviceid: str, userid: str, content_keys=None) -> bytes:
    """Decrypt a whole .kepub; returns rebuilt plaintext zip (epub) bytes.

    Encrypted entries are replaced with plaintext, others kept as-is; keys derive from deviceid+userid.
    """
    userkey = user_key(deviceid, userid)
    src = zipfile.ZipFile(io.BytesIO(kepub_bytes))
    keymap = _load_encryption_keymap(src, content_keys)
    buf = io.BytesIO()
    out = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    for item in src.infolist():
        data = src.read(item.filename)
        ek = keymap.get(item.filename)
        if ek:
            data = _decrypt_entry(data, ek, userkey)
        out.writestr(item, data)
    out.close()
    return buf.getvalue()
