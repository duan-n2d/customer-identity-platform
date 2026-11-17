"""Vietnamese name and phone pools for the synthetic generator.

Frequencies roughly follow the real distribution (Nguyen alone is about 38% of the
population), which matters: it creates the *common-name collisions* that make identity
resolution hard. A uniform pool would make the problem unrealistically easy.
"""

from __future__ import annotations

# (family name, relative weight)
FAMILY_NAMES: list[tuple[str, float]] = [
    ("Nguyễn", 38.0),
    ("Trần", 11.0),
    ("Lê", 9.5),
    ("Phạm", 7.0),
    ("Hoàng", 5.0),
    ("Huỳnh", 5.0),
    ("Phan", 4.5),
    ("Vũ", 4.0),
    ("Võ", 3.5),
    ("Đặng", 2.5),
    ("Bùi", 2.0),
    ("Đỗ", 2.0),
    ("Hồ", 1.5),
    ("Ngô", 1.5),
    ("Dương", 1.0),
    ("Lý", 1.0),
]

MIDDLE_NAMES_M: list[str] = ["Văn", "Đức", "Minh", "Quốc", "Hữu", "Thanh", "Công", "Anh"]
MIDDLE_NAMES_F: list[str] = ["Thị", "Ngọc", "Thùy", "Khánh", "Mai", "Hồng", "Phương", "Kim"]

GIVEN_NAMES_M: list[str] = [
    "An", "Anh", "Bình", "Cường", "Dũng", "Hải", "Hiếu", "Hùng", "Huy", "Khoa",
    "Khánh", "Long", "Nam", "Phong", "Phúc", "Quân", "Sơn", "Thành", "Tuấn", "Việt",
]  # fmt: skip

GIVEN_NAMES_F: list[str] = [
    "An", "Anh", "Chi", "Dung", "Giang", "Hà", "Hạnh", "Hoa", "Hương", "Lan",
    "Linh", "Mai", "Ngân", "Nhung", "Phương", "Thảo", "Trang", "Trinh", "Uyên", "Vy",
]  # fmt: skip

# Real Vietnamese mobile prefixes (see cip.normalize). The generator only emits valid ones.
MOBILE_PREFIXES: list[str] = (
    ["090", "091", "093", "094", "096", "097", "098"]
    + [f"03{d}" for d in range(2, 10)]
    + ["070", "076", "077", "078", "079"]
    + [f"08{d}" for d in range(1, 10)]
)

EMAIL_DOMAINS: list[tuple[str, float]] = [
    ("gmail.com", 70.0),
    ("yahoo.com", 10.0),
    ("outlook.com", 10.0),
    ("hotmail.com", 5.0),
    ("icloud.com", 5.0),
]
