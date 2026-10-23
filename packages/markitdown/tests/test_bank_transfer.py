#!/usr/bin/env python3 -m pytest
import inspect
import io

from markitdown.converters._bank_fields import (
    empty_info,
    match_bank,
    normalize_amount,
    normalize_info,
    to_json,
)
from markitdown.converters._bank_text_parser import parse_bank_text

# These tests cover the offline `--bank` path on recorded OCR output, so they
# need neither the OCR model nor the cloud VLM. Each fixture is the verbatim
# text local OCR produced for a real receipt, mangled diacritics included.

LABELLED_RECEIPT = "\n".join(
    [
        "VCB Digibank",
        "Giao dich thành công!",
        "89,000 VND",
        "11:33 Thú Hai 10/08/2026",
        "Tài khon nhn 0251002726351",
        "Tên nguòi nhân TRAN TU UYEN",
        "Ngân hàng nhân Vietcombank",
        "Ngân hàng g TMCP Ngoąi Thuōng Vit Nam",
        "Nôi dung VU NGOC HOAN",
        "chuyen tien",
        "Phí chuyên tiên Min phí",
        "Hình thúc chuyên Chuyên tiên trong",
        "Vietcombank",
        "Mã giao dich 15507345760",
        "Luu mu chuyên tiên",
    ]
)

CARD_RECEIPT = "\n".join(
    [
        "Chuyên tiên thành công",
        "220,000 VND",
        "20:40 - 09/08/2026",
        "TRAN TU UYEN",
        "Vietcombank (VCB)",
        "0251002726351",
        "LE QUOC DAI chuyen tien",
        "Cm on bgn dã sù dung dich vu cúa MBBank",
        "MB",
        "8",
        "Chia sè Luu ành Luu mãu",
        "Thuc hin giao dich khác",
    ]
)

WRAPPED_CARD_RECEIPT = "\n".join(
    [
        "MB",
        "Chuyên tiên thành công",
        "300,000 VND",
        "21:40 - 10/08/2026",
        "TRAN TU UYEN",
        "Vietcombank (VCB)",
        "0251002726351",
        "Ice Cold Killer chuyen khoan nhanh qua",
        "Zalo",
        "Giao dich duąc xác nhn bδi MB.",
        "Vui lòng không chinh sūa hình ånh này.",
    ]
)

PHOTOGRAPHED_RECEIPT = "\n".join(
    [
        "40",
        "ibank",
        "Giao dich thành công!",
        "3,518,000 VND",
        "11:28 ThùNäm 13/08/2026",
        "0251002726351",
        "oàn nhn",
        "TRANTUUYEN",
        "nguòi nhan",
        "inhàng nhn Ngân hàng TMCP Ngoai T Vietcombank Thuong Viet Nam",
        "PHANXUAN BANG",
        "i dung chuyent tien",
        "Min phi",
        "ni chuyên tiên Chuyên tiên trong",
        "Hinh thúc chuyên Vietcombank",
        "15559089917",
        "Mã giao dich",
        "Luu mäu chuyên tiên",
        "Möi",
    ]
)

# The bank and the account number share one row, and the message label is the
# four-word "Nội dung chuyển tiền".
INLINE_BANK_ROW_RECEIPT = "\n".join(
    [
        "8:40",
        "MSB",
        "Chuyên tiên thành công",
        "1,188,000 VND",
        "08:40 – 22/07/2026",
        "Nguròi nhn",
        "TRAN TU UYEN",
        "Vietcombank . 0251002726351",
        "Nguòi chuyên",
        "NGUYEN TRUC PHUONG",
        "Nôi dung chuyên tin",
        "NGUYEN TRUC PHUONG chuyen tien",
        "Hình thúc",
        "Chuyên nhanh 24/7",
        "Chi tiêt giao dich",
        "C+ O",
        "Chia sé Luu nguòi nhn",
        "Giao dich khác",
    ]
)

