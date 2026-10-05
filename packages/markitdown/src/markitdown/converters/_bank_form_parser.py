"""
Recover bank-transfer fields from a bilingual payment order ("Ủy nhiệm chi").

Front end of the offline ``--bank`` parser (:mod:`._bank_text_parser`) for the
counter-transfer form banks print on paper, which shares no layout with the app
receipts that module was written for.

The form prints the payer's block first and the beneficiary's below it, both
behind the same labels ("Số tài khoản/ Account No."), so matching a label
wherever it appears reads the payer's account as the beneficiary's. Rows are
therefore only read between the "Đơn vị thụ hưởng/ Beneficiary" header and the
signature block underneath it.

Every label is printed twice — Vietnamese then English — with the value after
the English half, and several labels share a line. Matching is done on the
English half alone: it is ASCII, so OCR leaves it intact, whereas the
Vietnamese half loses the diacritics that forced :mod:`._bank_text_parser` into
skeleton matching. A value runs from its label to the next one on the line.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ._bank_fields import fold_ascii

# English labels, per output field, most specific first. A None field marks a
# label that only bounds a value: the row names the payer or form metadata.
_LABELS: Tuple[Tuple[Optional[str], str], ...] = (
    ("bank", r"benn?eficiary'?s?\s*bank"),
    # "Benneficiary" is the form's own misprint, not OCR damage.
    ("account_name", r"(?<!the )benn?eficiary"),
    ("account_number", r"account\s*no\.?"),
    ("amount", r"amount\s+in\s+figures"),
    ("content", r"details"),
    (None, r"account\s*name"),
    (None, r"amount\s+in\s+words"),
    # The payer's bank: "Tại Ngân hàng Á Châu_chi nhánh/ At ACB_Branch".
    (None, r"at\s+\S*[_\s]branch"),
    (None, r"province\s*/\s*city"),
    (None, r"date\s+of\s+issue"),
    (None, r"place\s+of\s+issue"),
    (None, r"id\s*/\s*pp"),
    (None, r"applicant"),
    (None, r"transfer\s+fee\s+to\s+be\s+paid\s+by"),
)

# Wording only this form carries. Three are required so that an app receipt
# mentioning a beneficiary in passing is not read as a form.
_FORM_MARKERS: Tuple[str, ...] = (
    r"benn?eficiary",
    r"applicant",
    r"amount\s+in\s+(?:figures|words)",
    r"account\s*no\.?",
    r"transfer\s+fee\s+to\s+be\s+paid\s+by",
    r"uy\s+nhiem\s+chi",
    r"payment\s+order",
)
_MINIMUM_MARKERS = 3

# The header opening the beneficiary block. "The beneficiary" is the fee row
# further down, which must not be mistaken for it.
_SECTION_START = re.compile(r"(?<!the )benn?eficiary", re.IGNORECASE)

# The first row of the signature block that closes the beneficiary details.
_SECTION_END = re.compile(
    r"transfer\s+fee|the\s+applicant|post\s+date|chief\s+accountant|signature",
    re.IGNORECASE,
)

_ACCOUNT_RUN_PATTERN = re.compile(r"(?<!\d)\d{6,19}(?!\d)")


# Fold one character to ASCII, keeping it as-is when folding would change its
# width. Applied per character, this keeps offsets in the folded line pointing
# at the same characters of the original.
def _fold_char(char: str) -> str:
    folded = fold_ascii(char)
    return folded if len(folded) == 1 else char


def _fold_aligned(text: str) -> str:
    return "".join(_fold_char(char) for char in text)


# Report whether the text is a payment order rather than an app receipt.
def _is_form(lines: List[str]) -> bool:
    folded = _fold_aligned(" ".join(lines))
    found = sum(
        1 for marker in _FORM_MARKERS if re.search(marker, folded, re.IGNORECASE)
    )
    return found >= _MINIMUM_MARKERS


# Return the half-open range of lines holding the beneficiary details.
def _section_bounds(lines: List[str]) -> Optional[Tuple[int, int]]:
    start: Optional[int] = None
    for index, line in enumerate(lines):
        if _SECTION_START.search(_fold_aligned(line)):
            start = index
            break

    if start is None:
        return None

    for index in range(start + 1, len(lines)):
        if _SECTION_END.search(_fold_aligned(lines[index])):
            return start, index
    return start, len(lines)


# Locate every label on a line, keeping the outermost match where two overlap:
# "Beneficiary's Bank" names the bank, not a beneficiary called "'s Bank".
def _find_labels(line: str) -> List[Tuple[int, int, Optional[str]]]:
    folded = _fold_aligned(line)
    matches = [
        (match.start(), match.end(), field)
        for field, pattern in _LABELS
        for match in re.finditer(pattern, folded, re.IGNORECASE)
    ]
    matches.sort(key=lambda item: (item[0], -item[1]))

    kept: List[Tuple[int, int, Optional[str]]] = []
    for start, end, field in matches:
        if kept and start < kept[-1][1]:
            continue
        kept.append((start, end, field))
    return kept


# Cut a value where the next label's Vietnamese half begins. That half is never
# matched — OCR mangles it — but it always ends in "/", so the value stops at
# the first slash and the partial word carrying it is dropped.
def _trim_value(value: str) -> str:
    text = value.strip()
    slash = text.find("/")
    if slash != -1:
        head = text[:slash]
        text = head.rsplit(" ", 1)[0] if " " in head else ""
    return " ".join(text.split()).strip(" :|-.")


# Reduce a value to the part of it the field can hold. Only the account number
# needs it: it is followed on the same row by text no slash separates.
def _clean_value(field: str, value: str) -> str:
    trimmed = _trim_value(value)
    if field != "account_number":
        return trimmed

    match = _ACCOUNT_RUN_PATTERN.search(trimmed)
    return match.group(0) if match else ""


# Extract the beneficiary fields of a payment order, or {} when the text is not
# one. Values are raw: callers normalize them through :mod:`._bank_fields`.
def parse_bank_form(lines: List[str]) -> Dict[str, str]:
    if not _is_form(lines):
        return {}

    bounds = _section_bounds(lines)
    if bounds is None:
        return {}

    fields: Dict[str, str] = {}
    for line in lines[bounds[0] : bounds[1]]:
        labels = _find_labels(line)
        for position, (_, end, field) in enumerate(labels):
            if field is None or field in fields:
                continue
            stop = labels[position + 1][0] if position + 1 < len(labels) else len(line)
            value = _clean_value(field, line[end:stop])
            if value:
                fields[field] = value

    return fields
