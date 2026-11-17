"""Synthetic world of customers, with the traps that make identity resolution hard.

The generator produces *source-system records* plus a hidden ground truth (which person each
record really belongs to). The pipeline never sees the truth; only the evaluation does.

Traps deliberately built in (each is tagged in the truth so the report can break errors down):

* ``household``     family members share one phone and a family name (different people!)
* ``store_phone``   cashiers type the *store's* number for customers who gave none, so a handful
                    of numbers appear on thousands of unrelated records
* ``recycled``      a phone number is re-issued to a different person after the first owner
                    abandons it (same number, two people, different time windows)
* ``common_name``   very frequent names collide between unrelated people
* ``phone_changed`` the same person uses different numbers over time (only email/name/dob link
                    these records)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from cip.generator import vn_data
from cip.generator.noise import digit_typo, format_dob, format_phone, noisy_name, typo
from cip.normalize import name_key, normalize_phone, strip_diacritics

BASE_DATE = date(2026, 6, 1)  # pipeline day 0
PLACEHOLDER_NUMBERS = ["0123456789", "0900000000", "0911111111", "0987654321"]


@dataclass
class WorldConfig:
    n_persons: int = 6000
    seed: int = 7
    days: int = 4  # incremental pipeline days after the initial load (day 0)
    backlog_days: int = 730
    household_share: float = 0.12
    recycled_share: float = 0.03
    phone_changed_share: float = 0.08
    n_stores: int = 30
    daily_new_person_rate: float = 0.01
    daily_event_rate: float = 0.006
    late_arrival_share: float = 0.05
    drift_day: int | None = (
        None  # from this day the CRM feed renames `phone` (applied when writing landing files)
    )
    # per-source noise
    p_pos_no_phone: float = 0.40
    p_pos_store_phone: float = (
        0.50  # of those without a phone, share where the cashier typed the store number
    )
    p_pos_placeholder: float = 0.03
    p_pos_no_name: float = 0.25
    p_phone_digit_typo: float = 0.015
    p_dob_swap: float = 0.02


@dataclass
class PhoneSpan:
    number: str  # national format, 10 digits
    start_day: int
    end_day: int | None = None  # exclusive


@dataclass
class Person:
    pid: str
    family: str
    middle: str
    given: str
    sex: str
    dob: str
    emails: list[str] = field(default_factory=list)
    phones: list[PhoneSpan] = field(default_factory=list)
    household: str | None = None
    first_day: int = 0
    flags: set[str] = field(default_factory=set)

    @property
    def full_name(self) -> str:
        return f"{self.family} {self.middle} {self.given}"


def _weighted(rng: np.random.Generator, items: list[tuple[str, float]]) -> str:
    names = [n for n, _ in items]
    w = np.array([x for _, x in items], dtype=float)
    return names[int(rng.choice(len(names), p=w / w.sum()))]


class World:
    def __init__(self, cfg: WorldConfig):
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.seed)
        self.persons: list[Person] = []
        self.households: dict[str, str] = {}  # household id -> shared phone (national)
        self.store_phones: list[str] = []
        self._used_numbers: set[str] = set()
        self._used_emails: set[str] = set()
        self._build()

    # ------------------------------------------------------------------ construction
    def _new_number(self) -> str:
        while True:
            prefix = vn_data.MOBILE_PREFIXES[
                int(self.rng.integers(0, len(vn_data.MOBILE_PREFIXES)))
            ]
            number = prefix + "".join(str(int(x)) for x in self.rng.integers(0, 10, size=7))
            if number in self._used_numbers or not normalize_phone(number).valid:
                continue
            self._used_numbers.add(number)
            return number

    def _new_email(self, p: Person) -> str:
        local_base = strip_diacritics(f"{p.given}.{p.family}").lower().replace(" ", "")
        while True:
            style = int(self.rng.integers(0, 3))
            suffix = str(int(self.rng.integers(1, 99)))
            local = [
                f"{local_base}{suffix}",
                f"{local_base.replace('.', '')}{suffix}",
                f"{p.given.lower()}{p.dob[2:4]}{suffix}",
            ][style]
            local = strip_diacritics(local).lower()
            email = f"{local}@{_weighted(self.rng, vn_data.EMAIL_DOMAINS)}"
            if email not in self._used_emails:
                self._used_emails.add(email)
                return email

    def _make_person(self, idx: int, first_day: int) -> Person:
        sex = "M" if self.rng.random() < 0.5 else "F"
        middle_pool = vn_data.MIDDLE_NAMES_M if sex == "M" else vn_data.MIDDLE_NAMES_F
        given_pool = vn_data.GIVEN_NAMES_M if sex == "M" else vn_data.GIVEN_NAMES_F
        year = int(self.rng.integers(1960, 2008))
        month, day = int(self.rng.integers(1, 13)), int(self.rng.integers(1, 29))
        p = Person(
            pid=f"T{idx:07d}",
            family=_weighted(self.rng, vn_data.FAMILY_NAMES),
            middle=str(self.rng.choice(middle_pool)),
            given=str(self.rng.choice(given_pool)),
            sex=sex,
            dob=f"{year:04d}-{month:02d}-{day:02d}",
            first_day=first_day,
        )
        return p

    def _build(self) -> None:
        cfg, rng = self.cfg, self.rng
        # day-0 population plus the people who first appear during the incremental days
        n_new_per_day = max(1, round(cfg.daily_new_person_rate * cfg.n_persons))
        for i in range(cfg.n_persons):
            self.persons.append(self._make_person(i, int(rng.integers(-cfg.backlog_days, 1))))
        for d in range(1, cfg.days + 1):
            for _ in range(n_new_per_day):
                self.persons.append(self._make_person(len(self.persons), d))

        # households: 2-4 members share a family name and a phone
        order = [int(i) for i in rng.permutation(cfg.n_persons)]
        n_house_members = int(cfg.household_share * cfg.n_persons)
        cursor, hid = 0, 0
        while cursor + 2 <= n_house_members:
            size = int(rng.integers(2, 5))
            members = [self.persons[i] for i in order[cursor : cursor + size]]
            cursor += size
            hid += 1
            hh = f"H{hid:05d}"
            shared = self._new_number()
            self.households[hh] = shared
            family = members[0].family
            for m in members:
                m.family, m.household = family, hh
                m.flags.add("household")

        # own phones; household members often rely on the shared number instead
        for p in self.persons:
            keep_own = p.household is None or rng.random() < 0.6
            if keep_own:
                p.phones.append(PhoneSpan(self._new_number(), start_day=-10_000))
            if rng.random() < 0.20:
                p.phones.append(PhoneSpan(self._new_number(), start_day=-10_000))

        # phone changes: same person, new number from a cut-over day
        changers = rng.choice(
            cfg.n_persons, size=int(cfg.phone_changed_share * cfg.n_persons), replace=False
        )
        for i in changers:
            p = self.persons[int(i)]
            if not p.phones:
                continue
            cut = int(rng.integers(-400, -30))
            p.phones[0].end_day = cut
            p.phones.append(PhoneSpan(self._new_number(), start_day=cut))
            p.flags.add("phone_changed")

        # recycled numbers: a new owner inherits a number the old owner abandoned
        n_recycled = int(cfg.recycled_share * cfg.n_persons)
        pool = [int(i) for i in rng.permutation(cfg.n_persons)]
        for _ in range(n_recycled):
            old, new = self.persons[pool.pop()], self.persons[pool.pop()]
            if not old.phones or old.household or new.household or "phone_changed" in old.flags:
                continue
            cut = int(rng.integers(-400, -60))
            abandoned = old.phones[0].number
            old.phones[0].end_day = cut
            old.phones.append(PhoneSpan(self._new_number(), start_day=cut))
            new.phones = [PhoneSpan(abandoned, start_day=cut + 30)]
            new.first_day = max(new.first_day, cut + 30)
            new.flags.discard("phone_changed")  # its phones were just replaced
            old.flags.add("recycled")
            new.flags.add("recycled")

        # emails
        for p in self.persons:
            r = rng.random()
            n = 0 if r < 0.40 else (1 if r < 0.85 else 2)
            p.emails = [self._new_email(p) for _ in range(n)]

        self.store_phones = [self._new_number() for _ in range(cfg.n_stores)]

    # ------------------------------------------------------------------ helpers
    def phone_at(self, p: Person, day: int) -> tuple[str | None, str]:
        """The number this person would give on ``day``, and which kind of number it is."""
        active = [
            s for s in p.phones if s.start_day <= day and (s.end_day is None or day < s.end_day)
        ]
        shared = self.households.get(p.household) if p.household else None
        if shared and (not active or self.rng.random() < 0.4):
            return shared, "household"
        if not active:
            return None, "none"
        span = active[0] if len(active) == 1 or self.rng.random() < 0.85 else active[1]
        return span.number, "own"


def _day_to_date(day: int) -> date:
    return BASE_DATE + timedelta(days=day)


def _ingested_at(day: int) -> str:
    return f"{_day_to_date(day).isoformat()}T02:00:00"


def _email_noise(rng: np.random.Generator, email: str) -> str:
    local, domain = email.split("@")
    r = rng.random()
    if r < 0.30:
        return f"{local}@{domain.upper()}"
    if r < 0.35:
        return f"{local}+shop@{domain}"
    if r < 0.36:
        return f"{local}@{domain.replace('gmail', 'gmial')}"
    if r < 0.37:
        return f"{typo(rng, local)}@{domain}"
    return email


# ---------------------------------------------------------------------------------------
# source-record builders. Each returns (row, truth_flags)
# ---------------------------------------------------------------------------------------
_SOURCE_NAME_NOISE = {
    "pos": dict(
        p_drop_diacritics=0.70, p_upper=0.20, p_given_first=0.05, p_drop_middle=0.30, p_typo=0.08
    ),
    "ecom": dict(
        p_drop_diacritics=0.40, p_upper=0.05, p_given_first=0.15, p_drop_middle=0.20, p_typo=0.04
    ),
    "crm": dict(
        p_drop_diacritics=0.15, p_upper=0.02, p_given_first=0.03, p_drop_middle=0.10, p_typo=0.03
    ),
    "loyalty": dict(
        p_drop_diacritics=0.25, p_upper=0.10, p_given_first=0.05, p_drop_middle=0.15, p_typo=0.03
    ),
}


def _name(w: World, p: Person, source: str) -> str:
    return noisy_name(w.rng, p.family, p.middle, p.given, **_SOURCE_NAME_NOISE[source])


def _phone_text(w: World, number: str | None) -> str | None:
    if number is None:
        return None
    if w.rng.random() < w.cfg.p_phone_digit_typo:
        number = digit_typo(w.rng, number)
    return format_phone(w.rng, number)


def build_record(w: World, p: Person, source: str, day: int, seq: int) -> tuple[dict, dict]:
    rng, cfg = w.rng, w.cfg
    flags = {"uses_household_phone": False, "uses_store_phone": False}
    event_date = _day_to_date(day).isoformat()
    number, kind = w.phone_at(p, day)
    flags["uses_household_phone"] = kind == "household"

    if source == "pos":
        name: str | None = _name(w, p, "pos")
        phone: str | None
        if rng.random() < cfg.p_pos_no_phone:
            number, flags["uses_household_phone"] = None, False
            if rng.random() < cfg.p_pos_store_phone:
                number = w.store_phones[int(rng.integers(0, cfg.n_stores))]
                flags["uses_store_phone"] = True
        phone = _phone_text(w, number)
        if rng.random() < cfg.p_pos_placeholder:
            phone = PLACEHOLDER_NUMBERS[int(rng.integers(0, len(PLACEHOLDER_NUMBERS)))]
            flags["uses_store_phone"] = False
        if rng.random() < cfg.p_pos_no_name:
            name = None
        elif rng.random() < 0.10:
            name = p.given  # cashier typed only a first name
        row = {
            "txn_id": f"POS{seq:09d}",
            "store_id": f"S{int(rng.integers(1, cfg.n_stores + 1)):02d}",
            "customer_name": name,
            "customer_phone": phone,
            "txn_date": event_date,
        }
        return row, flags

    if source == "ecom":
        email = _email_noise(rng, p.emails[int(rng.integers(0, len(p.emails)))])
        row = {
            "account_id": f"EC{seq:09d}",
            "full_name": _name(w, p, "ecom"),
            "email": email,
            "phone": _phone_text(w, number) if rng.random() < 0.55 else None,
            "created_date": event_date,
        }
        return row, flags

    if source == "crm":
        row = {
            "contact_id": f"CR{seq:09d}",
            "full_name": _name(w, p, "crm"),
            "email": _email_noise(rng, p.emails[0]) if p.emails and rng.random() < 0.6 else None,
            "phone": _phone_text(w, number) if rng.random() < 0.92 else None,
            "dob": format_dob(rng, p.dob, cfg.p_dob_swap) if rng.random() < 0.90 else None,
            "updated_date": event_date,
        }
        return row, flags

    row = {  # loyalty
        "member_no": f"LY{seq:09d}",
        "full_name": _name(w, p, "loyalty"),
        "phone": _phone_text(w, number) if rng.random() < 0.97 else None,
        "dob": format_dob(rng, p.dob, cfg.p_dob_swap) if rng.random() < 0.85 else None,
        "enrolled_date": event_date,
    }
    return row, flags


@dataclass
class GeneratedData:
    sources: dict[str, pd.DataFrame]  # per source, includes an ``ingest_day`` column
    truth_persons: pd.DataFrame
    truth_records: pd.DataFrame
    config: WorldConfig


def _ingest_day(rng: np.random.Generator, cfg: WorldConfig, event_day: int) -> int:
    if event_day <= 0:
        # a small share of old events is delivered late, in a later batch
        if cfg.days > 0 and rng.random() < 0.004:
            return int(rng.integers(1, cfg.days + 1))
        return 0
    if rng.random() < cfg.late_arrival_share:
        return min(cfg.days, event_day + int(rng.integers(1, 4)))
    return event_day


def generate(cfg: WorldConfig | None = None) -> GeneratedData:
    cfg = cfg or WorldConfig()
    w = World(cfg)
    rng = w.rng
    rows: dict[str, list[dict]] = {s: [] for s in ("pos", "ecom", "crm", "loyalty")}
    truth_rows: list[dict] = []
    seq = {s: 0 for s in rows}
    id_field = {"pos": "txn_id", "ecom": "account_id", "crm": "contact_id", "loyalty": "member_no"}

    def emit(p: Person, source: str, day: int) -> None:
        seq[source] += 1
        row, flags = build_record(w, p, source, day, seq[source])
        ingest_day = _ingest_day(rng, cfg, day)
        row["ingest_day"] = ingest_day
        row["ingested_at"] = _ingested_at(ingest_day)
        rows[source].append(row)
        truth_rows.append(
            {
                "record_key": f"{source}:{row[id_field[source]]}",
                "source": source,
                "person_id": p.pid,
                "event_day": day,
                "ingest_day": ingest_day,
                **flags,
            }
        )

    # --- initial history + each person's own events
    for p in w.persons:
        lo = max(p.first_day, -cfg.backlog_days)
        hi = cfg.days
        if lo > hi:
            continue

        def day_in_range(lo: int = lo, hi: int = hi) -> int:
            return int(rng.integers(lo, hi + 1))

        n_pos = int(rng.poisson(2.5))
        for _ in range(n_pos):
            emit(p, "pos", day_in_range())
        if p.emails and rng.random() < 0.55:
            for _ in range(2 if rng.random() < 0.10 else 1):
                emit(p, "ecom", day_in_range())
        if rng.random() < 0.45:
            for _ in range(2 if rng.random() < 0.08 else 1):
                emit(p, "crm", day_in_range())
        if rng.random() < 0.35:
            emit(p, "loyalty", day_in_range())
        if not any(t["person_id"] == p.pid for t in truth_rows[-12:]):
            emit(p, "pos", day_in_range())  # every person appears somewhere

    # --- extra activity on each incremental day from existing customers
    existing = [p for p in w.persons if p.first_day <= 0]
    n_events = max(1, round(cfg.daily_event_rate * cfg.n_persons))
    for d in range(1, cfg.days + 1):
        for i in rng.choice(len(existing), size=n_events, replace=False):
            p = existing[int(i)]
            r = rng.random()
            source = (
                "pos" if r < 0.6 else ("loyalty" if r < 0.8 else ("ecom" if p.emails else "pos"))
            )
            emit(p, source, d)

    sources = {s: pd.DataFrame(r) for s, r in rows.items()}

    persons = pd.DataFrame(
        [
            {
                "person_id": p.pid,
                "full_name": p.full_name,
                "name_key": name_key(p.full_name),
                "sex": p.sex,
                "dob": p.dob,
                "household_id": p.household,
                "in_household": "household" in p.flags,
                "phone_recycled": "recycled" in p.flags,
                "phone_changed": "phone_changed" in p.flags,
                "first_day": p.first_day,
            }
            for p in w.persons
        ]
    )
    sizes = persons.groupby("name_key")["person_id"].transform("count")
    persons["common_name"] = sizes >= 3  # three or more unrelated people share this exact name
    return GeneratedData(sources, persons, pd.DataFrame(truth_rows), cfg)


_RECORD_KEY_RE = re.compile(r"^(pos|ecom|crm|loyalty):[A-Z]+\d+$")


def is_valid_record_key(key: str) -> bool:
    return bool(_RECORD_KEY_RE.match(key))
