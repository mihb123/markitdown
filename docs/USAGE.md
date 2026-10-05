# Hướng dẫn sử dụng MarkItDown

MarkItDown chuyển tài liệu, hình ảnh, âm thanh và nội dung web thành Markdown. Repo
này còn bổ sung OCR ảnh, nhận diện biên lai chuyển khoản và HTTP API.

## 0. Cài đặt

```bash
git clone git@github.com:microsoft/markitdown.git
cd markitdown
pip install -e 'packages/markitdown[all,image-ocr]'
```

- `[all]`: hỗ trợ PDF, Word, Excel, PowerPoint, audio, YouTube và các định dạng tài liệu khác.
- `[image-ocr]`: thêm OCR offline cho `--text` và `--bank`.
- Kiểm tra cài đặt: `markitdown --version` và `markitdown --help`.

## 1. Làm việc với PDF

**Command:**

```bash
markitdown path-to-file.pdf > document.md
```

**Usage:**

- `-o document.md`: ghi thẳng kết quả ra file, ví dụ `markitdown report.pdf -o report.md`.
- `--use-docintel -e URL`: dùng Azure Document Intelligence cho PDF scan hoặc bố cục phức tạp.
- `--use-cu --cu-endpoint URL`: dùng Azure Content Understanding để trích xuất chất lượng cao hơn.

## 2. Làm việc với Word

**Command:**

```bash
markitdown document.docx -o document.md
```

**Usage:**

- Hỗ trợ `.docx`; tiêu đề, danh sách, bảng, link và comment được chuyển sang Markdown khi có thể.
- `--keep-data-uris`: giữ nguyên ảnh dạng data URI thay vì rút gọn, ví dụ `markitdown document.docx --keep-data-uris`.

## 3. Làm việc với Excel và CSV

**Command:**

```bash
markitdown workbook.xlsx -o workbook.md
markitdown data.csv -o data.md
```

**Usage:**

- Hỗ trợ `.xlsx`, `.xls` và `.csv`; dữ liệu bảng được xuất thành bảng Markdown.
- `-c UTF-8`: chỉ định charset khi CSV bị nhận sai encoding, ví dụ `markitdown data.csv -c UTF-8`.

## 4. Làm việc với PowerPoint

**Command:**

```bash
markitdown slides.pptx -o slides.md
```

**Usage:**

- Hỗ trợ `.pptx`; lấy text, tiêu đề, bảng, ghi chú và nội dung biểu đồ khi có thể.
- `--use-cu --cu-endpoint URL`: dùng Content Understanding nếu slide chứa nhiều nội dung khó trích xuất.

## 5. Bóc tách text từ ảnh

**Command:**

```bash
markitdown --text image.png -o text.txt
```

**Usage:**

- `-t`, `--text`: OCR ảnh `.jpg`, `.jpeg`, `.png`, `.webp` bằng model offline.
- `-cl`, `--cloud`: dùng VLM đã cấu hình thay cho OCR offline, ví dụ `markitdown -t -cl image.jpg`.
- `--debug-timing`: in thời gian từng công đoạn ra stderr để chẩn đoán hiệu năng.

## 6. Nhận diện biên lai chuyển khoản

**Command:**

```bash
markitdown --bank receipt.png
```

**Usage:**

- `-b`, `--bank`: trả JSON gồm `amount`, `account_number`, `account_name`, `bank`, `content`.
- Tính năng này luôn chạy OCR local; field không nhận diện được là chuỗi rỗng và `--cloud` không áp dụng.
- Lưu kết quả: `markitdown -b receipt.png -o receipt.json`.

## 7. Mô tả ảnh bằng VLM

**Command:**

```bash
markitdown --des vi image.jpg -o description.txt
```

**Usage:**

- `-d`, `--des [en|vi]`: mô tả ảnh bằng cloud VLM; mặc định là tiếng Anh.
- Ví dụ tiếng Anh: `markitdown -d image.jpg`; tiếng Việt: `markitdown -d vi image.jpg`.
- Cần cấu hình `MARKITDOWN_VLM_BASE_URL`, `MARKITDOWN_VLM_API_KEY` và `MARKITDOWN_VLM_MODEL`.

