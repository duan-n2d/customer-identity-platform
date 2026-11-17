"""Normalisation of identifiers: Vietnamese phones, emails, names, dates of birth, and tokens.

Design rules:
* Pure functions, no I/O, so they are property-testable.
* Normalisation never *invents* data. If an identifier cannot be trusted it becomes ``None``
  (and the reason is returned), so downstream matching treats it as missing rather than as
  evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

# --------------------------------------------------------------------------- phones
# Vietnamese mobile prefixes (first three digits of the 10-digit national number).
_VN_MOBILE_PREFIXES = frozenset(
    {f"03{d}" for d in range(2, 10)}
    | {"052", "055", "056", "058", "059"}
    | {"070", "076", "077", "078", "079"}
    | {f"08{d}" for d in range(1, 10)}
    | {f"09{d}" for d in range(0, 10)}
)

# Placeholders that appear constantly in real exports and must never act as evidence.
_PLACEHOLDER_PHONES = frozenset(
    {"0123456789", "0987654321", "0900000000", "0911111111", "0999999999", "0987654320"}
)


@dataclass(frozen=True)
class PhoneResult:
    e164: str | None
    valid: bool
    reason: str  # "ok" or why the value was rejected


def _has_long_run(digits: str, n: int = 7) -> bool:
    """True for placeholder-looking numbers: >= n identical digits, or an ascending/descending run."""
    if re.search(rf"(\d)\1{{{n - 1},}}", digits):
        return True
    for i in range(len(digits) - n + 1):
        window = digits[i : i + n]
        steps = {int(b) - int(a) for a, b in zip(window, window[1:], strict=False)}
        if steps in ({1}, {-1}):
            return True
    return False


def normalize_phone(raw: str | None) -> PhoneResult:
    """Normalise a Vietnamese mobile number to E.164 (``+84XXXXXXXXX``).

    Handles ``0901234567``, ``+84 901 234 567``, ``84901234567``, ``0084901234567``,
    separators (spaces, dots, dashes), and the nine-digit form that appears when a spreadsheet
    dropped the leading zero.
    """
    if raw is None:
        return PhoneResult(None, False, "missing")
    text = str(raw).strip()
    if not text:
        return PhoneResult(None, False, "missing")
    digits = re.sub(r"\D", "", text)
    if text.startswith("+") and digits.startswith("84"):
        national = "0" + digits[2:]
    elif digits.startswith("0084"):
        national = "0" + digits[4:]
    elif digits.startswith("84") and len(digits) == 11:
        national = "0" + digits[2:]
    elif len(digits) == 9 and digits[0] in "35789":
        national = "0" + digits  # leading zero lost by a spreadsheet export
    else:
        national = digits
    if len(national) != 10 or not national.startswith("0"):
        return PhoneResult(None, False, "bad_length")
    if national in _PLACEHOLDER_PHONES or _has_long_run(national[1:]):
        return PhoneResult(None, False, "placeholder")
    if national[:3] not in _VN_MOBILE_PREFIXES:
        return PhoneResult(None, False, "unknown_prefix")
    return PhoneResult("+84" + national[1:], True, "ok")


# --------------------------------------------------------------------------- email
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)+$")
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def normalize_email(raw: str | None) -> str | None:
    """Lower-case, drop ``+tag`` sub-addressing, and fold Gmail dots. Returns None if invalid."""
    if raw is None:
        return None
    text = str(raw).strip().lower().replace(" ", "")
    if not _EMAIL_RE.match(text):
        return None
    local, domain = text.split("@", 1)
    local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        domain = "gmail.com"
        local = local.replace(".", "")
    return f"{local}@{domain}" if local else None


# --------------------------------------------------------------------------- names
def strip_diacritics(text: str) -> str:
    """Remove Vietnamese diacritics (including the stroked d, which NFD does not decompose)."""
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def name_tokens(raw: str | None) -> list[str]:
    """Lower-case, diacritic-free tokens of a personal name. Digits and punctuation are dropped."""
    if raw is None:
        return []
    text = strip_diacritics(str(raw)).lower()
    text = re.sub(r"[^a-z\s]", " ", text)
    return text.split()


def name_key(raw: str | None) -> str | None:
    """Order-insensitive key: sorted tokens. ``Nguyen Van An`` and ``An Nguyen Van`` collide.

    Vietnamese records mix family-name-first and given-name-first orderings across systems, so
    comparing a sorted token key is more robust than guessing which token is which.
    """
    tokens = name_tokens(raw)
    if len(tokens) < 2:  # a single token is not enough to identify anyone
        return None
    return " ".join(sorted(tokens))


# --------------------------------------------------------------------------- dates of birth
_DOB_FORMATS = (
    re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"),  # ISO
    re.compile(r"^(\d{2})/(\d{2})/(\d{4})$"),  # DD/MM/YYYY
)


def normalize_dob(raw: str | None, today: date | None = None) -> str | None:
    """Return an ISO date string or None. Rejects implausible ages (before 1900 or in the future)."""
    if raw is None:
        return None
    text = str(raw).strip()
    today = today or date.today()
    parsed: date | None = None
    try:
        if m := _DOB_FORMATS[0].match(text):
            parsed = date(int(m[1]), int(m[2]), int(m[3]))
        elif m := _DOB_FORMATS[1].match(text):
            parsed = date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None
    if parsed is None or parsed.year < 1900 or parsed > today:
        return None
    return parsed.isoformat()


def dob_compatible(a: str | None, b: str | None) -> bool:
    """False only when both dates are known and clearly different.

    A day/month swap (a common data-entry mistake) is treated as compatible, so one typo does
    not make two records of the same person impossible to link.
    """
    if a is None or b is None or a == b:
        return True
    ya, ma, da = a.split("-")
    yb, mb, db = b.split("-")
    return ya == yb and ma == db and da == mb


# --------------------------------------------------------------------------- tokens
def token(value: str | None, key: str) -> str | None:
    """Keyed, non-reversible token of an identifier (HMAC-SHA256, truncated to 32 hex chars).

    A plain hash of a phone number is trivially reversible by enumeration (there are only ~10^9
    numbers), so a secret key is required.
    """
    if value is None:
        return None
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]
