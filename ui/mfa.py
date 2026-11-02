"""Authenticator-app codes (TOTP, RFC 6238) and the encryption of their seeds, for the Grid UI's MFA secrets.

store.py keeps each seed in mfaSecrets encrypted with AES-256-GCM under a key derived from
GRID_SECRETS_KEY; app.py hands codes made from them to checks. Nothing here touches the database, and no
function returns or logs a seed except decrypt(), whose result only ever goes into code().

The saved sign-ins (savedSignins) are encrypted the same way by seal() and unseal(), under a key of their
own derived from the same GRID_SECRETS_KEY.
"""
import base64
import binascii
import hashlib
import hmac
import os
import re
import struct
import urllib.parse

KEY_ENV = "GRID_SECRETS_KEY"
# Shorter keys are refused: the key is the only thing protecting the seeds in the database.
MIN_KEY_LENGTH = 16
ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
DIGITS = (6, 7, 8)
MIN_PERIOD_S, MAX_PERIOD_S = 10, 300
# 80 bits is what many sites hand out (16 base32 characters); RFC 4226 asks for at least 128.
MIN_SEED_BYTES, MAX_SEED_BYTES = 10, 128
_BASE32 = re.compile(r"[A-Z2-7]+\Z")
_NONCE_BYTES = 12
# What GRID_SECRETS_KEY encrypts: kind -> (the HKDF info its key is derived with, the prefix of the associated
# data, what the key is needed for). A key per kind: a ciphertext of one kind never decrypts as another.
_KINDS = {
    "mfa": (b"playwright-grid-ui mfa seeds v1", b"mfa:", "store MFA secrets"),
    "signin": (b"playwright-grid-ui saved sign-ins v1", b"signin:", "save sign-ins"),
}


class InvalidSecret(ValueError):
    """A seed, otpauth:// URI or setting the UI can't use. The message never quotes the seed."""


class SecretsKeyMissing(Exception):
    """GRID_SECRETS_KEY is unset, too short, or the cryptography package is missing."""


class CannotDecrypt(Exception):
    """The stored seed (or sign-in) doesn't decrypt under the current key (GRID_SECRETS_KEY changed, or the
    row was tampered with)."""


def key_problem(kind="mfa"):
    """Why seeds (kind "signin": saved sign-ins) can't be stored or read right now, or None when they can."""
    purpose = _KINDS[kind][2]
    key = os.environ.get(KEY_ENV, "")
    if not key:
        return "Set {} to {}.".format(KEY_ENV, purpose)
    if len(key) < MIN_KEY_LENGTH:
        return "{} must be at least {} characters long.".format(KEY_ENV, MIN_KEY_LENGTH)
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return "The Grid UI needs the cryptography package to {}.".format(purpose)
    return None


def _aead(kind):
    problem = key_problem(kind)
    if problem:
        raise SecretsKeyMissing(problem)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=_KINDS[kind][0]).derive(os.environ[KEY_ENV].encode())
    return AESGCM(key)


def _associated(name, kind):
    return _KINDS[kind][1] + name.encode()


def seal(plaintext, name, kind="mfa"):
    """The stored form of plaintext (bytes): base64 of nonce + AES-GCM ciphertext, bound to the name."""
    nonce = os.urandom(_NONCE_BYTES)
    return base64.b64encode(nonce + _aead(kind).encrypt(nonce, plaintext, _associated(name, kind))).decode()


def unseal(stored, name, kind="mfa"):
    """The plaintext seal() stored under that name, as bytes."""
    aead = _aead(kind)  # SecretsKeyMissing, not an ImportError, without the cryptography package
    from cryptography.exceptions import InvalidTag
    try:
        raw = base64.b64decode(stored, validate=True)
        return aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _associated(name, kind))
    except (InvalidTag, binascii.Error, ValueError):
        raise CannotDecrypt("{!r} doesn't decrypt with the current {}: the key changed since it was stored, or "
                            "the row was altered.".format(name, KEY_ENV)) from None


