from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cip.normalize import (
    dob_compatible,
    name_key,
    name_tokens,
    normalize_dob,
    normalize_email,
    normalize_phone,
    strip_diacritics,
    token,
)

# ----------------------------------------------------------------------------- phones


@pytest.mark.parametrize(
    "raw",
    [
        "0903854127",
        "+84903854127",
        "+84 903 854 127",
        "84903854127",
        "0084903854127",
        "090 385 4127",
        "090.385.4127",
        "0903-854-127",
        "  0903854127  ",
        "903854127",  # spreadsheet dropped the leading zero
    ],
)
def test_phone_formats_normalise_to_the_same_e164(raw):
    result = normalize_phone(raw)
    assert result.valid, (raw, result)
    assert result.e164 == "+84903854127"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "missing"),
        ("", "missing"),
        ("   ", "missing"),
        ("12345", "bad_length"),
        ("0123456789", "placeholder"),
        ("0900000000", "placeholder"),
        ("0911111111", "placeholder"),
        ("0987654321", "placeholder"),
        ("0412345678", "placeholder"),  # ascending run
        ("0401234559", "unknown_prefix"),  # 040 is not a mobile prefix
    ],
)
def test_untrustworthy_phones_become_missing_not_evidence(raw, reason):
    result = normalize_phone(raw)
    assert not result.valid
    assert result.e164 is None
    assert result.reason == reason


@given(st.text(max_size=30))
def test_phone_normalisation_never_raises_and_output_is_wellformed(raw):
    result = normalize_phone(raw)
    if result.valid:
        assert result.e164 is not None
        assert result.e164.startswith("+84")
        assert len(result.e164) == 12
    else:
        assert result.e164 is None


@given(st.integers(min_value=0, max_value=99_999_999))
def test_phone_normalisation_is_idempotent(n):
    first = normalize_phone(f"090{n:08d}"[:10])
    if first.valid:
        assert normalize_phone(first.e164) == first


# ----------------------------------------------------------------------------- email


def test_email_folds_case_plus_tags_and_gmail_dots():
    assert normalize_email(" John.Doe+promo@GMail.com ") == "johndoe@gmail.com"
    assert normalize_email("a.b@yahoo.com") == "a.b@yahoo.com"  # dots only fold for gmail
    assert normalize_email("x@googlemail.com") == "x@gmail.com"


@pytest.mark.parametrize("raw", [None, "", "not-an-email", "a@b", "@x.com", "+tag@x.com"])
def test_invalid_emails_are_none(raw):
    assert normalize_email(raw) is None


# ----------------------------------------------------------------------------- names


def test_strip_diacritics_handles_the_stroked_d():
    assert strip_diacritics("Đặng Thị Hồng Nhung") == "Dang Thi Hong Nhung"
    assert strip_diacritics("Nguyễn Văn Ánh") == "Nguyen Van Anh"


def test_name_key_is_order_and_diacritic_insensitive():
    a = name_key("Nguyễn Văn An")
    assert a == name_key("NGUYEN VAN AN")
    assert a == name_key("An Nguyen Van")  # given-name-first ordering
    assert a == name_key("  nguyen   van an ")


def test_single_token_name_is_not_a_key():
    assert name_key("An") is None
    assert name_key(None) is None
    assert name_key("") is None


def test_name_tokens_drop_digits_and_punctuation():
    assert name_tokens("Trần Thị B. (VIP) 123") == ["tran", "thi", "b", "vip"]


@given(st.text(max_size=40))
def test_name_key_is_idempotent_on_its_own_output(raw):
    key = name_key(raw)
    if key is not None:
        assert name_key(key) == key


# ----------------------------------------------------------------------------- dob


def test_dob_formats_and_plausibility():
    today = date(2026, 11, 17)
    assert normalize_dob("1990-05-17", today) == "1990-05-17"
    assert normalize_dob("17/05/1990", today) == "1990-05-17"
    assert normalize_dob("1850-01-01", today) is None
    assert normalize_dob("2030-01-01", today) is None
    assert normalize_dob("31/02/1990", today) is None  # not a real date
    assert normalize_dob("garbage", today) is None
    assert normalize_dob(None, today) is None


def test_dob_compatibility_allows_unknown_and_day_month_swap():
    assert dob_compatible(None, "1990-05-17")
    assert dob_compatible("1990-05-17", "1990-05-17")
    assert dob_compatible("1990-05-07", "1990-07-05")  # swapped day and month
    assert not dob_compatible("1990-05-17", "1991-05-17")
    assert not dob_compatible("1990-05-17", "1990-06-17")


# ----------------------------------------------------------------------------- tokens


def test_token_is_keyed_deterministic_and_truncated():
    t1 = token("+84903854127", "key-a")
    assert t1 == token("+84903854127", "key-a")
    assert t1 != token("+84903854127", "key-b")  # different key, different token
    assert t1 != token("+84903854128", "key-a")
    assert len(t1) == 32
    assert "84903854127" not in t1
    assert token(None, "key-a") is None
