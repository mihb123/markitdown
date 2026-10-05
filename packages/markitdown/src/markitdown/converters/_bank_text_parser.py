"""
Recover bank-transfer fields from the raw text of a receipt screenshot.

Used as the offline backend of ``--bank`` (see :mod:`._bank_transfer`), on the
line-per-row text produced by :mod:`._local_ocr`.

Receipts come in two shapes and both are handled here:

* labelled rows — ``Tài khoản nhận   0251002726351`` — matched by label;
* label-less cards — name, bank, account number and message stacked in a box —
  matched positionally, anchored on the line naming the bank.

Local OCR mangles Vietnamese diacritics ("Tên người nhận" is read as "Tên nguòi
nhân"), so labels are never compared literally. Each candidate label is reduced
to its consonant skeleton (accents folded, vowels removed), which survives that
damage while staying specific enough to tell "ngân hàng nhận" from "ngân hàng
chuyển".

Only the beneficiary side is extracted: labels naming the sender, the fee or
the transaction id are recognized purely so their values are never mistaken for
a beneficiary value.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from ._bank_fields import fold_ascii, match_bank
from ._bank_form_parser import parse_bank_form
from .. import _env

# Beneficiary labels, per output field.
FIELD_LABELS: Dict[str, Tuple[str, ...]] = {
    "amount": ("so tien", "so tien chuyen", "tong so tien", "so tien giao dich"),
    "account_number": (
        "tai khoan nhan",
        "so tai khoan",
        "tai khoan huong",
        "tai khoan thu huong",
        "so tai khoan thu huong",
        "tai khoan nguoi nhan",
        "so tai khoan nguoi nhan",
        "den tai khoan",
        "tai khoan",
        "stk",
    ),
    "account_name": (
        "ten nguoi nhan",
        "nguoi nhan",
        "ten nguoi huong",
        "nguoi thu huong",
        "ten nguoi thu huong",
        "ten tai khoan",
        "chu tai khoan",
        "ten thu huong",
    ),
    "bank": (
        "ngan hang nhan",
        "ngan hang huong",
        "ngan hang thu huong",
        "den ngan hang",
        "ngan hang",
    ),
    "content": (
        "noi dung chuyen khoan",
        "noi dung chuyen tien",
        "noi dung giao dich",
        "noi dung",
        # ABBank labels the transfer message "Cùng nội dung", so the label does
        # not start with the word every other app starts it with.
        "cung noi dung",
        "loi nhan",
        "dien giai",
        "ghi chu",
        "mo ta",
    ),
}

TRANSACTION_TIME_LABELS: Tuple[str, ...] = (
    "ngay giao dich",
    "ngay thuc hien",
    "thoi gian giao dich",
    "thoi gian thuc hien",
    "thoi gian",
    "thoi gian chuyen",
    "ngay chuyen",
    "chuyen luc",
    "ngay gio giao dich",
    "transaction date",
    "transaction time",
    "transaction date and time",
)

# Labels whose value belongs to the sender or to transaction metadata. Matching
# them is what keeps the fee, the transaction id or the source account out of
# the result.
IGNORED_LABELS: Tuple[str, ...] = (
    "tai khoan nguon",
    "tu tai khoan",
    "tai khoan trich no",
    "so tai khoan chuyen",
    "ten nguoi chuyen",
    "nguoi chuyen",
    "nguoi gui",
    # The amount credited to the beneficiary, not a name: it shares its opening
    # words with the "người thụ hưởng" name label and must outrank it.
    "nguoi thu huong nhan",
    "ngan hang chuyen",
    "phi chuyen tien",
    "phi giao dich",
    "ma giao dich",
    "ma tham chieu",
    "so tham chieu",
    "hinh thuc chuyen",
    "hinh thuc",
    "phuong thuc",
    "cach thuc",
    "danh muc",
    "loai giao dich",
    "ma tra soat",
    *TRANSACTION_TIME_LABELS,
    "trang thai",
    "transaction id",
    "reference number",
)

# The subset of the above that names a number. A receipt sometimes prints the
# value first and its label underneath, so these are the only labels allowed to
# claim a bare digit row above them.
NUMERIC_METADATA_LABELS: Tuple[str, ...] = (
    "tai khoan nguon",
    "tu tai khoan",
    "tai khoan trich no",
    "so tai khoan chuyen",
    "phi chuyen tien",
    "phi giao dich",
    "ma giao dich",
    "ma tham chieu",
    "so tham chieu",
    "ma tra soat",
    "nguoi thu huong nhan",
    "transaction id",
    "reference number",
)


# Closing lines of a receipt: never part of the transfer message.
BOILERPLATE_LABELS: Tuple[str, ...] = (
    "cam on",
    "vui long",
    "xac nhan boi",
    "luu mau",
    "luu anh",
    "chia se",
    "thuc hien giao dich",
    "giao dich thanh cong",
    "chuyen tien thanh cong",
    "tao giao dich moi",
    "ve trang chu",
    "quay lai",
)

# Values printed in their own row on most receipts. A wrapped field never
# continues onto one of these, so they end a multi-line value.
VALUE_STOPWORDS: Tuple[str, ...] = (
    "mien phi",
    "thanh cong",
    "da nhan",
    "dang xu ly",
    "that bai",
)

# Skeletons shorter than this are too ambiguous to match on.
_MINIMUM_SKELETON = 3

# How many characters of a label skeleton may be missing from its left edge
# before a partially cropped label ("i dung" for "nội dung") stops matching, and
# how many stray ones may trail it — a unit suffix ("nhận(đ)") or OCR noise.
_MAXIMUM_CROP = 2
_MAXIMUM_TRAILING_NOISE = 2
_MINIMUM_TRAILING_TARGET = 6

# Below this length a boilerplate skeleton is only matched at the start of a
# line, where a looser search would fire on unrelated text.
_MINIMUM_CONTAINED_SKELETON = 5

# A label may span at most this many words.
_MAXIMUM_LABEL_WORDS = 5

# Fields whose value can wrap onto the following line(s).
_CONTINUABLE_FIELDS = ("content", "account_name")
_MAXIMUM_CONTINUATION_LINES = 2

# The tail of a wrapped message is short; a row of its own or a promo banner is
# not. Applied only when extending a value that is already filled in.
_MAXIMUM_CONTINUATION_WORDS = 6

# A calendar date, which marks a metadata row rather than the rest of a transfer
# message. A bare clock reading is not enough: a message often ends in one
# ("...CHUYEN KHOAN-040826-09:52:50 toMS846752").
_DATE_PATTERN = re.compile(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}")

_NUMBER = r"\d{1,3}(?:[.,\s]\d{3})+|\d{4,}"

# The amount and its currency, in either order: most apps print "1,500,000 VND"
# but some put the unit first ("VND 14,642,000").
_AMOUNT_PATTERN = re.compile(
    rf"(?:({_NUMBER})\s*(?:vnd|d)\b|\b(?:vnd|d)\s*({_NUMBER}))", re.IGNORECASE
)
_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9 .'\-]{3,}$")

_MINIMUM_ACCOUNT_DIGITS = 6
_MAXIMUM_ACCOUNT_DIGITS = 19

# Evidence a bank name needs before it counts as the beneficiary's: its own
# labelled row, an account number beside it, a recipient name above it, or the
# wording of a bank row.
_MINIMUM_BANK_SCORE = 2

# Wording that marks a row naming a bank rather than an app's own branding, and
# so survives a label that OCR damaged past recognition.
_BANK_ROW_PATTERN = re.compile(r"ngan hang|tmcp")

# An account number is one unbroken digit run, which is what separates it from
# the groups a date ("22/07/2026") or an amount ("1,188,000") breaks into. The
# lookarounds anchor the run to a whole one, so an over-long reference number
# yields no match at all rather than its first 19 digits.
_ACCOUNT_RUN_PATTERN = re.compile(
    rf"(?<!\d)\d{{{_MINIMUM_ACCOUNT_DIGITS},{_MAXIMUM_ACCOUNT_DIGITS}}}(?!\d)"
)

# The same number printed in space-separated groups ("0251 0027 2635 1"). Only a
# single space joins groups: a date row ("2026-07-15 13:38:20") would otherwise
# read as one long number. Joined groups must still clear a higher digit floor,
# which is what keeps the "2026 05" of a timestamp out.
_GROUPED_ACCOUNT_PATTERN = re.compile(r"\d+(?: \d+)+")
_MINIMUM_GROUPED_DIGITS = 9

# A leading "Đến:" / "Tại:" style prefix, which modern apps put in front of the
# value instead of on a row of its own. The prefix must contain a letter so that
# a clock reading ("05:55") is not read as a labelled row.
_LEADING_LABEL_PATTERN = re.compile(r"^(?=[^\s:]*[A-Za-z])[^\s:]{1,20}\s*:\s*")

# Longest first word that may be dropped as a bare "Đến"-style prefix.
_MAXIMUM_NAME_PREFIX = 4


# Reduce text to its consonant skeleton, the form label matching compares on.
def _skeleton(text: str) -> str:
    letters = re.sub(r"[^a-z]", "", fold_ascii(text).lower())
    return re.sub(r"[aeiouy]", "", letters)


# Reduce the first `count` words of a line to the skeleton label matching
# compares on, or None when that run cannot be a label at all.
#
# Every word of a label leaves a consonant behind, so a word that reduces to
# nothing ("ưu") is not one — except in first position, where the image edge may
# have cropped the label down to a bare vowel ("i dung" for "nội dung"). Without
# that rule a vowel-only word pads the word count for free, which is how the
# promo banner "Nhận ưu đãi đến 1.1 triệu U-Point..." came to match the
# two-word "người nhận" on the three letters of "Nhận" alone.
def _prefix_skeleton(words: List[str], count: int) -> Optional[str]:
    if any(not _skeleton(word) for word in words[1:count]):
        return None
    return _skeleton(" ".join(words[:count]))


# Every label the parser knows, paired with the field it fills (None = ignore).
_LABEL_CANDIDATES: Tuple[Tuple[Optional[str], str], ...] = tuple(
    [(None, label) for label in IGNORED_LABELS]
    + [(field, label) for field, labels in FIELD_LABELS.items() for label in labels]
)


# Report whether a line is receipt boilerplate rather than transfer data.
def _is_boilerplate(line: str) -> bool:
    skeleton = _skeleton(line)
    if not skeleton:
        return True

    for label in BOILERPLATE_LABELS:
        target = _skeleton(label)
        if skeleton.startswith(target):
            return True
        if len(target) >= _MINIMUM_CONTAINED_SKELETON and target in skeleton:
            return True
    return False


# Report whether a line is a standalone row value that no wrapped field can run
# into ("Miễn phí", "Thành công").
def _is_stopword_value(line: str) -> bool:
    skeleton = _skeleton(line)
    return any(skeleton == _skeleton(value) for value in VALUE_STOPWORDS)


# Report whether a line carries no information beyond digits and separators.
def _is_digit_line(line: str) -> bool:
    stripped = re.sub(r"[\s.\-]", "", fold_ascii(line))
    return bool(stripped) and stripped.isdigit()


# Score how well a word prefix matches a label. An exact skeleton match beats a
# left-cropped one, and a longer label beats a shorter one, so "ngân hàng nhận"
# wins over the "ngân hàng" that also matches the same row.
#
# A match must span the same number of words as the label. Stripping vowels
# makes short labels collide across word boundaries — "diễn giải" and "đăng"
# both reduce to "dng" — and a whole missing leading word means the line is a
# different row that merely ends like a label ("chuyển tiền" is not the tail of
# the "phí chuyển tiền" row). Only characters lost from the label's first word
# are tolerated, which is how a label cropped by the image edge ("i dung" for
# "nội dung") still matches.
def _label_score(prefix: str, label: str, prefix_words: int) -> int:
    target = _skeleton(label)
    if len(prefix) < _MINIMUM_SKELETON or len(target) < _MINIMUM_SKELETON:
        return 0
    if prefix_words != len(label.split()):
        return 0
    if prefix == target:
        return len(target) * 2
    if target.endswith(prefix) and len(target) - len(prefix) <= _MAXIMUM_CROP:
        return len(target) * 2 - 1
    # Only a long label may carry trailing noise. On a three-letter skeleton the
    # allowance is wider than the label itself, which let unrelated prose
    # ("Đóng Gửi" against "diễn giải") match.
    if (
        len(target) >= _MINIMUM_TRAILING_TARGET
        and prefix.startswith(target)
        and len(prefix) - len(target) <= _MAXIMUM_TRAILING_NOISE
    ):
        return len(target) * 2 - 1
    return 0


# Find the label a line starts with. Returns (field, score, words_consumed),
# where `field` is None for a label that must be ignored, or None overall when
# the line carries no recognizable label.
def _match_label(line: str) -> Optional[Tuple[Optional[str], int, int]]:
    words = fold_ascii(line).split()
    best: Optional[Tuple[Optional[str], int, int]] = None

    for count in range(1, min(len(words), _MAXIMUM_LABEL_WORDS) + 1):
        prefix = _prefix_skeleton(words, count)
        if prefix is None or len(prefix) < _MINIMUM_SKELETON:
            continue

        for field, label in _LABEL_CANDIDATES:
            score = _label_score(prefix, label, count)
            if score and (best is None or score > best[1]):
                best = (field, score, count)

    return best


# Report whether a line is a row whose value must never be read as beneficiary
# data (the fee, the transaction id, the sender).
def _has_ignored_label(line: str) -> bool:
    match = _match_label(line)
    return match is not None and match[0] is None


# Store a labelled value, keeping the match made against the most specific
# label when the same field is named more than once.
def _store(
    fields: Dict[str, str],
    scores: Dict[str, int],
    field: str,
    value: str,
    score: int,
) -> None:
    if not value:
        return
    if field in fields and score <= scores.get(field, 0):
        return
    fields[field] = value
    scores[field] = score


# Report whether a wrapped value may run onto this line.
def _continues_value(line: str) -> bool:
    if _is_boilerplate(line) or _is_stopword_value(line) or _is_digit_line(line):
        return False
    return match_bank(line) is None


# Report whether a line may be appended to a value that is already filled in.
#
# A labelled row printed empty ("Nội dung" alone) has to take its value from the
# line below, so that case stays permissive. Extending a value that is already
# there is the risky direction — it is what swallowed a promo banner and a
# "Chuyển nhanh 247: 16:28 12/08/2026" row — so it additionally demands the
# short, date-free tail that a genuinely wrapped message looks like.
def _extends_value(line: str) -> bool:
    if not _continues_value(line):
        return False
    if len(line.split()) > _MAXIMUM_CONTINUATION_WORDS:
        return False
    return not _DATE_PATTERN.search(line)


# Walk the labelled rows, carrying a wrapped value onto the following lines
# while they hold no label of their own.
def _parse_labelled_rows(lines: List[str]) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    scores: Dict[str, int] = {}
    open_field: Optional[str] = None
    continuation_count = 0

    for line in lines:
        match = _match_label(line)

        if match is None:
            if open_field is None:
                continue
            accepts = _continues_value if not fields.get(open_field) else _extends_value
            if continuation_count >= _MAXIMUM_CONTINUATION_LINES or not accepts(line):
                open_field = None
                continue
            # Extend the value in place: this is the rest of a wrapped row, not
            # a competing match, so it must not go through the label guard.
            fields[open_field] = " ".join(
                filter(None, (fields.get(open_field, ""), line.strip()))
            )
            continuation_count += 1
            continue

        field, score, consumed = match
        open_field = None
        continuation_count = 0
        if field is None:
            continue

        value = " ".join(line.split()[consumed:]).strip(" :|-")
        _store(fields, scores, field, value, score)
        if field in _CONTINUABLE_FIELDS:
            open_field = field

    return fields


# Locate the line naming the beneficiary bank. A row matched by label is the
# strongest signal; otherwise a bank name sitting next to the account number or
# the recipient name beats one printed in a logo or a footer.
def _find_bank_line(lines: List[str]) -> Tuple[Optional[int], Optional[str]]:
    best_index: Optional[int] = None
    best_bank: Optional[str] = None
    best_score = 0

    for index, line in enumerate(lines):
        bank = match_bank(line)
        if bank is None:
            continue

        match = _match_label(line)
        # "Hình thức chuyển: Chuyển tiền trong Vietcombank" names a bank on a
        # metadata row; anchoring the card layout there would read the
        # surrounding rows as beneficiary details.
        if match is not None and match[0] is None:
            continue

        if match is not None and match[0] == "bank":
            score = 4
        elif any(
            _account_digits(other) for other in lines[max(0, index - 1) : index + 3]
        ):
            score = 3
        elif index > 0 and _NAME_PATTERN.match(lines[index - 1].strip()):
            score = 2
        elif _BANK_ROW_PATTERN.search(fold_ascii(line).lower()):
            score = 2
        else:
            score = 1

        if score > best_score:
            best_index, best_bank, best_score = index, bank, score

    # A name with nothing to tie it to the transfer is the issuing app's own
    # branding ("VPBank" above the receipt) or a promo banner. Reporting it as
    # the beneficiary bank would be worse than reporting nothing.
    if best_score < _MINIMUM_BANK_SCORE:
        return None, None

    return best_index, best_bank


# Return the account number a line carries, or "". The number does not need a
# row of its own: apps print it beside the bank ("Vietcombank • 0251002726351")
# or behind an inline label ("Tài khoản: 0251002726351"). Amount rows are
# excluded because an unseparated amount ("1188000 VND") is a digit run too.
def _account_digits(line: str) -> str:
    folded = fold_ascii(line)
    # An amount row ("1188000 VND") and a timestamp row are digit runs too. OCR
    # drops the space in "26/07/2026 05:55:05" often enough that the run
    # "202605" would otherwise pass for an account number.
    if _AMOUNT_PATTERN.search(folded) or _DATE_PATTERN.search(folded):
        return ""

    match = _ACCOUNT_RUN_PATTERN.search(folded)
    if match:
        return match.group(0)

    for grouped in _GROUPED_ACCOUNT_PATTERN.finditer(folded):
        digits = grouped.group(0).replace(" ", "")
        if _MINIMUM_GROUPED_DIGITS <= len(digits) <= _MAXIMUM_ACCOUNT_DIGITS:
            return digits
    return ""


# Return the first account-number-looking line at or after `start`, wrapping to
# the top of the receipt, and skipping digit runs that a neighbouring label
# marks as transaction metadata.
def _find_account_line(lines: List[str], start: int) -> Tuple[Optional[int], str]:
    for index in list(range(start, len(lines))) + list(range(0, start)):
        digits = _account_digits(lines[index])
        if not digits:
            continue

        if _is_metadata_number(lines, index):
            continue
        return index, digits

    return None, ""


# Report whether the digits on `index` belong to a row a metadata label claims.
#
# A label on the line itself or above it labels those digits. A label below only
# does when it stands alone: some receipts print the value first and its label
# under it ("15559089917" / "Mã giao dịch"), but a metadata row that carries its
# own value ("Thời gian 03/08/2026") says nothing about the line above it.
def _is_metadata_number(lines: List[str], index: int) -> bool:
    for offset in (-1, 0):
        neighbour = index + offset
        if neighbour >= 0 and _has_ignored_label(lines[neighbour]):
            return True

    below = index + 1
    if below < len(lines):
        consumed = _numeric_metadata_words(lines[below])
        if consumed and len(lines[below].split()) <= consumed:
            return True
    return False


# Return how many words of `line` name a numeric metadata field, or 0. Only such
# a label can claim the digits printed above it: a bare "Mã giao dịch" under a
# number labels that number, whereas "Người chuyển" names a person and merely
# heads the block below it.
def _numeric_metadata_words(line: str) -> int:
    words = fold_ascii(line).split()
    for count in range(1, min(len(words), _MAXIMUM_LABEL_WORDS) + 1):
        prefix = _prefix_skeleton(words, count)
        if prefix is None:
            continue
        for label in NUMERIC_METADATA_LABELS:
            if _label_score(prefix, label, count):
                return count
    return 0


# Yield the line itself and the forms left after dropping a short leading word
# such as "Đến", "Tới" or "To", most specific first.
def _name_candidates(line: str) -> List[str]:
    candidates = [line]
    stripped = _LEADING_LABEL_PATTERN.sub("", line).strip()
    if stripped != line:
        candidates.append(stripped)

    head, _, tail = line.partition(" ")
    if tail and len(head) <= _MAXIMUM_NAME_PREFIX:
        candidates.append(tail.strip())
    return candidates


# Read the beneficiary name from the line above the bank, where label-less
# receipt cards print it.
def _find_card_name(lines: List[str], bank_index: int) -> str:
    for index in range(bank_index - 1, -1, -1):
        candidate = lines[index].strip()
        if _is_digit_line(candidate) or _is_boilerplate(candidate):
            continue
        if match_bank(candidate) or _match_label(candidate) is not None:
            continue
        # "Đến: TRAN TU UYEN" and "Tới TRAN TU UYEN" are name rows whose label is
        # too short to reduce to a matchable skeleton, so they arrive here with
        # the prefix attached.
        for text in _name_candidates(candidate):
            if _NAME_PATTERN.match(text):
                return text
    return ""


# Read the transfer message from the lines below the account number, joining the
# wrapped remainder of a long message.
def _find_card_content(lines: List[str], account_index: int) -> str:
    parts: List[str] = []
    for line in lines[account_index + 1 :]:
        candidate = line.strip()
        if not _continues_value(candidate) or _match_label(candidate) is not None:
            break
        parts.append(candidate)
        if len(parts) > _MAXIMUM_CONTINUATION_LINES:
            break
    return " ".join(parts)


# Read the headline amount, which is printed next to its currency rather than
# behind a label on most receipts.
def _find_amount(lines: List[str]) -> str:
    for line in lines:
        match = _AMOUNT_PATTERN.search(fold_ascii(line))
        if match:
            return match.group(1) or match.group(2)
    return ""


_TRANSACTION_TIME_MIN_CONFIDENCE = 0.85
_TRANSACTION_CLOCK = (
    r"(?<![0-9:])(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})"
    r"(?::(?P<second>[0-9]{2}))?(?![0-9])(?:\s*(?P<meridiem>am|pm|sa|ch)\b)?"
)
_TRANSACTION_CLOCK_PATTERN = re.compile(_TRANSACTION_CLOCK)
_BARE_CLOCK_PATTERN = re.compile(
    rf"(?:(?:luc|vao|at)\s*)?-?\s*{_TRANSACTION_CLOCK}[\s.]*"
)
_TRAILING_CLOCK_PATTERN = re.compile(rf"\b(?:luc|vao|at)\s+{_TRANSACTION_CLOCK}[\s.]*$")
_MONTH_NAMES = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_MONTH_NAME = rf"(?P<month_name>{'|'.join(_MONTH_NAMES)})[a-z]*\.?"

_TRANSACTION_DATE_PATTERNS: Tuple[Tuple["re.Pattern[str]", bool], ...] = tuple(
    (re.compile(pattern), swappable)
    for pattern, swappable in (
        (
            r"(?<![0-9])(?P<year>20[0-9]{2})(?P<sep>[/.-])"
            r"(?P<month>[0-9]{1,2})(?P=sep)(?P<day>[0-9]{1,2})(?![0-9])",
            False,
        ),
        (
            r"(?<![0-9])(?P<day>[0-9]{1,2})\s?(?P<sep>[/.-])\s?"
            r"(?P<month>[0-9]{1,2})\s?(?P=sep)\s?(?P<year>[0-9]{4})(?![0-9])",
            True,
        ),
        (
            r"(?<![0-9])(?P<day>[0-9]{1,2})/(?P<month>[0-9]{1,2})"
            r"/(?P<year>[0-9]{2})(?![0-9/])",
            True,
        ),
        (
            r"(?<![0-9])(?P<day>[0-9]{1,2})\s*(?:thg|thang|th)\s*"
            r"(?P<month>[0-9]{1,2})\s*(?:,|\bnam\b)?\s*(?P<year>[0-9]{4})(?![0-9])",
            False,
        ),
        (
            rf"\b{_MONTH_NAME}\s+(?P<day>[0-9]{{1,2}}),?\s+(?P<year>[0-9]{{4}})(?![0-9])",
            False,
        ),
        (
            rf"(?<![0-9])(?P<day>[0-9]{{1,2}})\s+{_MONTH_NAME},?\s+(?P<year>[0-9]{{4}})(?![0-9])",
            False,
        ),
    )
)

_Clock = Tuple[int, int, Optional[int]]
_Timestamp = Tuple[date, Optional[_Clock]]


def _normalize_time_text(text: str) -> str:
    folded = re.sub(r"[–—−]", "-", fold_ascii(text).lower())
    return re.sub(r"([/.-][0-9]{4})(?=[0-9]{1,2}:[0-9]{2})", r"\1 ", folded)


def _build_date(match: "re.Match[str]", swappable: bool) -> Optional[date]:
    year = int(match["year"])
    if year < 100:
        year += 2000
    if match.groupdict().get("month_name"):
        month = _MONTH_NAMES.index(match["month_name"]) + 1
    else:
        month = int(match["month"])
    day = int(match["day"])
    orders = [(month, day), (day, month)] if swappable else [(month, day)]
    latest = date.today() + timedelta(days=1)
    for month_value, day_value in orders:
        try:
            value = date(year, month_value, day_value)
        except ValueError:
            continue
        return value if 2000 <= year and value <= latest else None
    return None


def _find_dates(text: str) -> List[Tuple[int, int, date]]:
    found: List[Tuple[int, int, date]] = []
    for pattern, swappable in _TRANSACTION_DATE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(
                start < taken_end and taken_start < end
                for taken_start, taken_end, _ in found
            ):
                continue
            value = _build_date(match, swappable)
            if value is not None:
                found.append((start, end, value))
    return found


def _build_clock(match: "re.Match[str]") -> Optional[_Clock]:
    hour = int(match["hour"])
    minute = int(match["minute"])
    second = None if match["second"] is None else int(match["second"])
    meridiem = match["meridiem"]
    if meridiem:
        if hour < 1 or hour > 12:
            return None
        if meridiem in ("pm", "ch"):
            hour = hour % 12 + 12
        else:
            hour %= 12
    if hour > 23 or minute > 59 or (second is not None and second > 59):
        return None
    return hour, minute, second


def _merge_clocks(clocks: Sequence[_Clock]) -> Optional[_Clock]:
    minutes = {(hour, minute) for hour, minute, _ in clocks}
    seconds = {second for _, _, second in clocks if second is not None}
    if len(minutes) != 1 or len(seconds) > 1:
        return None
    hour, minute = minutes.pop()
    return hour, minute, seconds.pop() if seconds else None


def _read_timestamp(text: str) -> Optional[_Timestamp]:
    normalized = _normalize_time_text(text)
    dates = _find_dates(normalized)
    if len({value for _, _, value in dates}) != 1:
        return None
    clocks = [
        clock
        for match in _TRANSACTION_CLOCK_PATTERN.finditer(normalized)
        if not any(
            start < match.end() and match.start() < end for start, end, _ in dates
        )
        and (clock := _build_clock(match)) is not None
    ]
    return dates[0][2], _merge_clocks(clocks)


def _read_bare_clock(text: str) -> Optional[_Clock]:
    match = _BARE_CLOCK_PATTERN.fullmatch(_normalize_time_text(text).strip())
    return None if match is None else _build_clock(match)


def _read_trailing_clock(text: str) -> Optional[_Clock]:
    match = _TRAILING_CLOCK_PATTERN.search(_normalize_time_text(text).strip())
    return None if match is None else _build_clock(match)


def _transaction_time_min_confidence() -> float:
    try:
        value = float(
            _env.resolve("MARKITDOWN_BANK_TIME_MIN_CONFIDENCE")
            or _TRANSACTION_TIME_MIN_CONFIDENCE
        )
    except ValueError:
        return _TRANSACTION_TIME_MIN_CONFIDENCE
    return value if 0 <= value <= 1 else _TRANSACTION_TIME_MIN_CONFIDENCE


def _transaction_label_words(line: str) -> Optional[int]:
    words = re.sub(r"^[^0-9A-Za-z\u00C0-\u1EF9]+", "", line).split()
    for label in sorted(
        TRANSACTION_TIME_LABELS, key=lambda item: len(item.split()), reverse=True
    ):
        count = len(label.split())
        if len(words) >= count and _skeleton(" ".join(words[:count])) == _skeleton(
            label
        ):
            return count
    return None


def _choose_date(candidates: List[_Timestamp]) -> Optional[date]:
    dates = {value for value, _ in candidates}
    if len(dates) > 1:
        dates = {value for value, clock in candidates if clock is not None}
    return dates.pop() if len(dates) == 1 else None


def _format_timestamp(value: date, clock: Optional[_Clock]) -> str:
    if clock is None:
        return value.isoformat()
    hour, minute, second = clock
    return datetime(
        value.year, value.month, value.day, hour, minute, second or 0
    ).isoformat(timespec="minutes" if second is None else "seconds")


def _parse_transaction_time(
    lines: List[str],
    line_confidences: Optional[Sequence[float]],
    content: str = "",
) -> str:
    if line_confidences is None or len(line_confidences) != len(lines):
        return ""
    min_confidence = _transaction_time_min_confidence()
    content_text = _normalize_time_text(content)
    labelled: List[_Timestamp] = []
    unlabelled: List[_Timestamp] = []

    for index, line in enumerate(lines):
        consumed = _transaction_label_words(line)
        value_index = index
        if consumed is None:
            if content_text and _normalize_time_text(line).strip() in content_text:
                continue
            value = line
            bucket = unlabelled
        else:
            value = " ".join(line.split()[consumed:])
            if not _find_dates(_normalize_time_text(value)):
                value_index += 1
                if value_index >= len(lines):
                    continue
                value = lines[value_index]
            bucket = labelled

        if not line_confidences[value_index] >= min_confidence:
            continue
        timestamp = _read_timestamp(value)
        if timestamp is None:
            continue
        value_date, clock = timestamp
        next_index = value_index + 1
        if (
            clock is None
            and next_index < len(lines)
            and line_confidences[next_index] >= min_confidence
        ):
            clock = _read_bare_clock(lines[next_index])
        if (
            clock is None
            and value_index > 0
            and line_confidences[value_index - 1] >= min_confidence
        ):
            clock = _read_trailing_clock(lines[value_index - 1])
        bucket.append((value_date, clock))

    candidates = labelled or unlabelled
    chosen = _choose_date(candidates)
    if chosen is None:
        return ""
    for bucket in (labelled, unlabelled):
        clocks = [clock for value, clock in bucket if value == chosen and clock]
        if clocks:
            return _format_timestamp(chosen, _merge_clocks(clocks))
    return _format_timestamp(chosen, None)


# Extract every recognizable beneficiary field from OCR text. Values are raw:
# callers normalize them through :mod:`._bank_fields`.
def parse_bank_text(
    text: str, *, line_confidences: Optional[Sequence[float]] = None
) -> Dict[str, str]:
    raw_lines = text.splitlines()
    if line_confidences is not None:
        if len(line_confidences) == len(raw_lines):
            line_confidences = [
                score
                for line, score in zip(raw_lines, line_confidences)
                if line.strip()
            ]
        else:
            line_confidences = None
    lines = [line.strip() for line in raw_lines if line.strip()]
    if not lines:
        return {}

    # A payment order repeats every label for the payer and the beneficiary, so
    # the row walk below would read the payer's block. Its own parser knows
    # which half of the form to read.
    form_fields = parse_bank_form(lines)
    if form_fields:
        if not form_fields.get("amount"):
            form_fields["amount"] = _find_amount(lines)
        form_fields["transaction_time"] = _parse_transaction_time(
            lines, line_confidences, form_fields.get("content", "")
        )
        return {field: value for field, value in form_fields.items() if value}

    fields = _parse_labelled_rows(lines)

    if not fields.get("amount"):
        fields["amount"] = _find_amount(lines)

    bank_index, bank_name = _find_bank_line(lines)
    if not fields.get("bank") and bank_name:
        fields["bank"] = bank_name

    account_index: Optional[int] = None
    if not fields.get("account_number"):
        account_index, digits = _find_account_line(
            lines, 0 if bank_index is None else bank_index + 1
        )
        if digits:
            fields["account_number"] = digits

    # The name sits above the block naming the beneficiary. That block is headed
    # by the bank, except on the receipts that show the bank as a logo only, no
    # text — there the account number is the lowest row to anchor on.
    if not fields.get("account_name"):
        anchor = bank_index if bank_index is not None else account_index
        if anchor is not None:
            fields["account_name"] = _find_card_name(lines, anchor)

    # Only a card layout stacks the message under the account number. Without a
    # bank line there is no card to anchor on, and the rows below the number are
    # as likely to be a footer as a message.
    if (
        not fields.get("content")
        and account_index is not None
        and bank_index is not None
    ):
        fields["content"] = _find_card_content(lines, account_index)

    fields["transaction_time"] = _parse_transaction_time(
        lines, line_confidences, fields.get("content", "")
    )

    return {field: value for field, value in fields.items() if value}