# Values carry an inline "Đến:" / "Tài khoản:" prefix, and the reference number
# is a long digit run that must not be read as the account.
INLINE_PREFIX_RECEIPT = "\n".join(
    [
        "05:55",
        "Zalo",
        "BIDV",
        "Giao dich thành công",
        "100,000 vND",
        "26/07/202605:55:05",
        "Dên: TRAN TU UYEN",
        "Tài khoàn: 0251002726351",
        "Tai:NHTMCP Ngoai thuong Vit Nam (VCB)",
        "Luu danh ba",
        "Nôi dung",
        "Nguyen Ngoc Minh chuyen khoan nhanh qua",
        "Zalo",
        "Sö tham chiëu",
        "020097048807260555042026wylu688154",
        "%",
        "Chia sè Luu ành Giói thiu",
        "KH mói",
        "Vê trang chù Giao dich mói",
    ]
)

# A transaction-detail table: the sender's account, the transaction id and the
# credited amount are all rows that look like beneficiary data.
DETAIL_TABLE_RECEIPT = "\n".join(
    [
        "MobiFone 13:38 el 4G* 78",
        "Chi tiët giao dich",
        "Giao dich thành công",
        "-1,000,000d",
        "2026-07-15 13:38:20",
        "Mã giao dich 2513817335",
        "Mã tra soát 6196SGTTH2M67931",
        "Loai giao dich Chuyn tin tù TK SACOMBANK dn TK NH",
        "ni dia",
        "Sδ tài khoån chuyn 060102420317",
        "Nguòi nhn TRAN TU UYEN",
        "Sô tài khoån 0251002726351",
        "Ngân hàng Vietcombank",
        "Ngui thu huông nhân(ã) 1,000,000",
        "Din giài Hong Tran chuyen khoan nhanh qua Zalo",
        "Töng sô tin () 1,000,000",
        "Ti vê Chia sé",
    ]
)

# An English receipt that states the transfer as prose, with no beneficiary name,
# bank or message anywhere on it.
PROSE_RECEIPT = "\n".join(
    [
        "20:32 Zalo Q .ll LTE2 Vo) .ill 84%",
        "Transfer",
        "successful",
        "You have successfully transferred 1,500,000d",
        "to account number r 1038079000.",
        "Transaction Details",
        "Transaction ID 2483910714",
        "Back to Transactions",
        "Add another transaction",
        "O",
    ]
)

# A promo banner sits directly under the message row.
BANNER_RECEIPT = "\n".join(
    [
        "VietinBank",
        "Chuyên tiên thành công",
        "1,700,000 VND",
        "10/08/2026 10:20 • 520K2680F15KPUYX",
        "TRAN TU UYEN",
        "0251002726351 · Vietcombank_Ngân hàng",
        "Ngogi thuong Vit Nam (VCB)",
        "Nôi dung coc do amac",
        "TÓI UU RON MI NGAY MO KHOA TRAI NGHIEM UU TIÊN",
        "NÂNG TÂM V-PLUS 20X06212-r V-ADVANCE",
        "cCaC0DV4D/nam",
        "Gundie l Dugen Es ke- 13",
        "TRÁI NGHIÊM 18 Dóng Trieu 66 Döng Triçu",
        "DNG KY NGAY!",
    ]
)

# The account number is printed in space-separated groups, and the bank name is
# cut off to fit its column.
GROUPED_NUMBER_RECEIPT = "\n".join(
    [
        "GIAO DICH VÓI TPBANK",
        "NHÂN vô VÀN QUÀ TNG HÂP DÂN",
        "TINH H S∆Y HI'",
        "TPBank",
        "Chuyên thành công!",
        "400,000 VND",
        "TRAN TU UYEN",
        "VIETCOMBAN... 0251 0027 26351",
        "Lòi nhn: Tri chuyen khoan nhanh qua",
        "Zalo",
        "Chuyên nhanh 247: 09:59 13/08/2026",
        "Luu mãu Chia sè Gn thè Luu nguòi",
        "nhân",
        "Giao dich khác vê trang chù",
    ]
)

# Sender and beneficiary each get their own block, and the beneficiary bank is
# shown as a logo with no text — so `bank` has to come back empty rather than
# naming the app that issued the receipt.
TWO_BLOCK_RECEIPT = "\n".join(
    [
        "17:01 . 64",
        "← Chi Tiët Giao Dich",
        "Giao dich thành công",
        "-500,000 VND",
        "Tù tài khoån",
        "CAO VAN LOI",
        "1000 3514 330",
        "Ti tài khon",
        "TRAN TU UYEN",
        "0251 0027 2635 1",
        "Só du sau giao dich: ******** VND",
        "Thòi gian thuc hin: 16:58 13/08/2026",
        "Mã giao dich: 682V60026225BOVC -",
        "6225TPBVI2CFQQH8",
        "Danh muc: Gn thè",
        "Nôi dung chuyên tiên: Cao Van Loi chuyen tien",
        "quan ao da banh",
        "Cách thúc: Chuyên nhanh : 247",
        "C",
        "Thc hin lai Chia Sè Xác nhn GD Tra soát GD",
    ]
)