Ví dụ `.env`:

```env
MARKITDOWN_VLM_BASE_URL=https://your-provider.example/v1
MARKITDOWN_VLM_API_KEY=your_api_key
MARKITDOWN_VLM_MODEL=your_vision_model
```

## 8. Làm việc với audio

**Command:**

```bash
markitdown recording.mp3 -o transcript.md
```

**Usage:**

- Hỗ trợ `.wav`, `.mp3`, `.m4a` và audio trong `.mp4`; metadata và transcript được xuất ra Markdown.
- MP3/M4A/MP4 cần `ffmpeg`; cài extra riêng bằng `pip install 'markitdown[audio-transcription]'`.

## 9. Làm việc với HTML, text và notebook

**Command:**

```bash
markitdown page.html -o page.md
markitdown notebook.ipynb -o notebook.md
```

**Usage:**

- Hỗ trợ HTML, TXT, JSON, XML và Jupyter Notebook `.ipynb`.
- `-c CHARSET`: chỉ định charset; `-m MIME_TYPE` hoặc `-x EXTENSION` giúp nhận dạng dữ liệu không có tên file.

## 10. Làm việc với ZIP, EPUB và Outlook

**Command:**

```bash
markitdown archive.zip -o archive.md
markitdown book.epub -o book.md
markitdown email.msg -o email.md
```

**Usage:**

- ZIP được duyệt từng file và ghép kết quả; EPUB giữ cấu trúc sách; MSG lấy nội dung và metadata email.
- Cài `markitdown[all]` để có đủ dependency cho các định dạng này.

## 11. Chuyển nội dung từ URL và YouTube

**Command:**

```bash
markitdown https://example.com/article -o article.md
markitdown 'https://www.youtube.com/watch?v=VIDEO_ID' -o video.md
```

**Usage:**

- URL HTTP(S) được tải rồi tự nhận dạng định dạng; chỉ dùng URL từ nguồn tin cậy.
- YouTube hỗ trợ URL `watch`, `youtu.be`, `shorts` và `embed`; transcript phụ thuộc video có phụ đề khả dụng.

## 12. Đọc dữ liệu từ stdin

**Command:**

```bash
cat path-to-file.pdf | markitdown -x pdf > document.md
```

**Usage:**

- Bỏ `filename` để đọc stdin; dùng `-x pdf`, `-m application/pdf` hoặc `-c UTF-8` khi cần gợi ý định dạng.
- `-o FILE` và chuyển hướng `> FILE` đều dùng được; `-o` thuận tiện hơn trên Windows.

## 13. Dùng Azure Document Intelligence

**Command:**

```bash
markitdown report.pdf --use-docintel -e 'https://RESOURCE.cognitiveservices.azure.com/' -o report.md
```

**Usage:**

- `--use-docintel`: bật Document Intelligence; `-e`, `--endpoint` truyền endpoint Azure.
- Có thể đặt `MARKITDOWN_DOCINTEL_ENDPOINT` trong `.env` rồi chỉ chạy `markitdown report.pdf --use-docintel`.

## 14. Dùng Azure Content Understanding

**Command:**

```bash
markitdown report.pdf --use-cu --cu-endpoint 'https://RESOURCE.cognitiveservices.azure.com/' -o report.md
```

**Usage:**

- `--use-cu`: bật Content Understanding; endpoint cũng có thể lấy từ `MARKITDOWN_CU_ENDPOINT`.
- `--cu-analyzer ID`: chọn custom analyzer; `--cu-file-types pdf,jpeg,mp4` giới hạn loại file chuyển qua CU.

## 15. Dùng plugin

**Command:**

```bash
markitdown --list-plugins
markitdown --use-plugins document.pdf -o document.md
```

**Usage:**

- `--list-plugins`: liệt kê plugin đã cài; `-p`, `--use-plugins`: bật plugin cho lần chuyển đổi hiện tại.
- Plugin mặc định bị tắt; chỉ cài và bật plugin từ nguồn tin cậy.

## 16. Dùng Python API

**Command:**

