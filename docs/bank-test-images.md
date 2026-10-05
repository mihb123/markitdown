# Bộ ảnh test cho `--bank` / `POST /v1/bank`

Ghi lại 13 ảnh biên lai dùng để kiểm tra độ chính xác của việc bóc tách 5 trường
(`amount`, `account_number`, `account_name`, `bank`, `content`).

Mỗi ảnh được chọn vì nó phá một giả định khác nhau về cách biên lai được trình
bày. Text OCR thô của từng ảnh đã được ghi thành fixture verbatim trong
`packages/markitdown/tests/test_bank_transfer.py`, nên bộ test chạy được mà
không cần model OCR lẫn ảnh gốc — nhưng vẫn giữ URL ở đây để tái tạo khi cần.

Base URL: `https://amacsport.sgp1.digitaloceanspaces.com/images/`

## 1. Bảng tra cứu

| # | File | App gửi | Layout | Điểm khó |
| - | ---- | ------- | ------ | -------- |
| 1 | `6a70b0495864d.png` | MSB | thẻ, nhãn dọc | số TK nằm chung dòng với tên bank (`Vietcombank • 0251...`); nhãn 4 từ `Nội dung chuyển tiền` |
| 2 | `6a70b07baa157.jpg` | MB Bank | thẻ, không nhãn | tên/bank/số TK/nội dung xếp chồng; footer có tên bank của app (`MBBank`) |
| 3 | `6a70b0ce14ab6.png` | BIDV | nhãn inline | giá trị có prefix `Đến:` / `Tài khoản:`; số tham chiếu 34 ký tự |
| 4 | `6a70b0f791f95.png` | Sacombank | bảng 2 cột | có **số TK người gửi**, mã giao dịch, và dòng `Người thụ hưởng nhận(đ)` dễ bị nhầm là tên |
| 5 | `6a70b3c4262c4.png` | (tiếng Anh) | câu văn xuôi | `You have successfully transferred ... to account number ...`; không có tên/bank/nội dung |
| 6 | `6a7d89171a48e.png` | VietinBank | thẻ | banner quảng cáo ngay dưới nội dung; số TK + bank chung dòng, bank wrap 2 dòng |
| 7 | `6a7d8a42a4010.png` | ACB | nhãn ngắn | nhãn `Từ` / `Đến` chỉ 1 âm; số tiền còn viết bằng chữ; nội dung wrap 2 dòng |
| 8 | `6a7d93da93705.png` | TPBank | thẻ | **số TK tách nhóm bằng space** (`0251 0027 26351`); tên bank bị cắt `VIETCOMBANK, V...` |
| 9 | `6a7db7b0d2375.png` | Techcombank | nhãn dọc | **tiền tệ đứng trước số** (`VND 14,642,000`); nhãn `Tới` |
| 10 | `6a7dbbb73070b.png` | TPBank | 2 block Từ/Tới | bank **chỉ có logo, không có chữ** → `bank` phải rỗng; block người gửi có số TK riêng |
| 11 | `6a7ddf99235b9.png` | TPBank | thẻ | tên bank bị cắt còn `VIETCOMBAN...`; banner quảng cáo ở đầu ảnh |
| 12 | `6a715709dfd45.jpg` | ACB | nhãn ngắn | banner NAPAS chiếm 6 dòng OCR ngay sau nội dung |
| 13 | `6a71585d6090d.png` | VPBank | dark mode | tên người nhận là **tên công ty**; bank chỉ có logo → rỗng; dark mode làm OCR hỏng chữ số |

## 2. Giá trị kỳ vọng

Tất cả đều là biên lai chuyển vào cùng vài tài khoản thật, nên `account_number`
lặp lại nhiều — đó là chủ ý: nó giúp phát hiện ngay khi parser lấy nhầm số của
người gửi hoặc mã giao dịch.

| # | amount | account_number | account_name | bank | content |
| - | ------ | -------------- | ------------ | ---- | ------- |
| 1 | `1188000` | `0251002726351` | `TRAN TU UYEN` | `Vietcombank` | `NGUYEN TRUC PHUONG chuyen tien` |
| 2 | `1200000` | `0251002726351` | `TRAN TU UYEN` | `Vietcombank` | `ZED SPORT` |
| 3 | `100000` | `0251002726351` | `TRAN TU UYEN` | `Vietcombank` | `Nguyen Ngoc Minh chuyen khoan nhanh qua Zalo` |
| 4 | `1000000` | `0251002726351` | `TRAN TU UYEN` | `Vietcombank` | `Hong Tran chuyen khoan nhanh qua Zalo` |
| 5 | `1500000` | `1038079000` | *(rỗng)* | *(rỗng)* | *(rỗng)* |
| 6 | `1700000` | `0251002726351` | `TRAN TU UYEN` | `Vietcombank` | `coc do amac` |
| 7 | `24746000` | `0251002726351` | `TRAN TU UYEN` | `Vietcombank` | `KIWI CHUYEN KHOAN-130826-11:42:59 Ytfr066255` |
| 8 | `1800000` | `0251002726351` | `TRAN TU UYEN` | `Vietcombank` | `lop 10A4 MC ck coc ao lop bo the thao` |
| 9 | `14642000` | `0251002726351` | `TRAN TU UYEN` | `Vietcombank` | `VU MINH TIEN chuyen tien` |
| 10 | `500000` | `0251002726351` | `TRAN TU UYEN` | *(rỗng)* | `Cao Van Loi chuyen tien quan ao da banh` |
| 11 | `400000` | `0251002726351` | `TRAN TU UYEN` | `Vietcombank` | `Tri chuyen khoan nhanh qua Zalo` |
| 12 | `1683000` | `1038079000` | `TRAN TU UYEN` | `Vietcombank` | `HA BICH HAO CHUYEN KHOAN-040826-09:52:50 toMS846752` |
| 13 | `1000000` | `1059814478` | `CTY TNHH MTV SX TM VA DV AMAC` | *(rỗng)* | `DO HONG VU chuyen tien coc` |