# The beneficiary is a company, and the bank is a logo only. OCR read the leading
# "1" of "1 000 000 đ" as "(cid", so the amount is not recoverable from this text
# — the true amount is 1000000.
COMPANY_RECEIPT = "\n".join(
    [
        "14:33 .l 4G 69",
        "VPBank Xong",
        "Thành công",
        "(i 000 000 d",
        "Thông tin nguði nhân",
        "CTY TNHH MTV SX TM VA DV AMAC",
        "1059814478",
        "Thòi gian 03/08/2026",
        "Nôi dung DO HONG VU chuyen tien coc",
        "Phuong thúc Chuyên nhanh Napas 247",
        "Mã tra soát 923261",
        "Chi tiët giao dich",
        "Nhn luşt mδ quà",
        "cùng Hoàn tin Thinh vuşng",
        "Ban có hài lòng vói trài nghiêm Chuyên tiên trên VP..",
        "Chia sè Giao dich mói Chia hóa don",
    ]
)

# The same VPBank layout as above, but OCR dropped the grey underlined "đ" after
# the headline amount, and a "Phí 0 đ" row follows the message.
VPBANK_NO_CURRENCY_RECEIPT = "\n".join(
    [
        "VPBank",
        "Thành công",
        "500 000",
        "Thông tin nguöi nhân",
        "CTY TNHH MTV SX TM VA DV AMAC",
        "1059814478",
        "Thòi gian 28/07/2026",
        "Nôi dung Coc tien may ao dong phuc",
        "Phí 0 d",
        "Mã giao dich FT26209688203391/28226783",
        "3148",
        "Mã tra soát 833148",
    ]
)

# A paper payment order ("Ủy nhiệm chi"): bilingual labels, several fields per
# row, and the payer's block printed above the beneficiary's behind the very
# same labels.
PAYMENT_ORDER_RECEIPT = "\n".join(
    [
        "nVA nENAPE",
        "Tên tài khon/ Account NameCONG TY CO PHAN RSVP",
        "Sδ tài khoån/ Account No. 28271688 Tai Ngân hàng Á Châu_chi nhánh/ "
        "At ACB_Branch ACB - CN HOA",
        "HUNG",
        "DON V! THU HUNG/ Beneficiary",
        "Don vi thy hung/ Benneficiary CONG TY TNHH MTV SAN XUAT THUONG MAI VA "
        "DICH VU AMAC",
        "Sδ tài khon/ Account No. 1059814478",
        "CMND/PP/ ID/ PP Ngày Cp/ Date of issue Noi Câp/ Place of issue",
        "Tai ngân hàng/ Beneficiary's Bank VCB CN HCM TInh/Tp/ Province/City "
        "Thanh pho Ho Chi Minh",
        "Sδ tin bång sō/ Amount in figures 10,342,080 VND Bàng chū/ Amount in "
        "words Muòi triu ba trm bôn muoi hai nghin",
        "không trm tám muoi dông",
        "Ni dung/ Details Thanh toan tien hang-Amac",
        "Phí chuyen tien do/ Transfer fee to be paid by Don vi trà tin chiu phi/ "
        "The applicant Don vi thu huðng chiu phi/ The beneficiary",
        "DON VI TRÀ TIÈN/ Applicant NGÂN HÀNG À CHÂU/ ACB NGÂN HÀNG B/ B Bank",
        "K toán truðng Chù tài khon Ghi sô ngày/ Post Date Ghi sô ngày/ Post Date",
        "Chief Accountant Account Holder Giao dich viên Kim soát viên Giao dich "
        "viên Truöng don vi",
        "Teller Supervisor Teller Manager",
        "CONG TY CO PHAN Signature Not Verified",
        "RSVP Ngân hàng TMCP Á Châu",
        "05/08/2026",
        "ACB",
        "QF-34/TG-01.15 Contact Center 24/7:1900 54 54 86-(028) 38 247 247 "
        "acb.com.vn",
    ]
)