```python
from markitdown import MarkItDown

result = MarkItDown().convert("report.pdf")
print(result.markdown)
```

**Usage:**

- `convert(path_or_url)`: tự nhận dạng nguồn; `convert_local()` chỉ đọc file local; `convert_stream()` nhận binary stream.
- Dùng hàm hẹp nhất phù hợp với nguồn dữ liệu để dễ kiểm soát quyền truy cập và bảo mật.

## 17. Chạy HTTP API

**Command:**

```bash
make start
```

**Usage:**

- `make start|stop|restart|status|logs`: quản lý server nền; `make serve` chạy foreground.
- API mặc định ở `http://localhost:9890`; Swagger UI tại `http://localhost:9890/docs`.
- Đặt `MARKITDOWN_API_KEYS=mid_your_key` trong `.env`, rồi gửi header `X-API-Key` cho request `/v1/*`.

Các endpoint chính:

| Endpoint | Mục đích | Ví dụ ngắn |
| --- | --- | --- |
| `POST /v1/bank` | Đọc biên lai, trả JSON 5 field | `curl -H "X-API-Key: $MID_KEY" -F "file=@receipt.png" localhost:9890/v1/bank` |
| `POST /v1/text` | OCR ảnh, trả link tải `.txt` | `curl -H "X-API-Key: $MID_KEY" -F "file=@image.png" localhost:9890/v1/text` |
| `POST /v1/describe` | Mô tả ảnh bằng VLM | `curl -H "X-API-Key: $MID_KEY" -F "file=@image.jpg" -F "language=vi" localhost:9890/v1/describe` |
| `POST /v1/documents` | Chuyển tài liệu, trả link tải `.md` | `curl -H "X-API-Key: $MID_KEY" -F "file=@report.pdf" localhost:9890/v1/documents` |
| `GET /v1/files/{id}` | Tải file kết quả | `curl -OJ localhost:9890/v1/files/FILE_ID` |
| `GET /health` | Kiểm tra trạng thái server | `curl localhost:9890/health` |

Mỗi endpoint xử lý file nhận một trong hai field: `file=@path` để upload hoặc
`url=https://...` để server tải nguồn về. Xem [tài liệu HTTP API đầy đủ](API.md)
để biết cấu hình, response, mã lỗi, giới hạn tải và hướng dẫn production.

## 18. Các flag CLI dùng chung

| Flag | Ý nghĩa | Ví dụ |
| --- | --- | --- |
| `-o`, `--output FILE` | Ghi kết quả ra file thay vì stdout | `markitdown file.pdf -o file.md` |
| `-x`, `--extension EXT` | Gợi ý phần mở rộng khi nguồn không có tên | `cat file | markitdown -x pdf` |
| `-m`, `--mime-type TYPE` | Gợi ý MIME type | `cat file | markitdown -m application/pdf` |
| `-c`, `--charset NAME` | Gợi ý charset | `markitdown data.csv -c UTF-8` |
| `--keep-data-uris` | Không rút gọn data URI | `markitdown page.html --keep-data-uris` |
| `--debug-timing` | In timing xử lý ra stderr | `markitdown -t image.png --debug-timing` |
| `-v`, `--version` | In version và thoát | `markitdown --version` |
| `-h`, `--help` | Xem trợ giúp CLI hiện tại | `markitdown --help` |

## 19. Docker

**Command:**

```bash
docker build -t markitdown:latest .
docker run --rm -i markitdown:latest < document.pdf > document.md
```

**Usage:**

- Image mặc định chạy CLI và đọc dữ liệu từ stdin; mount volume nếu cần đọc nhiều file local.
- Không đưa API key hoặc credential cloud trực tiếp vào Docker image; truyền qua biến môi trường lúc chạy.

## Lưu ý bảo mật

- CLI chạy với quyền của process hiện tại và có thể đọc file hoặc URL mà process truy cập được.
- Không truyền trực tiếp path/URL không tin cậy vào `convert()`; ưu tiên `convert_local()` hoặc `convert_stream()` khi phù hợp.
- Khi public HTTP API, bật API key, đặt sau reverse proxy/firewall và giữ cơ chế chặn URL private mặc định.
