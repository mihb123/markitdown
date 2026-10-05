"""
Field shape and value normalization for bank-transfer extraction (CLI: ``--bank``).

Both extraction backends — the cloud VLM (:mod:`._vlm_client`) and the offline
OCR parser (:mod:`._bank_text_parser`) — produce loosely formatted values, so
this module owns the single definition of the output contract: the six field
names, their canonical formatting, and the JSON rendering. Every field is a
string, and a field that could not be recognized is an empty string.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from typing import Dict, Mapping, Optional, Tuple

# The output contract: order is preserved in the emitted JSON.
BANK_FIELDS = (
    "amount",
    "account_number",
    "account_name",
    "bank",
    "content",
    "transaction_time",
)

# Canonical bank name -> the spellings that may appear on a receipt (accent-free
# and lowercase). Aliases of four characters or less are matched on word
# boundaries so that short codes like "mb" or "acb" do not fire inside a longer
# word.
BANK_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Vietcombank", ("vietcombank", "vcb", "ngoai thuong")),
    ("VietinBank", ("vietinbank", "viettinbank", "cong thuong", "ctg")),
    ("BIDV", ("bidv", "dau tu va phat trien")),
    ("Agribank", ("agribank", "nong nghiep va phat trien nong thon")),
    ("Techcombank", ("techcombank", "tcb", "ky thuong")),
    ("MB Bank", ("mbbank", "mb bank", "quan doi", "mb")),
    ("ACB", ("acb", "a chau")),
    ("VPBank", ("vpbank", "viet nam thinh vuong")),
    ("TPBank", ("tpbank", "tien phong")),
    ("Sacombank", ("sacombank", "sai gon thuong tin", "stb")),
    ("VIB", ("vib", "quoc te")),
    ("SHB", ("shb", "sai gon ha noi")),
    ("HDBank", ("hdbank", "hd bank")),
    ("MSB", ("msb", "hang hai")),
    ("OCB", ("ocb", "phuong dong")),
    ("SeABank", ("seabank", "seaba")),
    ("Eximbank", ("eximbank", "xuat nhap khau")),
    ("LPBank", ("lpbank", "lienvietpostbank", "loc phat")),
    ("SCB", ("scb",)),
    ("Nam A Bank", ("nam a bank", "namabank")),
    ("BVBank", ("bvbank", "ban viet")),
    ("PVcomBank", ("pvcombank", "pvcom")),
    ("ABBANK", ("abbank", "an binh")),
    ("Bac A Bank", ("bac a bank", "bacabank")),
    ("VietABank", ("vietabank", "viet a")),
    ("NCB", ("ncb", "quoc dan")),
    ("KienlongBank", ("kienlongbank", "kien long")),
    ("Cake", ("cake by vpbank", "cake")),
    ("Timo", ("timo",)),
    ("MoMo", ("momo", "vi momo")),
    ("ZaloPay", ("zalopay",)),
    ("Viettel Money", ("viettel money", "viettelpay")),
)

_SHORT_ALIAS_LENGTH = 4

# A name the app cut off to fit its column ("VIETCOMBAN... 0251 0027 26351").
_TRUNCATED_PATTERN = re.compile(r"([a-z0-9 ]+?)\s*\.\.\.")
_MINIMUM_TRUNCATED_LENGTH = 6


# Strip Vietnamese diacritics so OCR output and label tables can be compared on
# a stable ASCII form. `đ` has no combining decomposition, so it is mapped first.
def fold_ascii(text: str) -> str:
    replaced = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", replaced)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


# Return the all-empty result used when nothing could be recognized.
def empty_info() -> Dict[str, str]:
    return {field: "" for field in BANK_FIELDS}


# Reduce an amount to plain digits: separators, currency markers and a trailing
# decimal part ("1.234.567,00") are dropped so "89,000 VND" becomes "89000".
def normalize_amount(value: str) -> str:
    match = re.search(r"\d[\d.,\s]*", fold_ascii(value))
    if not match:
        return ""

    digits = match.group(0).strip()
    digits = re.sub(r"[.,](\d{1,2})$", "", digits)
    digits = re.sub(r"\D", "", digits)
    return digits.lstrip("0") or digits


# Keep only the characters an account number can contain. A few banks issue
# alphanumeric numbers, so letters are preserved and upper-cased.
def normalize_account_number(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]", "", fold_ascii(value))
    return cleaned.upper() if cleaned else ""


# Collapse whitespace and drop the punctuation OCR leaves around a name.
def normalize_account_name(value: str) -> str:
    cleaned = " ".join(value.split())
    return cleaned.strip(" .,:;-|*").strip()


# Map any recognizable spelling onto its canonical bank name; text that matches
# no known bank is returned cleaned up rather than discarded.
def normalize_bank(value: str) -> str:
    canonical = match_bank(value)
    if canonical:
        return canonical
    return " ".join(value.split()).strip(" .,:;-|*").strip()


# Return the canonical bank name mentioned in `text`, or None.
def match_bank(text: str) -> Optional[str]:
    haystack = " ".join(fold_ascii(text).lower().split())
    if not haystack:
        return None

    for canonical, aliases in BANK_ALIASES:
        for alias in aliases:
            if len(alias) <= _SHORT_ALIAS_LENGTH:
                if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", haystack):
                    return canonical
            elif alias in haystack:
                return canonical

    return _match_truncated_bank(haystack)


# Resolve a bank name the app cut short, by treating the text before an ellipsis
# as a prefix of an alias. The prefix must be long enough to name one bank only.
def _match_truncated_bank(haystack: str) -> Optional[str]:
    for match in _TRUNCATED_PATTERN.finditer(haystack):
        prefix = match.group(1).strip().split(" ")[-1]
        if len(prefix) < _MINIMUM_TRUNCATED_LENGTH:
            continue
        for canonical, aliases in BANK_ALIASES:
            if any(alias.startswith(prefix) for alias in aliases):
                return canonical
    return None


# Collapse whitespace in the free-text transfer message.
def normalize_content(value: str) -> str:
    cleaned = " ".join(value.split())
    return cleaned.strip(" .,:;-|*").strip()


def normalize_transaction_time(value: str) -> str:
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?", value
    ):
        return ""
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return ""
    return value


_NORMALIZERS = {
    "amount": normalize_amount,
    "account_number": normalize_account_number,
    "account_name": normalize_account_name,
    "bank": normalize_bank,
    "content": normalize_content,
    "transaction_time": normalize_transaction_time,
}


# Apply the per-field normalizer to a raw mapping, filling every missing or
# unusable value with an empty string.
def normalize_info(raw: Mapping[str, object]) -> Dict[str, str]:
    info = empty_info()
    for field in BANK_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str):
            value = "" if value is None else str(value)
        value = value.strip()
        if not value:
            continue
        info[field] = _NORMALIZERS[field](value)
    return info


# Render the result as the JSON document the CLI prints.
def to_json(info: Mapping[str, str]) -> str:
    ordered = {field: info.get(field, "") for field in BANK_FIELDS}
    return json.dumps(ordered, ensure_ascii=False, indent=2)