NOT_A_RECEIPT = "\n".join(
    [
        "CLOUDFLARE",
        "mihb.site or one of its subdomains has been",
        "issued a new SSL/TLS certificate",
        "Log date: 2026-07-08 12:44:57 UTC",
        "Issuer: CN=YE2,O=Let's Encrypt,C=US",
    ]
)


# Agribank labels the beneficiary rows "... thụ hưởng", so both the name and the
# number labels run a word longer than the ones every other app prints.
BENEFICIARY_LABEL_RECEIPT = "\n".join(
    [
        "09:06 . 4G 91",
        "Giao dich thành công AGRIBANK",
        "3,719,000 VND",
        "Mã giao dich: 146710598",
        "AGRIBANK",
        "Tên nguòi thu huông TRAN TU UYEN",
        "Sō tài khoån thu huðng 0251002726351",
        "Ngân hàng thu huöng Vietcombank-Ngân",
        "hàng TMCP Ngoai",
        "Thuong Vit Nam",
        "Phí giao dich Min phí",
        "Thòi gian giao dich 12-08-2026 09:06:10",
        "Ni dung chuyên tiên Trieu Long chuyen",
        "khoan nhanh qua Zalo",
        "Luu danh ba Luu ånh Chia sè",
    ]
)

# ABBank, read off a photo of a phone held sideways. It labels the account number
# "Tài khoản người nhận" and the message "Cùng nội dung".
SIDEWAYS_ABBANK_RECEIPT = "\n".join(
    [
        "09:50 .",
        "ABBank",
        "Chuyên tiên thành công",
        "2.800.000 VND",
        "Dên TRAN TU UYEN",
        "Tài khoàn nguōi nhân",
        "0251 0027 2635 1",
        "VIETCOMBANK (VCB)",
        "Cüng nôi dung NGUYEN THI HUONG LY chuyen tien",
        "Thöi gian chuyên",
        "14/08/2026 09:49",
        "B Ban dugc tng thè tín dung, min 100% phí phát hành Nhân thè",
        "Luu nguòi nhân",
        "Vê trang chù",
    ]
)


# Techcombank: the beneficiary block is headed by a bare "Tài khoản nhận" label,
# the name rides on the headline ("Tới TRAN TU UYEN"), and a promo banner near
# the foot starts with the vowel-only word "ưu" — which used to pad the word
# count enough for "Nhận ưu" to pass for the "người nhận" name label.
TECHCOMBANK_PROMO_RECEIPT = "\n".join(
    [
        "09:49 26",
        "Zalo",
        "TECHCOMBANK",
        "Chuyên thành công",
        "TÓi TRAN TU UYEN",
        "VND 30,117,000",
        "Tài khon nhn",
        "Ngân hàng TMCP Ngoai thuong Vit Nam",
        "0251 0027 2635 1",
        "Lòi nhán",
        "Hong Tuoi chuyen khoan nhanh qua Zalo",
        "Ngày thuc hin",
        "20 thg 8, 2026 lúc 9:49",
        "Mã giao dich",
        "FT26232679240523",
        "Dây là khoån chi tiêu gì?",
        "Nhân uu di dén 1.1 triu U-Point khi mð thè tín",
        "dung mói.Mò thè ngay",
        "Chia sè Luu ngui nhân Chia tiên",
        "Hoàn thành",
    ]
)


def test_labelled_receipt() -> None:
    info = normalize_info(parse_bank_text(LABELLED_RECEIPT))

    assert info["amount"] == "89000"
    assert info["account_number"] == "0251002726351"
    assert info["account_name"] == "TRAN TU UYEN"
    assert info["bank"] == "Vietcombank"
    assert info["content"] == "VU NGOC HOAN chuyen tien"


def test_labelled_receipt_ignores_sender_side_rows() -> None:
    # The fee, the transfer type and the transaction id must not leak into any
    # beneficiary field.
    info = normalize_info(parse_bank_text(LABELLED_RECEIPT))

    assert "15507345760" not in info.values()
    assert "Min phí" not in info["content"]


