"""Adobe ADEPT client crypto/signature layer (shared by anonymous device activation and .acsm fulfillment).

MIT independent rewrite of the public libgourou/DeDRM algorithms (no GPL code copied). Pure local pycryptodome;
sign_node applies a PKCS#1 v1.5 textbook RSA signature over the Adobe canonical hash of an XML node.
Locally unit-testable: serial/fingerprint/nonce/hash_node/sign_node.
"""
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes

_ADEPT_NS = "http://ns.adobe.com/adept"

# Adobe canonical hash markers (ASN tags)
_ASN_NS_TAG = 1
_ASN_CHILD = 2
_ASN_END_TAG = 3
_ASN_TEXT = 4
_ASN_ATTRIBUTE = 5


def make_serial() -> str:
    """Random device serial: sha1(256 random bytes) as lowercase hex."""
    return hashlib.sha1(get_random_bytes(256)).hexdigest().lower()


def make_fingerprint(serial: str, devsalt: bytes) -> str:
    """Device fingerprint: base64(sha1(serial + devsalt)), <=20B."""
    digest = hashlib.sha1((serial + devsalt.decode("latin-1")).encode("latin-1")).digest()
    return base64.b64encode(digest)


def encrypt_with_device_key(data: bytes, devsalt: bytes) -> bytes:
    """AES-256-CBC, random IV, PKCS7; returns iv || encrypted."""
    iv = get_random_bytes(16)
    pad = 16 - len(data) % 16 or 16
    padded = data + bytes([pad]) * pad
    return iv + AES.new(devsalt, AES.MODE_CBC, iv).encrypt(padded)


def decrypt_with_device_key(data: bytes, devsalt: bytes) -> bytes:
    """Decrypt IV(v16) + AES-CBC(devsalt), strips PKCS7."""
    plain = AES.new(devsalt, AES.MODE_CBC, data[:16]).decrypt(data[16:])
    return plain[:-plain[-1]] if plain[-1] <= 16 else plain


def add_nonce(now=None) -> tuple[str, str]:
    """Returns (nonce_b64, expiration). nonce = base64(8B little-endian unix ms + 62167219200000 || 4B little-endian 0)."""
    dt = now or datetime.now(timezone.utc)
    sec = int((dt - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1000) + 62167219200000
    payload = sec.to_bytes(8, "little") + b"\x00" * 4
    exp = (dt + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return base64.b64encode(payload).decode("utf-8"), exp


# ---- XML canonical hash (recursive over nodes; order: ns, name, attrs, CHILD, text, children, END_TAG) ----
def _hash_do_append_string(ctx, s):
    b = s.encode("utf-8")
    ctx.update(bytes([len(b) // 256, len(b) & 0xFF]) + b)


def _hash_do_append_tag(ctx, tag):
    if tag <= 5:
        ctx.update(bytes([tag]))


def _hash_node_ctx(node, ctx):
    ns, local = node.tag.rsplit("}", 1) if "}" in node.tag else ("", node.tag)
    ns = ns.lstrip("{")   # critical: rsplit leaves the Clark-notation leading `{`; unstripped, the namespace gains 1 byte and every signature fails
    if local in ("hmac", "signature") and ns == _ADEPT_NS:
        return           # Adobe hmac/signature excluded from the hash
    _hash_do_append_tag(ctx, _ASN_NS_TAG)
    _hash_do_append_string(ctx, ns)
    _hash_do_append_string(ctx, local)
    for attr in sorted(node.keys()):
        ans, aloc = attr.rsplit("}", 1) if "}" in attr else ("", attr)
        ans = ans.lstrip("{")
        _hash_do_append_tag(ctx, _ASN_ATTRIBUTE)
        _hash_do_append_string(ctx, ans)
        _hash_do_append_string(ctx, aloc)
        _hash_do_append_string(ctx, node.get(attr))
    _hash_do_append_tag(ctx, _ASN_CHILD)
    if node.text and node.text.strip():
        text = node.text.strip()
        for i in range(0, len(text), 0x7FFF):
            _hash_do_append_tag(ctx, _ASN_TEXT)
            _hash_do_append_string(ctx, text[i:i + 0x7FFF])
    for child in node:
        if isinstance(child, ET.Element):
            _hash_node_ctx(child, ctx)
    _hash_do_append_tag(ctx, _ASN_END_TAG)


def hash_node(node) -> bytes:
    """Adobe canonical hash (SHA1) of an XML node; returns digest bytes. node is an ElementTree element."""
    ctx = hashlib.sha1()
    _hash_node_ctx(node, ctx)
    return ctx.digest()


# ---- textbook RSA signature (PKCS#1 v1.5): 00 01 FF.. 00 message, pow(message, d, n) ----
def rsapad_sign(priv_der: bytes, message: bytes) -> bytes:
    key = RSA.importKey(priv_der)
    keylen = (key.n.bit_length() + 7) // 8
    if len(message) > keylen - 11:
        raise ValueError("message too long for RSA sign")
    padlen = keylen - len(message) - 3
    block = b"\x00\x01" + b"\xff" * padlen + b"\x00" + message
    m = int.from_bytes(block, "big")
    if m >= key.n:
        raise ValueError("message block >= modulus")
    return pow(m, key.d, key.n).to_bytes(keylen, "big")


def sign_node(node, priv_der: bytes) -> str:
    """ADEPT signature: PKCS#1 v1.5 wraps the SHA1 digest directly (**no DigestInfo**).

    libgourou padWithPKCS1 / DeDRM pad_message only build `00 01 FF..FF 00 <20B hash>`; adding DigestInfo makes
    Adobe report "unparsable" -> E_ADEPT_USER_AUTH (a previously hit pitfall).
    """
    sig = rsapad_sign(priv_der, hash_node(node))
    return base64.b64encode(bytes(sig)).decode("utf-8")


# ---- self-generated device RSA pair ----
def make_device_keypair() -> bytes:
    """Generate a 2048-bit device RSA private key (DER, PKCS#8) for activateDevice/fulfill signing."""
    return RSA.generate(2048).exportKey("DER")