Về `bank` rỗng ở ảnh 10 và 13: hai biên lai này chỉ vẽ **logo** bank người nhận,
không in tên ra chữ. Parser được thiết kế trả rỗng thay vì lấy tên bank của app
đang mở (TPBank / VPBank) — dữ liệu sai hại hơn dữ liệu thiếu.

## 3. Kết quả hiện tại: 64/65 trường

Chạy toàn bộ 13 ảnh qua đường OCR offline (không dùng cloud VLM), đối chiếu với
bảng ở mục 2. Chỉ còn **một** trường sai:

| Ảnh | OCR đọc ra | Đúng phải là | Ảnh hưởng |
| --- | ---------- | ------------ | --------- |
| 13 | `(i 000 000 d` | `1 000 000 đ` | mất chữ số đầu → `amount` ra `000000` |

Chữ số `1` đã biến mất khỏi đầu vào nên không thể phục hồi ở tầng parser. Ảnh
này là dark mode với số cỡ rất lớn; cloud VLM đọc đúng.

### 3.1 Hai lỗi ở tầng tiền xử lý ảnh đã sửa

Cả hai đều làm giảm chất lượng OCR trên **mọi** ảnh, không riêng biên lai:

| Lỗi | Biểu hiện |
| --- | --------- |
| Truyền mảng **RGB** cho RapidOCR, trong khi nó theo quy ước OpenCV và cần **BGR** — đỏ và xanh bị đảo | `TRAN TU UYEN` bị đọc thành `TRAN TUUYEN`; nhãn tiếng Việt nát nhiều hơn |
| Không xử lý ảnh dark mode (chữ sáng trên nền tối, ngược với dữ liệu model được train) | `CTY TNHH...` bị đọc thành `CTY1 TNHH...` |

Sửa ở `_local_ocr._detect`: đảo kênh màu về BGR, và tự nghịch đảo ảnh khi độ sáng
trung bình dưới 110. Ngưỡng này tách sạch bộ mẫu — ảnh dark mode đo được 49,
12 ảnh còn lại từ 198 đến 244.

### 3.2 Đã thử đổi model OCR — không nên đổi

Đo trên cùng 13 ảnh, cùng parser, chỉ thay model:

| Bộ model | Dung lượng | Điểm |
| -------- | ---------- | ---- |
| **PP-OCRv5 latin (đang dùng)** | 96 MB | **61/65** |
| PP-OCRv6 small-det + medium-rec | 86 MB | 60/65 |
| PP-OCRv6 small-rec | 109 MB | 56/65 |
| PP-OCRv6 medium-rec | 165 MB | 56/65 |
| PP-OCRv6 tiny-rec | 92 MB | 52/65 |

PP-OCRv6 mới hơn nhưng **kém hơn** cho tiếng Việt: nó tối ưu cho CJK, còn bộ
`latin` của v5 chuyên cho chữ Latin.

Về model chuyên tiếng Việt: không có bản chính thức nào. Dict của cả
`latin_PP-OCRv5` và `PP-OCRv6` đều **không chứa** chữ Việt có dấu (thiếu khối
Unicode U+1EA0–U+1EF9), nên hai model này về nguyên tắc không thể xuất ra `ế`,
`ộ`, `ữ`... Chỉ tìm được một bản fine-tune cộng đồng có đủ charset tiếng Việt —
`tieubaoca/pp-ocrv6-medium-rec-vietnamese` (HuggingFace) — nhưng nó chỉ có định
dạng Paddle (62 MB `.pdiparams`), chưa có ONNX, mới đăng và chưa có benchmark.
Vì nền của nó là PP-OCRv6 (bộ kém nhất trong bảng trên), đây không phải hướng
đáng đầu tư trước.

Điều này không chặn `--bank`: cả 5 trường cần lấy đều là ASCII (tên in hoa không
dấu, số, tên bank), còn nhãn tiếng Việt bị nát dấu thì parser đã đối chiếu bằng
bộ xương phụ âm. Dấu tiếng Việt chỉ quan trọng với `--text` / `/v1/text`.

## 4. Cách chạy lại

```bash
# bộ test offline (fixture đã ghi sẵn, không cần model)
pytest packages/markitdown/tests/test_bank_transfer.py

# chạy lại trên ảnh thật
curl -sS -o r1.png https://amacsport.sgp1.digitaloceanspaces.com/images/6a70b0495864d.png
markitdown --bank r1.png

# xem OCR đọc được gì, khi kết quả sai
markitdown --text r1.png
```

Khi thêm ảnh test mới: chạy `markitdown --text <ảnh>` để lấy text OCR thô, dán
verbatim (giữ nguyên dấu bị hỏng) thành fixture mới trong
`test_bank_transfer.py`, rồi bổ sung một dòng vào 2 bảng trên.