def test_card_receipt_without_labels() -> None:
    info = normalize_info(parse_bank_text(CARD_RECEIPT))

    assert info["amount"] == "220000"
    assert info["account_number"] == "0251002726351"
    assert info["account_name"] == "TRAN TU UYEN"
    assert info["bank"] == "Vietcombank"
    assert info["content"] == "LE QUOC DAI chuyen tien"


def test_card_receipt_joins_wrapped_content() -> None:
    info = normalize_info(parse_bank_text(WRAPPED_CARD_RECEIPT))

    assert info["content"] == "Ice Cold Killer chuyen khoan nhanh qua Zalo"
    # The app that issued the receipt is not the beneficiary bank.
    assert info["bank"] == "Vietcombank"


def test_photographed_receipt_recovers_key_fields() -> None:
    info = normalize_info(parse_bank_text(PHOTOGRAPHED_RECEIPT))

    assert info["amount"] == "3518000"
    assert info["account_number"] == "0251002726351"
    assert info["bank"] == "Vietcombank"


def test_inline_bank_row_receipt() -> None:
    info = normalize_info(parse_bank_text(INLINE_BANK_ROW_RECEIPT))

    assert info["amount"] == "1188000"
    # The number shares its row with the bank name.
    assert info["account_number"] == "0251002726351"
    assert info["bank"] == "Vietcombank"
    # The four-word "Nội dung chuyển tiền" label must be consumed whole, and the
    # "Hình thức" row below must not be pulled in as a continuation.
    assert info["content"] == "NGUYEN TRUC PHUONG chuyen tien"
    # OCR damaged the "Người nhận" label past matching ("Nguròi nhn"), so the name
    # is recovered from the row above the bank instead.
    assert info["account_name"] == "TRAN TU UYEN"


def test_inline_prefix_receipt() -> None:
    info = normalize_info(parse_bank_text(INLINE_PREFIX_RECEIPT))

    assert info["amount"] == "100000"
    assert info["account_number"] == "0251002726351"
    assert info["account_name"] == "TRAN TU UYEN"
    assert info["bank"] == "Vietcombank"
    assert info["content"] == "Nguyen Ngoc Minh chuyen khoan nhanh qua Zalo"


def test_detail_table_prefers_beneficiary_rows() -> None:
    info = normalize_info(parse_bank_text(DETAIL_TABLE_RECEIPT))

    assert info["amount"] == "1000000"
    assert info["account_name"] == "TRAN TU UYEN"
    assert info["bank"] == "Vietcombank"
    assert info["content"] == "Hong Tran chuyen khoan nhanh qua Zalo"
    # Not the sender's account on the "Số tài khoản chuyển" row, and not the
    # transaction id.
    assert info["account_number"] == "0251002726351"


def test_prose_receipt_reports_only_what_is_printed() -> None:
    info = normalize_info(parse_bank_text(PROSE_RECEIPT))

    assert info["amount"] == "1500000"
    assert info["account_number"] == "1038079000"
    # Nothing on this receipt names a beneficiary, a bank or a message.
    assert info["account_name"] == ""
    assert info["bank"] == ""
    assert info["content"] == ""


def test_banner_does_not_extend_the_message() -> None:
    info = normalize_info(parse_bank_text(BANNER_RECEIPT))

    assert info["content"] == "coc do amac"
    assert info["account_number"] == "0251002726351"
    assert info["account_name"] == "TRAN TU UYEN"
    assert info["bank"] == "Vietcombank"


def test_grouped_number_and_truncated_bank() -> None:
    info = normalize_info(parse_bank_text(GROUPED_NUMBER_RECEIPT))

    assert info["amount"] == "400000"
    assert info["account_number"] == "0251002726351"
    assert info["account_name"] == "TRAN TU UYEN"
    # "VIETCOMBAN..." resolves through the ellipsis, and the app's own TPBank
    # branding does not win.
    assert info["bank"] == "Vietcombank"
    assert info["content"] == "Tri chuyen khoan nhanh qua Zalo"