def encrypt(seed, name):
    """The stored form of a normalized seed: base64 of nonce + AES-GCM ciphertext, bound to the name."""
    return seal(seed.encode(), name)


def decrypt(stored, name):
    try:
        return unseal(stored, name).decode()
    except (CannotDecrypt, UnicodeDecodeError):
        raise CannotDecrypt("The seed of {!r} doesn't decrypt with the current {}: the key changed since it was "
                            "stored. Set the old key again, or delete the secret and add it again.".format(name, KEY_ENV)) from None


def normalize_seed(seed):
    """A base32 seed as authenticator apps accept it (any case, spaces, dashes, padding) -> canonical form."""
    if not isinstance(seed, str):
        raise InvalidSecret("Give the secret's seed as text (base32), or an otpauth:// URI.")
    canonical = re.sub(r"[\s=-]", "", seed).upper()
    if not canonical or not _BASE32.match(canonical):
        raise InvalidSecret("The seed must be base32: the letters A-Z and the digits 2-7.")
    size = len(seed_bytes(canonical))
    if not MIN_SEED_BYTES <= size <= MAX_SEED_BYTES:
        raise InvalidSecret("The seed must be {} to {} bytes long ({} to {} base32 characters).".format(
            MIN_SEED_BYTES, MAX_SEED_BYTES, -(-MIN_SEED_BYTES * 8 // 5), MAX_SEED_BYTES * 8 // 5))
    return canonical


def seed_bytes(canonical):
    try:
        return base64.b32decode(canonical + "=" * (-len(canonical) % 8))
    except binascii.Error:
        raise InvalidSecret("The seed must be base32: the letters A-Z and the digits 2-7.") from None


def validate_settings(algorithm, digits, period):
    algorithm = (algorithm or "SHA1").upper() if isinstance(algorithm, str) or algorithm is None else algorithm
    if not isinstance(algorithm, str) or algorithm not in ALGORITHMS:  # a list isn't even hashable
        raise InvalidSecret("algorithm must be SHA1, SHA256 or SHA512.")
    digits = 6 if digits is None else digits
    if isinstance(digits, bool) or digits not in DIGITS:
        raise InvalidSecret("digits must be 6, 7 or 8.")
    period = 30 if period is None else period
    if isinstance(period, bool) or not isinstance(period, int) or not MIN_PERIOD_S <= period <= MAX_PERIOD_S:
        raise InvalidSecret("period must be a whole number of seconds from {} to {}.".format(MIN_PERIOD_S, MAX_PERIOD_S))
    return algorithm, digits, period


def parse_otpauth(uri):
    """The settings in an otpauth://totp/ URI (what an enrolment QR code holds):
    {"seed", "issuer", "account", "algorithm", "digits", "period"}."""
    parts = urllib.parse.urlsplit(uri.strip())
    if parts.scheme.lower() != "otpauth":
        raise InvalidSecret("An enrolment URI starts with otpauth://.")
    if parts.netloc.lower() != "totp":
        raise InvalidSecret("Only time-based codes (otpauth://totp/) are supported, not counter-based ones (hotp).")
    query = dict(urllib.parse.parse_qsl(parts.query))
    label = urllib.parse.unquote(parts.path.lstrip("/"))
    issuer, _, account = label.rpartition(":")
    issuer = query.get("issuer") or issuer.strip() or None
    try:
        digits = int(query["digits"]) if "digits" in query else None
        period = int(query["period"]) if "period" in query else None
    except ValueError:
        raise InvalidSecret("digits and period in the URI must be numbers.") from None
    algorithm, digits, period = validate_settings(query.get("algorithm"), digits, period)
    return {"seed": normalize_seed(query.get("secret", "")), "issuer": issuer, "account": account.strip() or None,
            "algorithm": algorithm, "digits": digits, "period": period}


def step_at(t, period):
    return int(t // period)


def code(canonical_seed, step, digits=6, algorithm="SHA1"):
    """The code of a time step: RFC 4226 HOTP with the step number as the counter."""
    mac = hmac.new(seed_bytes(canonical_seed), struct.pack(">Q", step), ALGORITHMS[algorithm]).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)
