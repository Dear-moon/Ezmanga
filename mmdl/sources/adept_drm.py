"""Inline Adobe ADEPT ebook content decryption (AES-128-CBC continuous stream + RSA PKCS#1 v1.5 bookkey unwrap).

Pure local algorithm, no DeDRM_tools dependency (that one is GPL; MIT reimplementation of the public ADEPT mechanism).

ADEPT-encrypted epub essentials (mirrors ineptepub):
- META-INF/rights.xml adept:encryptedKey (base64) unwraps to the 16B bookkey via the account RSA private key with PKCS1_v1_5.
- META-INF/encryption.xml marks encrypted entries (aes128-cbc / aes128-cbc-uncompressed).
- All encrypted entries share one AES-128-CBC object (CBC chain state persists across files); per file: decrypt whole
  string, drop the first 16B block, unpad PKCS7, then raw inflate(-15).

Output zip rebuilt: mimetype stored, unencrypted entries verbatim, removed encryption.xml entries dropped.
"""
import base64
import io
import zlib
import zipfile
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

_ADEPT_NS = "http://ns.adobe.com/adept"
_ENC_NS = "http://www.w3.org/2001/04/xmlenc#"
_META_NAMES = ("mimetype", "META-INF/rights.xml")


def _ad(tag):
    return "{%s}%s" % (_ADEPT_NS, tag)


def _en(tag):
    return "{%s}%s" % (_ENC_NS, tag)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _unpad(data, block=16):
    pad = data[-1]
    return data[:-pad] if 0 < pad <= block else data


def _deflate_raw(data):
    dc = zlib.decompressobj(-15)
    try:
        out = dc.decompress(data)
        extra = dc.decompress(b"Z") + dc.flush()
        if extra:
            out += extra
        return out
    except zlib.error:
        return data


def _remove_hardening(rights, keytype, keydata):
    """RMSDK>=10 'hardened' ADEPT: derive kek from keyType, IV from resource/device/fulfillment uuids."""
    text = lambda name: rights.findtext(".//%s" % _ad(name)) or ""
    import hashlib
    from uuid import UUID
    resource = UUID(text("resource"))
    device = UUID(text("device"))
    fulfill = UUID(text("fulfillment")[:36])
    kekiv = UUID(int=resource.int ^ device.int ^ fulfill.int).bytes
    rem = int(keytype, 10) % 16
    h = hashlib.sha256(keytype.encode("ascii")).digest()
    kek = h[2 * rem:16 + rem] + h[rem:2 * rem]
    return _unpad(AES.new(kek, AES.MODE_CBC, kekiv).decrypt(keydata))


class Decryptor:
    """Decrypt entry by entry per encryption.xml markers (shared AES-128-CBC continuous stream)."""

    def __init__(self, bookkey, encryption: bytes):
        self._aes = AES.new(bookkey, AES.MODE_CBC, b"\x00" * 16)
        self._encryption = encryption
        self._encrypted = set()      # encrypted entries needing inflate (bytes key)
        self._no_decomp = set()      # encrypted but not inflated (video etc.)
        self._other = set()          # unrecognized algorithm -> kept verbatim
        self._tree = None
        self._parse()

    def _parse(self):
        if not self._encryption:
            return
        root = ET.fromstring(self._encryption)
        self._tree = root
        for enc in list(root.iter(_en("EncryptedData"))):
            method = enc.find("./%s" % _en("EncryptionMethod"))
            cref = enc.find("./%s/%s" % (_en("CipherData"), _en("CipherReference")))
            if method is None or cref is None:
                continue
            algo = method.get("Algorithm", "")
            uri = cref.get("URI")
            if not uri:
                continue
            key = uri.encode("utf-8")
            if algo == "http://www.w3.org/2001/04/xmlenc#aes128-cbc":
                self._encrypted.add(key)
                root.remove(enc)
            elif algo == "http://ns.adobe.com/adept/xmlenc#aes128-cbc-uncompressed":
                self._no_decomp.add(key)
                root.remove(enc)
            else:
                self._other.add(key)

    def has_remaining_xml(self):
        return bool(self._other)

    def get_xml(self) -> bytes:
        if self._tree is None:
            return self._encryption
        data = ET.tostring(self._tree, encoding="utf-8", xml_declaration=False)
        return b'<?xml version="1.0" encoding="UTF-8"?>\n' + data

    def is_encrypted(self, path):
        return path.encode("utf-8") in self._encrypted or path.encode("utf-8") in self._no_decomp

    def decrypt(self, path, data) -> bytes:
        key = path.encode("utf-8")
        if key not in self._encrypted and key not in self._no_decomp:
            return data
        out = self._aes.decrypt(data)[16:]      # drop first block
        out = _unpad(out)
        if key not in self._no_decomp:
            out = _deflate_raw(out)
        return out


def decrypt_epub(epub_bytes: bytes, userkey: bytes) -> bytes:
    """Decrypt an ADEPT-encrypted epub. userkey: ADE account RSA private key DER (PKCS#1 / PKCS#8).

    Returns plaintext epub bytes. Raises for non-ADEPT encryption (missing rights.xml/encryption.xml).
    """
    zp = zipfile.ZipFile(io.BytesIO(epub_bytes))
    names = zp.namelist()
    if "META-INF/rights.xml" not in names or "META-INF/encryption.xml" not in names:
        raise RuntimeError("ADEPT: 非 ADEPT 加密 epub（缺 META-INF/rights.xml 或 encryption.xml）")

    rights = ET.fromstring(zp.read("META-INF/rights.xml"))
    ek_elem = rights.find(".//%s" % _ad("encryptedKey"))
    if ek_elem is None:
        raise RuntimeError("ADEPT: rights.xml 缺 encryptedKey")
    keytext = (ek_elem.text or "").strip()
    keytype = ek_elem.attrib.get("keyType", "0")
    bookkey_b64 = base64.b64decode(keytext)

    if len(keytext) == 64:
        # Adobe PassHash / B&N -- not Kobo (Kobo AC4 is standard ADEPT); minimal implementation kept
        key = base64.b64decode(userkey)[:16]
        bookkey = _unpad(AES.new(key, AES.MODE_CBC, b"\x00" * 16).decrypt(bookkey_b64))
        if len(bookkey) > 16:
            bookkey = bookkey[-16:]
    else:
        rsakey = RSA.importKey(userkey)
        try:
            if int(keytype, 10) > 2:
                bookkey_b64 = _remove_hardening(rights, keytype, bookkey_b64)
            bookkey = PKCS1_v1_5.new(rsakey).decrypt(bookkey_b64, None)
        except (ValueError, TypeError):
            bookkey = None
        if not bookkey:
            raise RuntimeError("ADEPT: 无法解密 bookkey（密钥不匹配）")

    decryptor = Decryptor(bookkey, zp.read("META-INF/encryption.xml"))
    body_names = [n for n in names if n not in _META_NAMES]
    buf = io.BytesIO()
    out = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    for path in ["mimetype"] + body_names:
        data = zp.read(path)
        zi = zipfile.ZipInfo(path)
        zi.compress_type = zipfile.ZIP_DEFLATED
        if path == "mimetype":
            zi.compress_type = zipfile.ZIP_STORED
        try:
            old = zp.getinfo(path)
            zi.date_time = old.date_time
        except KeyError:
            pass
        if path == "META-INF/encryption.xml":
            if decryptor.has_remaining_xml():
                data = decryptor.get_xml()
            else:
                continue
            out.writestr(zi, data)
        else:
            out.writestr(zi, decryptor.decrypt(path, data))
    out.close()
    return buf.getvalue()