def test_two_block_receipt_skips_the_sender_block() -> None:
    info = normalize_info(parse_bank_text(TWO_BLOCK_RECEIPT))

    assert info["amount"] == "500000"
    # The sender's "1000 3514 330" sits in an earlier block.
    assert info["account_number"] == "0251002726351"
    assert info["account_name"] == "TRAN TU UYEN"
    # The receipt shows the beneficiary bank as a logo only.
    assert info["bank"] == ""
    assert info["content"] == "Cao Van Loi chuyen tien quan ao da banh"


def test_company_beneficiary_receipt() -> None:
    info = normalize_info(parse_bank_text(COMPANY_RECEIPT))

    assert info["account_number"] == "1059814478"
    assert info["account_name"] == "CTY TNHH MTV SX TM VA DV AMAC"
    assert info["bank"] == ""
    assert info["content"] == "DO HONG VU chuyen tien coc"
    # OCR lost the leading digit of "1 000 000 đ" (true amount: 1000000); a zero
    # amount is reported as none so the caller can re-read the image.
    assert info["amount"] == ""


def test_amount_without_currency_glyph_and_fee_row() -> None:
    info = normalize_info(parse_bank_text(VPBANK_NO_CURRENCY_RECEIPT))

    assert info["amount"] == "500000"
    assert info["account_number"] == "1059814478"
    assert info["account_name"] == "CTY TNHH MTV SX TM VA DV AMAC"
    assert info["bank"] == ""
    # The fee row must not be glued onto the message as a "wrapped" line.
    assert info["content"] == "Coc tien may ao dong phuc"


def test_zero_amount_is_never_reported() -> None:
    text = "\n".join(["Thành công", "0 d", "Phí 0 VND", "1059814478"])
    assert normalize_info(parse_bank_text(text))["amount"] == ""
    assert normalize_amount("0 d") == ""
    assert normalize_amount("000 000 d") == ""
    # A zero row is skipped, not allowed to shadow the real amount after it.
    text = "\n".join(["Phí 0 d", "500 000 d"])
    assert normalize_info(parse_bank_text(text))["amount"] == "500000"


def test_zero_amount_triggers_a_reread(monkeypatch) -> None:
    from markitdown.converters import _bank_transfer

    reads = []

    def fake_extract(stream, info, *, debug_timing=False, det_max_side=None):
        reads.append(det_max_side)
        amount = {None: "000 000 d", 1600: "1000 000 d"}[det_max_side]
        return {"account_number": "1059814478", "amount": amount}

    monkeypatch.setattr(_bank_transfer, "_extract_via_ocr", fake_extract)
    info = _bank_transfer.extract_bank_info(io.BytesIO(b""))

    assert info["amount"] == "1000000"
    assert info["account_number"] == "1059814478"
    assert reads == [None, 1600]


def test_non_receipt_is_not_reread(monkeypatch) -> None:
    from markitdown.converters import _bank_transfer

    reads = []
    monkeypatch.setattr(
        _bank_transfer,
        "_extract_via_ocr",
        lambda *a, **k: reads.append(1) or {},
    )
    assert _bank_transfer.extract_bank_info(io.BytesIO(b""))["amount"] == ""
    assert len(reads) == 1


def test_payment_order_reads_the_beneficiary_block() -> None:
    info = normalize_info(parse_bank_text(PAYMENT_ORDER_RECEIPT))

    assert info["amount"] == "10342080"
    assert info["account_number"] == "1059814478"
    assert (
        info["account_name"] == "CONG TY TNHH MTV SAN XUAT THUONG MAI VA DICH VU AMAC"
    )
    assert info["bank"] == "Vietcombank"
    assert info["content"] == "Thanh toan tien hang-Amac"


def test_payment_order_ignores_the_payer_block() -> None:
    # The payer's rows carry the same labels as the beneficiary's, and ACB is
    # both the payer's bank and the bank issuing the form.
    info = normalize_info(parse_bank_text(PAYMENT_ORDER_RECEIPT))

    assert info["account_number"] != "28271688"
    assert "RSVP" not in info["account_name"]
    assert info["bank"] != "ACB"


def test_beneficiary_labelled_receipt() -> None:
    # "Số tài khoản thụ hưởng" used to match only the shorter "số tài khoản",
    # leaving "thụ hưởng" glued to the front of the number, and "Tên người thụ
    # hưởng" matched nothing at all.
    info = normalize_info(parse_bank_text(BENEFICIARY_LABEL_RECEIPT))

    assert info["amount"] == "3719000"
    assert info["account_number"] == "0251002726351"
    assert info["account_name"] == "TRAN TU UYEN"
    assert info["bank"] == "Vietcombank"
    assert info["content"] == "Trieu Long chuyen khoan nhanh qua Zalo"


