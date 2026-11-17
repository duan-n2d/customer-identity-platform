"""Controlled, seeded noise applied to identifiers the way real systems corrupt them."""

from __future__ import annotations

import numpy as np

from cip.normalize import strip_diacritics

_KEYBOARD_NEIGHBOURS = {
    "a": "qwsz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr", "f": "drtgvc",
    "g": "ftyhbv", "h": "gyujnb", "i": "ujko", "j": "huikmn", "k": "jiolm", "l": "kop",
    "m": "njk", "n": "bhjm", "o": "iklp", "p": "ol", "q": "wa", "r": "edft", "s": "awedxz",
    "t": "rfgy", "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu", "z": "asx",
}  # fmt: skip


def typo(rng: np.random.Generator, text: str) -> str:
    """One realistic typing error: swap, drop, or substitute with a neighbouring key."""
    letters = [i for i, c in enumerate(text) if c.isalpha()]
    if len(letters) < 3:
        return text
    i = int(rng.choice(letters[1:]))  # never corrupt the first letter (people type that right)
    kind = rng.integers(0, 3)
    chars = list(text)
    if kind == 0 and i + 1 < len(chars):
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    elif kind == 1:
        del chars[i]
    else:
        neighbours = _KEYBOARD_NEIGHBOURS.get(strip_diacritics(chars[i]).lower())
        if neighbours:
            chars[i] = str(rng.choice(list(neighbours)))
    return "".join(chars)


def noisy_name(
    rng: np.random.Generator,
    family: str,
    middle: str,
    given: str,
    *,
    p_drop_diacritics: float,
    p_upper: float,
    p_given_first: float,
    p_drop_middle: float,
    p_typo: float,
) -> str:
    """Render a full name the way one source might store it."""
    tokens = [family, middle, given]
    if rng.random() < p_drop_middle:
        tokens = [family, given]
    if rng.random() < p_given_first:
        tokens = list(reversed(tokens))  # Western ordering: given name first
    name = " ".join(tokens)
    if rng.random() < p_drop_diacritics:
        name = strip_diacritics(name)
    if rng.random() < p_typo:
        name = typo(rng, name)
    if rng.random() < p_upper:
        name = name.upper()
    return name


def format_phone(rng: np.random.Generator, national: str) -> str:
    """Render a 10-digit number ``0XXXXXXXXX`` in one of the formats seen in real exports."""
    d = national
    forms = [
        d,
        d,
        d,  # plain form is the most common
        "+84" + d[1:],
        "84" + d[1:],
        f"{d[:3]} {d[3:6]} {d[6:]}",
        f"{d[:4]}.{d[4:7]}.{d[7:]}",
        f"{d[:4]}-{d[4:7]}-{d[7:]}",
        d[1:],  # leading zero lost by a spreadsheet
    ]
    return forms[int(rng.integers(0, len(forms)))]


def digit_typo(rng: np.random.Generator, national: str) -> str:
    """Change one digit of a phone number (keeps the result a plausible-looking number)."""
    i = int(rng.integers(3, len(national)))
    new = str((int(national[i]) + int(rng.integers(1, 10))) % 10)
    return national[:i] + new + national[i + 1 :]


def format_dob(rng: np.random.Generator, iso: str, p_swap: float) -> str:
    """Render a DOB as ISO or DD/MM/YYYY, occasionally with day and month swapped."""
    y, m, d = iso.split("-")
    if rng.random() < p_swap and int(d) <= 12:
        m, d = d, m  # only when the swapped date is still a valid date
    return f"{y}-{m}-{d}" if rng.random() < 0.5 else f"{d}/{m}/{y}"