def test_sideways_abbank_receipt() -> None:
    info = normalize_info(parse_bank_text(SIDEWAYS_ABBANK_RECEIPT))

    assert info["amount"] == "2800000"
    assert info["account_number"] == "0251002726351"
    assert info["account_name"] == "TRAN TU UYEN"
    assert info["bank"] == "Vietcombank"
    assert info["content"] == "NGUYEN THI HUONG LY chuyen tien"


def test_techcombank_promo_receipt() -> None:
    info = normalize_info(
        parse_bank_text(
            TECHCOMBANK_PROMO_RECEIPT,
            line_confidences=[0.99] * len(TECHCOMBANK_PROMO_RECEIPT.splitlines()),
        )
    )

    assert info["amount"] == "30117000"
    assert info["account_number"] == "0251002726351"
    # The name comes off the headline row above the bank, not out of the promo
    # banner at the foot of the receipt.
    assert info["account_name"] == "TRAN TU UYEN"
    assert info["bank"] == "Vietcombank"
    assert info["content"] == "Hong Tuoi chuyen khoan nhanh qua Zalo"
    assert info["transaction_time"] == "2026-08-20T09:49"
    assert (
        normalize_info(parse_bank_text(TECHCOMBANK_PROMO_RECEIPT))["transaction_time"]
        == ""
    )


def test_extraction_takes_no_cloud_backend() -> None:
    # `--bank` is local-only: the signature carries no cloud switch and the
    # module does not reach for the cloud client.
    from markitdown.converters import _bank_transfer

    assert (
        "force_cloud"
        not in inspect.signature(_bank_transfer.extract_bank_info).parameters
    )
    assert not hasattr(_bank_transfer, "_vlm_client")


def test_non_receipt_yields_empty_fields() -> None:
    info = normalize_info(parse_bank_text(NOT_A_RECEIPT))

    assert info == empty_info()


def test_empty_text_yields_empty_fields() -> None:
    assert normalize_info(parse_bank_text("")) == empty_info()


def test_normalize_amount_strips_separators_and_currency() -> None:
    assert normalize_amount("3,518,000 VND") == "3518000"
    assert normalize_amount("89.000đ") == "89000"
    assert normalize_amount("1.234.567,00") == "1234567"
    assert normalize_amount("Miễn phí") == ""


def test_match_bank_resolves_aliases() -> None:
    assert match_bank("Vietcombank (VCB)") == "Vietcombank"
    assert match_bank("Ngân hàng TMCP Ngoại Thương Việt Nam") == "Vietcombank"
    assert match_bank("MBBank") == "MB Bank"
    assert match_bank("chuyen tien") is None


def test_json_output_keeps_every_field() -> None:
    rendered = to_json(empty_info())

    for field in (
        "amount",
        "account_number",
        "account_name",
        "bank",
        "content",
        "transaction_time",
    ):
        assert f'"{field}": ""' in rendered


if __name__ == "__main__":
    """Runs this file's tests from the command line."""
    test_labelled_receipt()
    test_labelled_receipt_ignores_sender_side_rows()
    test_card_receipt_without_labels()
    test_card_receipt_joins_wrapped_content()
    test_photographed_receipt_recovers_key_fields()
    test_inline_bank_row_receipt()
    test_inline_prefix_receipt()
    test_detail_table_prefers_beneficiary_rows()
    test_prose_receipt_reports_only_what_is_printed()
    test_banner_does_not_extend_the_message()
    test_grouped_number_and_truncated_bank()
    test_two_block_receipt_skips_the_sender_block()
    test_company_beneficiary_receipt()
    test_payment_order_reads_the_beneficiary_block()
    test_payment_order_ignores_the_payer_block()
    test_techcombank_promo_receipt()
    test_non_receipt_yields_empty_fields()
    test_empty_text_yields_empty_fields()
    test_normalize_amount_strips_separators_and_currency()
    test_match_bank_resolves_aliases()
    test_json_output_keeps_every_field()
    print("All tests passed!")
