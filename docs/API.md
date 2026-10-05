# MarkItDown HTTP API

API server cho MarkItDown: mỗi tính năng của CLI là một endpoint riêng.

| Endpoint              | Tương đương CLI    | Đầu vào                          | Kết quả trả về                       |
| --------------------- | ------------------ | -------------------------------- | ------------------------------------ |
| `POST /v1/bank`       | `markitdown -b`    | ảnh biên lai chuyển tiền         | JSON 6 trường trong `data`           |
| `POST /v1/text`       | `markitdown -t`    | ảnh                              | link tải file `.txt` đã bóc tách text |
| `POST /v1/describe`   | `markitdown -d`    | ảnh                              | link tải file `.txt` mô tả ảnh        |
| `POST /v1/documents`  | `markitdown FILE`  | pdf, docx, xlsx, pptx, csv, ...  | link tải file `.md`                   |
| `GET /v1/files/{id}`  | —                  | id file đã tạo                   | tải file kết quả                      |
| `GET /health`         | —                  | —                                | trạng thái server                     |

Nguyên tắc: lệnh CLI nào in ra **JSON** thì endpoint trả JSON đó nguyên dạng trong
`data`; lệnh nào in ra **plain text** thì server lưu kết quả thành file và trả về
**đường link tải về** — mở link là tải file, không phải đọc body khổng lồ.

Cả 4 endpoint xử lý file đều nhận **1 trong 2** kiểu đầu vào (xem [mục 5.0](#50-đầu-vào-upload-file-hoặc-url)):

- `file` — upload trực tiếp (multipart)
- `url` — link http(s), server tự tải file về rồi xử lý

Mọi request `/v1/*` cần **API key** (xem [mục 4.1](#41-xác-thực-bằng-api-key)), và
mọi lỗi trả về kèm **`error_code`** để client xử lý (xem [mục 6](#6-mã-lỗi)).

---

## 1. Cài đặt & chạy bằng 1 lệnh

```bash
make start        # chạy nền
# hoặc
make serve        # chạy foreground (Ctrl+C để dừng)
```

Lần đầu chạy, target này tự làm hết phần cài đặt rồi start server:

1. tạo venv `.venv-api` (bằng `uv`),
2. cài `markitdown[all,image-ocr]` + `fastapi` + `uvicorn` + `python-multipart`,
3. thay `opencv-python` bằng `opencv-python-headless` (xem [mục 8](#8-model-ocr-offline)),
4. tải model OCR offline (~92 MB) về `~/.cache/markitdown`,
5. start server ở `PORT` trong `.env` (mặc định `9890`).

Các lần sau chỉ start server (bỏ qua bước cài đặt). Các target liên quan:

```bash
make setup-api   # chỉ cài đặt, không start
make models      # chỉ tải/kiểm tra model OCR offline
make test-api    # chạy test của API
make clean       # xoá .venv-api và build artifacts
```

Nếu muốn tự quản lý môi trường (không dùng Makefile):

```bash
uv venv .venv-api && source .venv-api/bin/activate
uv pip install -e "packages/markitdown[all,image-ocr]" fastapi "uvicorn[standard]" python-multipart
uv pip install --no-deps -e packages/markitdown-api
uv pip uninstall opencv-python && uv pip install opencv-python-headless
python -c "from markitdown.converters._local_ocr import download_models; print(download_models())"
markitdown-api
```

## 2. Cấu hình (file `.env`)

Tất cả cấu hình đọc từ biến môi trường thật, nếu không có thì đọc từ `.env` — thứ
tự tìm: `$MARKITDOWN_ENV_FILE` → `.env` ở thư mục hiện tại hoặc thư mục cha →
`~/.config/markitdown/.env`. Biến môi trường thật luôn thắng giá trị trong file.

| Key                               | Mặc định            | Ý nghĩa                                                        |
| --------------------------------- | ------------------- | -------------------------------------------------------------- |
| `PORT`                            | `9890`              | Port server lắng nghe                                          |
| `MARKITDOWN_API_PORT`             | —                   | Ghi đè `PORT` (khi host đã dùng `PORT` cho service khác)        |
| `MARKITDOWN_API_HOST`             | `0.0.0.0`           | Interface bind                                                  |
| `MARKITDOWN_API_MAX_UPLOAD_MB`    | `25`                | Dung lượng upload tối đa                                        |
| `MARKITDOWN_API_FILE_TTL_MINUTES` | `120`               | Thời gian sống của link tải về                                  |
| `MARKITDOWN_API_OUTPUT_DIR`       | thư mục temp        | Nơi lưu file `.txt`/`.md` được tạo ra                           |
| `MARKITDOWN_API_PUBLIC_URL`       | lấy theo request    | Base URL để tạo link tải (đặt khi chạy sau reverse proxy)        |
| `MARKITDOWN_API_CORS_ORIGINS`     | `*`                 | Danh sách origin cho CORS, phân tách bằng dấu phẩy               |
| `MARKITDOWN_API_ALLOW_PRIVATE_URLS` | `false`           | Cho phép field `url` trỏ tới địa chỉ nội bộ/loopback (chặn SSRF)  |
| `MARKITDOWN_API_KEYS`             | *(trống)*           | API key được chấp nhận, phân tách bằng dấu phẩy. Trống = tắt xác thực |
| `MARKITDOWN_API_PROTECT_DOWNLOADS` | `false`            | Yêu cầu API key cả khi tải file kết quả                           |
| `MARKITDOWN_ENABLE_PLUGINS`       | `false`             | Bật plugin của MarkItDown (ví dụ `markitdown-ocr`)               |
| `MARKITDOWN_VLM_BASE_URL`         | —                   | Endpoint VLM OpenAI-compatible (dùng cho cloud OCR / mô tả ảnh)  |
| `MARKITDOWN_VLM_API_KEY`          | —                   | API key của VLM                                                  |
| `MARKITDOWN_VLM_MODEL`            | —                   | Model vision (ví dụ `gpt-4o-mini`)                               |
| `MARKITDOWN_VLM_TIMEOUT_SECONDS`  | `120`               | Timeout mỗi lần gọi cloud VLM                                    |
| `MARKITDOWN_VLM_MAX_RETRIES`      | `1`                 | Số lần gọi lại khi VLM trả 429/5xx (tôn trọng `Retry-After`)      |
| `MARKITDOWN_OCR_THREADS`          | `-1` (dùng hết core)| Giới hạn số thread ONNX Runtime cho OCR offline, khi máy còn chạy service khác |
| `MARKITDOWN_BANK_TIME_MIN_CONFIDENCE` | `0.85`          | Điểm OCR tối thiểu của dòng chứa thời gian giao dịch; số từ `0` đến `1` |

**Chịu tải & quan sát** (chi tiết ở [mục 7.2](#72-chịu-tải-hàng-loạt-ảnh) và [mục 7.3](#73-log--chẩn-đoán-khi-service-chết)):

| Key                                  | Mặc định       | Ý nghĩa                                                                    |
| ------------------------------------ | -------------- | -------------------------------------------------------------------------- |
| `MARKITDOWN_API_MAX_CONCURRENCY`     | số core (min 2)| Số lượt chuyển đổi chạy đồng thời; vượt thì xếp hàng                        |
| `MARKITDOWN_API_QUEUE_TIMEOUT_SECONDS` | `30`         | Chờ tối đa trong hàng đợi trước khi trả `503 server_busy`                    |
| `MARKITDOWN_API_LOG_LEVEL`           | `INFO`         | Mức log (`DEBUG` để thấy cả log memory định kỳ)                             |
| `MARKITDOWN_API_LOG_FILE`            | *(trống)*      | Ghi log ra file có xoay vòng; trống = chỉ ghi stderr                        |
| `MARKITDOWN_API_LOG_MAX_MB`          | `20`           | Dung lượng tối đa 1 file log trước khi xoay vòng                            |
| `MARKITDOWN_API_LOG_BACKUPS`         | `5`            | Số file log cũ giữ lại                                                      |
| `MARKITDOWN_API_MEMORY_WARN_MB`      | `1024`         | Vượt ngưỡng RSS này thì log WARNING (`0` = tắt)                             |
| `MARKITDOWN_API_MEMORY_CHECK_SECONDS`| `60`           | Chu kỳ kiểm tra bộ nhớ (`0` = tắt)                                          |

## 3. Chạy & quản lý server

### 3.1 Chạy nền (khuyến nghị)

| Lệnh           | Việc nó làm                                                                 |
| -------------- | --------------------------------------------------------------------------- |
| `make start`   | Start server ở chế độ nền, chờ đến khi `/health` phản hồi rồi in trạng thái  |
| `make stop`    | Dừng server (SIGTERM, sau 10s không tắt thì SIGKILL)                        |
| `make restart` | `stop` rồi `start`                                                          |
| `make status`  | In pid, URL, đường dẫn log và kết quả `/health`                             |
| `make logs`    | `tail -f` log của server                                                    |

```bash
$ make start
Starting markitdown-api...
markitdown-api: running (pid 1154296)
  url:    http://127.0.0.1:9890
  docs:   http://127.0.0.1:9890/docs
  log:    .venv-api/server.log
  health: {"status":"success","message":"Service is healthy.","data":{...}}

$ make status
markitdown-api: stopped
```

> [!IMPORTANT]
> `make start` chạy server bằng `nohup` + pid file: tiện lúc phát triển nhưng
> **không tự khởi động lại** khi process chết (segfault, bị OOM killer giết, ai
> đó `kill` nhầm). Chạy production thì dùng systemd — xem [mục 3.3](#33-chạy-dưới-systemd-khuyến-nghị-cho-production).

Chi tiết cách hoạt động:

- pid được lưu ở `.venv-api/server.pid`, log ở `.venv-api/server.log` (log ghi
  nối tiếp qua các lần start). Đổi được bằng `make start API_LOG=/var/log/mid.log`.
- `make start` **không** start trùng: nếu pid còn sống thì chỉ in trạng thái; nếu
  có tiến trình lạ đang chiếm port thì báo lỗi và không start.
- Nếu server chết ngay lúc khởi động, `make start` in 5 dòng log cuối, dọn pid
  file và trả exit code khác 0 (dùng được trong script CI/deploy).
- `start`/`stop`/`status`/`restart` theo `PORT` trong `.env`. Muốn chạy port khác
  thì sửa `PORT` trong `.env` — đừng truyền `ARGS="--port ..."`, vì các target này
  vẫn kiểm tra sức khoẻ theo port trong `.env`.

### 3.2 Chạy foreground

```bash
make serve                                     # dùng PORT trong .env (mặc định 9890)
make serve ARGS="--reload"                     # dev mode, tự reload khi sửa code
.venv-api/bin/markitdown-api --port 8080       # gọi trực tiếp, ghi đè port
.venv-api/bin/markitdown-api --host 127.0.0.1  # chỉ cho phép truy cập nội bộ
```

Sau khi chạy:

- API: `http://localhost:9890`
- Swagger UI: `http://localhost:9890/docs`
- ReDoc: `http://localhost:9890/redoc`

> ⚠️ Server đọc mọi file được gửi lên với quyền của user đang chạy process. Khi mở
> ra Internet: bật API key (mục 4.1) và đặt sau reverse proxy/firewall.

### 3.3 Chạy dưới systemd (khuyến nghị cho production)

```bash
sudo make install-service        # cài unit, enable + start ngay
make service-status              # trạng thái + lý do lần chết gần nhất
make service-logs                # journalctl -f
sudo make uninstall-service      # gỡ
```

Vì sao cần: khi process chết vì lỗi native (segfault trong ONNX Runtime) hoặc bị
kernel OOM killer giết, **bản thân process không kịp ghi log gì** — đúng hiện
tượng "service stopped mà log trống". systemd giải quyết từ bên ngoài:

- `Restart=always` + `RestartSec=3` → tự chạy lại sau vài giây, client không
  còn nhận `502` từ proxy cho tới khi có người phát hiện.
- journald ghi rõ lý do thoát: `Main process exited, code=killed, signal=KILL`
  (OOM) hay `signal=SEGV` (lỗi native) — thông tin mà log ứng dụng không thể có.
- `MemoryMax` (mặc định `2G`, đổi bằng `make install-service SERVICE_MEMORY_MAX=3G`)
  giới hạn service, để một lượt chuyển đổi hỏng không kéo sập cả máy (DB, web).

Unit mẫu nằm ở [deploy/markitdown-api.service](../deploy/markitdown-api.service);
`make install-service` thay đường dẫn/user rồi cài vào `/etc/systemd/system/`.
Khi unit đang chạy, `make start` sẽ từ chối chạy bản thứ hai tranh port.

## 4. Xác thực & định dạng response

### 4.1 Xác thực bằng API key

Mọi request tới `/v1/*` phải mang API key. Gửi theo **một trong hai** cách:

```bash
curl -H "X-API-Key: mid_xxx" ...
curl -H "Authorization: Bearer mid_xxx" ...
```

Key được khai báo trong `.env`, cho phép nhiều key (phân tách bằng dấu phẩy) để
đổi key mà không làm client cũ chết:

```env
MARKITDOWN_API_KEYS=mid_key_dang_dung,mid_key_moi
```

**Thử API trên `/docs`:** bấm nút **Authorize** ở góc trên phải, dán key vào ô
`ApiKeyHeader` rồi bấm *Authorize* — key được gắn vào header `X-API-Key` cho **tất
cả** endpoint (khai báo global trong OpenAPI), và được giữ lại khi reload trang
(`persistAuthorization`). ReDoc cũng hiển thị scheme này. Không cần dán key lại ở
từng endpoint.

Sinh key mới:

```bash
.venv-api/bin/python -c "import secrets; print('mid_' + secrets.token_urlsafe(32))"
```

Sai hoặc thiếu key → `401` với `error_code: unauthorized`:

```json
{
  "status": "error",
  "error_code": "unauthorized",
  "message": "Missing or invalid API key. Send it as 'X-API-Key: <key>' or 'Authorization: Bearer <key>'.",
  "data": null
}
```

**Đường nào không cần key:**

| Đường                  | Lý do                                                                 |
| ---------------------- | --------------------------------------------------------------------- |
| `/health`              | Để monitoring/uptime check gọi được                                    |
| `/docs`, `/redoc`, `/openapi.json` | Trang tài liệu                                            |
| `GET /v1/files/{id}`   | `file_id` chứa 128 bit ngẫu nhiên nên bản thân link đã là "vé" — nhờ vậy mở link trên browser vẫn tải được. Muốn bắt buộc có key ở đây nữa thì đặt `MARKITDOWN_API_PROTECT_DOWNLOADS=true` |

Chi tiết triển khai:

- Key được so sánh bằng `secrets.compare_digest` (constant-time), và so với **tất
  cả** key đã cấu hình dù đã khớp — không để lộ thông tin qua thời gian phản hồi.
- Header key dài quá 512 ký tự bị bỏ qua thẳng, không đem đi so sánh.
- Middleware chạy **trước** mọi handler, nên request không có key sẽ bị chặn
  trước khi server đọc body hay đi tải `url`.
- `MARKITDOWN_API_KEYS` để trống ⇒ **tắt xác thực** (tiện cho dev). Khi đó server
  in cảnh báo lúc khởi động và `/health` trả `auth_required: false`.

### 4.2 Định dạng response

Thành công:

```json
{
  "status": "success",
  "message": "Receipt fields extracted.",
  "data": { }
}
```

Lỗi — thêm `error_code`, và `data` luôn `null`:

```json
{
  "status": "error",
  "error_code": "unsupported_format",
  "message": "This endpoint only accepts images (.jpg, .jpeg, .png, .webp), but the source is not one: content type 'text/plain', file name 'README.md', and its content does not start with JPEG, PNG or WebP data.",
  "data": null
}
```

- `status`: `success` hoặc `error`
- `error_code`: **mã ổn định, client nên `switch/case` trên field này** (xem [mục 6](#6-mã-lỗi))
- `message`: câu giải thích cho người đọc, tiếng Anh, có thể thay đổi cách diễn
  đạt giữa các version → **không parse chuỗi này**; muốn hiện tiếng Việt thì map
  từ `error_code` ở phía client
- `data`: payload của endpoint; `null` khi lỗi

---

## 5. Chi tiết các endpoint

### 5.0 Đầu vào: upload file hoặc URL

Mọi endpoint xử lý file nhận **đúng một** trong hai field sau:

| Field  | Kiểu   | Mô tả                                                                  |
| ------ | ------ | ---------------------------------------------------------------------- |
| `file` | file   | Upload trực tiếp (`multipart/form-data`)                               |
| `url`  | string | Link `http(s)` tới file; server tự tải về rồi xử lý                     |

Gửi cả hai, hoặc không gửi cái nào → `400`.

Mọi ví dụ dưới đây đều cần header API key (xem [mục 4.1](#41-xác-thực-bằng-api-key));
để gọn, các ví dụ sau giả định `export MID_KEY=mid_xxx`.

```bash
# upload
curl -X POST http://localhost:9890/v1/bank \
  -H "X-API-Key: $MID_KEY" \
  -F "file=@bienlai.png"

# hoặc gửi link (dùng -F hay -d đều được)
curl -X POST http://localhost:9890/v1/bank \
  -H "X-API-Key: $MID_KEY" \
  -F "url=https://amacsport.sgp1.digitaloceanspaces.com/images/6a7b4982add3b.png"

curl -X POST http://localhost:9890/v1/bank \
  -H "X-API-Key: $MID_KEY" \
  -d "url=https://amacsport.sgp1.digitaloceanspaces.com/images/6a7b4982add3b.png"
```

**Server làm gì với `url`:**

1. Chỉ chấp nhận `http://` và `https://` (không `file:`, `data:`, `ftp:`) → sai thì `400`.
2. Resolve DNS và **từ chối địa chỉ không public** (loopback, LAN, link-local
   như `169.254.169.254`) → `400`. Đây là lớp chặn SSRF, ngăn người gọi dùng API
   để đọc mạng nội bộ. Muốn cho phép (ví dụ tải file từ máy trong LAN) thì đặt
   `MARKITDOWN_API_ALLOW_PRIVATE_URLS=true`.
3. Bám theo redirect tối đa 5 lần, **kiểm tra lại địa chỉ ở từng hop** (redirect
   về IP nội bộ vẫn bị chặn).
4. Tải về với giới hạn dung lượng giống upload (`MARKITDOWN_API_MAX_UPLOAD_MB`);
   vượt thì `413`. Link tải lỗi/404/403 → `502` kèm HTTP status gốc.
5. Với 3 endpoint xử lý ảnh (`/v1/bank`, `/v1/text`, `/v1/describe`): **validate
   là ảnh mới xử lý**. Thứ tự kiểm tra: đuôi file trong URL → `Content-Type` →
   magic bytes của file (JPEG/PNG/WebP). Không phải ảnh → `415`.

Bước validate bằng magic bytes có mục đích thực tế: object storage
(DigitalOcean Spaces, S3, MinIO...) hay trả `application/octet-stream` và link
nhiều khi không có đuôi file — trường hợp đó vẫn nhận đúng là ảnh, còn file text
đặt tên `.png` thì vẫn bị loại.

### 5.1 `POST /v1/bank` — nhận diện ảnh biên lai chuyển tiền

Tương đương `markitdown --bank <ảnh>`. Trả về đúng 6 trường như CLI; trường không
nhận diện được hoặc không đủ chắc chắn là chuỗi rỗng (`""`).

Trường mới **`transaction_time`** là thời điểm thực hiện giao dịch, theo dạng
`YYYY-MM-DDTHH:MM`, `YYYY-MM-DDTHH:MM:SS` nếu ảnh có giây, hoặc chỉ `YYYY-MM-DD`
khi ảnh có ngày nhưng không đọc chắc được giờ. Giữ nguyên độ chính xác được in
trên ảnh; không tự thêm giây hay suy đoán múi giờ.

Cách tìm:

- Ưu tiên dòng có nhãn như **Ngày thực hiện**, **Ngày giao dịch**, **Ngày chuyển**,
  **Thời gian (giao dịch)**, **Chuyển lúc**, **Transaction date/time**; giá trị nằm
  cùng dòng hoặc dòng kế tiếp.
- Không có nhãn thì lấy dòng in đủ ngày tháng năm, ví dụ `16:54 - 22/09/2026` (MB),
  `16:10 Thứ Ba 22/09/2026` (VCB), `22/08/202610:59:41` (OCR dính chữ),
  `17/08/2026 17:19 • 560K...`, `(TPBank): 17/08/26;13:50`.
- Định dạng ngày: `DD/MM/YYYY`, `DD-MM-YYYY`, `DD.MM.YYYY`, `DD/MM/YY`, `YYYY-MM-DD`,
  `20 thg 8, 2026`, `18 Th8, 2026`, `ngày 20 tháng 8 năm 2026`, `Aug 26, 2026`.
  Ngày số luôn đọc theo kiểu Việt Nam **ngày trước tháng**; chỉ đọc tháng trước
  ngày khi kiểu Việt Nam không thể đúng (`08/17/2026`).
- Giờ `HH:MM` hoặc `HH:MM:SS`, có thể kèm `AM/PM` (`SA/CH`); giờ ở dòng ngay dưới
  ngày (`14–08–2026` / `18:14:25`) hoặc cuối dòng trên (`... vào 09:33` / `15/08/2026`)
  cũng được ghép.
- Ngày phải hợp lệ, từ năm 2000 và không sau ngày mai. Nhiều ngày khác nhau thì
  chỉ giữ ngày có kèm giờ; vẫn mâu thuẫn ⇒ `""`. Nhiều giờ khác nhau cho cùng
  một ngày ⇒ chỉ trả ngày.
- Dòng giá trị phải đạt điểm OCR `MARKITDOWN_BANK_TIME_MIN_CONFIDENCE`, mặc định
  **0.85** (điểm model OCR, không phải cam kết xác suất đúng).

Không lấy giờ trên thanh trạng thái điện thoại (không có ngày đi kèm), ngày
nằm trong lời nhắn, hay suy đoán từ mã giao dịch, tên file hoặc ngày hiện tại.

Cấu hình ngưỡng trong `.env` (biến môi trường thật được ưu tiên):

```env
MARKITDOWN_BANK_TIME_MIN_CONFIDENCE=0.85
```

Tăng ngưỡng để lọc chặt hơn. Giá trị không phải số, ngoài khoảng `0`–`1` hoặc
không hữu hạn được bỏ qua và dùng mặc định `0.85`.

**Chạy hoàn toàn trên local** — endpoint này không gọi cloud VLM. Trước đây nó ưu
tiên cloud và chỉ fallback sang OCR khi cloud lỗi; mỗi request vì thế tốn hàng
chục giây, và khi provider chết thì phải chờ hết timeout mới chạy OCR — công việc
mà OCR vốn tự làm được. Tham số `cloud` đã bị bỏ; truyền vào cũng không có tác
dụng.

**Request** — `multipart/form-data` hoặc `application/x-www-form-urlencoded`

| Field   | Kiểu    | Bắt buộc | Mô tả                                                            |
| ------- | ------- | -------- | ---------------------------------------------------------------- |
| `file`  | file    | ⚠️        | Ảnh biên lai: `.jpg`, `.jpeg`, `.png`, `.webp` — bắt buộc nếu không có `url` |
| `url`   | string  | ⚠️        | Link ảnh biên lai — bắt buộc nếu không có `file`                  |
| `debug` | boolean | ❌        | `true` = trả về chi tiết breakdown thời gian xử lý từng công đoạn (source_load, ocr, parse, total) trong object `debug` |

```bash
curl -X POST http://localhost:9890/v1/bank \
  -F "file=@bienlai.png"

# nhận diện từ link ảnh
curl -X POST http://localhost:9890/v1/bank \
  -F "url=https://amacsport.sgp1.digitaloceanspaces.com/images/6a7b4982add3b.png"

# bật debug để xem chi tiết thời gian xử lý từng bước
curl -X POST http://localhost:9890/v1/bank \
  -F "file=@bienlai.png" -F "debug=true"
```

**Response** `200`

```json
{
  "status": "success",
  "message": "Receipt fields extracted.",
  "data": {
    "amount": "89000",
    "account_number": "0123456789",
    "account_name": "NGUYEN VAN A",
    "bank": "Vietcombank",
    "content": "Thanh toan don hang 12345",
    "transaction_time": ""
  }
}
```

Nếu không nhận diện được trường nào, `status` vẫn là `success` nhưng `message` là
`"No receipt fields could be recognized."` và 6 trường đều rỗng.

**Ví dụ ảnh có thời gian giao dịch rõ ràng:**

```bash
curl -X POST http://localhost:9890/v1/bank \
  -H "X-API-Key: $MID_KEY" \
  -F "url=https://amacsport.sgp1.digitaloceanspaces.com/images/6a867509b44ac.jpg"
```

Ảnh ghi **Ngày thực hiện: 20 thg 8, 2026 lúc 9:49**, nên response là:

```json
{
  "status": "success",
  "message": "Receipt fields extracted.",
  "data": {
    "amount": "30117000",
    "account_number": "0251002726351",
    "account_name": "TRAN TU UYEN",
    "bank": "Vietcombank",
    "content": "Hong Tuoi chuyen khoan nhanh qua Zalo",
    "transaction_time": "2026-08-20T09:49"
  }
}
```

### 5.2 `POST /v1/text` — bóc tách text trong ảnh (OCR)

Tương đương `markitdown --text <ảnh>`. Nội dung text được lưu thành file `.txt`,
response trả về link tải.

**Request** — `multipart/form-data`

| Field   | Kiểu    | Bắt buộc | Mô tả                                                        |
| ------- | ------- | -------- | ------------------------------------------------------------ |
| `file`  | file    | ⚠️        | Ảnh `.jpg`, `.jpeg`, `.png`, `.webp` — bắt buộc nếu không có `url` |
| `url`   | string  | ⚠️        | Link ảnh — bắt buộc nếu không có `file`                       |
| `cloud` | boolean | ❌        | `true` = dùng cloud VLM; mặc định `false` = model OCR offline |

```bash
curl -X POST http://localhost:9890/v1/text \
  -F "file=@hoadon.png" -F "cloud=true"

curl -X POST http://localhost:9890/v1/text \
  -F "url=https://example.com/hoadon.png"
```

**Response** `200`

```json
{
  "status": "success",
  "message": "Text extracted.",
  "data": {
    "file_id": "581cd717ee09422e9c991f61874c0893__hoadon.txt",
    "file_name": "hoadon.txt",
    "download_url": "http://localhost:9890/v1/files/581cd717ee09422e9c991f61874c0893__hoadon.txt",
    "size_bytes": 1841,
    "characters": 1795,
    "expires_at": "2026-08-13T15:11:07.050311+00:00"
  }
}
```

Mở `download_url` trên browser là file được tải về ngay (`Content-Disposition:
attachment`). Link hết hiệu lực sau `MARKITDOWN_API_FILE_TTL_MINUTES` phút.

### 5.3 `POST /v1/describe` — mô tả / caption ảnh

Tương đương `markitdown --des <ảnh>`. Cần cấu hình cloud VLM.

**Request** — `multipart/form-data`

| Field      | Kiểu   | Bắt buộc | Mô tả                                                        |
| ---------- | ------ | -------- | ------------------------------------------------------------ |
| `file`     | file   | ⚠️        | Ảnh `.jpg`, `.jpeg`, `.png`, `.webp` — bắt buộc nếu không có `url` |
| `url`      | string | ⚠️        | Link ảnh — bắt buộc nếu không có `file`                       |
| `language` | string | ❌        | `en` (mặc định) hoặc `vi`                                     |

```bash
curl -X POST http://localhost:9890/v1/describe \
  -F "file=@anh.jpg" -F "language=vi"

curl -X POST http://localhost:9890/v1/describe \
  -F "url=https://example.com/anh.jpg" -F "language=vi"
```

**Response** `200` — cùng cấu trúc `data` như `/v1/text`.

### 5.4 `POST /v1/documents` — chuyển tài liệu sang Markdown

Tương đương `markitdown <file>`. Nhận PDF, DOCX, XLSX, XLS, PPTX, CSV, JSON, XML,
HTML, EPUB, ZIP, MSG, audio... Kết quả markdown được lưu thành file `.md`.

**Request** — `multipart/form-data`

| Field            | Kiểu    | Bắt buộc | Mô tả                                                        |
| ---------------- | ------- | -------- | ------------------------------------------------------------ |
| `file`           | file    | ⚠️        | File tài liệu cần chuyển đổi — bắt buộc nếu không có `url`     |
| `url`            | string  | ⚠️        | Link tài liệu — bắt buộc nếu không có `file`                  |
| `keep_data_uris` | boolean | ❌        | `true` = giữ nguyên data URI (ảnh base64) thay vì cắt bớt    |

Endpoint này không validate định dạng đầu vào (nhận mọi thứ MarkItDown hỗ trợ),
nên link nào cũng tải được — định dạng lạ thì converter sẽ báo lỗi.

```bash
curl -X POST http://localhost:9890/v1/documents \
  -F "file=@baocao.pdf"

curl -X POST http://localhost:9890/v1/documents \
  -F "url=https://example.com/baocao.pdf"
```

**Response** `200`

```json
{
  "status": "success",
  "message": "Document converted.",
  "data": {
    "file_id": "432081cf09f4423d9e1073f1e229a73e__baocao.md",
    "file_name": "baocao.md",
    "download_url": "http://localhost:9890/v1/files/432081cf09f4423d9e1073f1e229a73e__baocao.md",
    "size_bytes": 4703,
    "characters": 4651,
    "expires_at": "2026-08-13T15:11:07.129819+00:00"
  }
}
```

### 5.5 `GET /v1/files/{file_id}` — tải file kết quả

```bash
curl -OJ http://localhost:9890/v1/files/432081cf09f4423d9e1073f1e229a73e__baocao.md
```

Trả về file kèm `Content-Disposition: attachment`, `text/markdown` cho `.md` và
`text/plain` cho `.txt`. Trả `404` nếu `file_id` không tồn tại hoặc link đã hết hạn.

### 5.6 `GET /health`

```json
{
  "status": "success",
  "message": "Service is healthy.",
  "data": {
    "version": "0.1.0",
    "cloud_vlm_configured": true,
    "local_ocr_available": true,
    "plugins_enabled": false,
    "auth_required": true,
    "uptime_seconds": 19,
    "max_concurrency": 2,
    "in_flight": 2,
    "waiting": 3,
    "peak_in_flight": 2,
    "rejected_busy": 0,
    "rss_mb": 189
  }
}
```

Dùng để kiểm tra nhanh: đã cấu hình cloud VLM chưa, model OCR offline có sẵn
không, và **server đang tải bao nhiêu**:

| Trường | Đọc thế nào |
| ------ | ----------- |
| `in_flight` / `max_concurrency` | Đang chuyển đổi bao nhiêu trên tối đa bao nhiêu. Bằng nhau = đang đầy tải. |
| `waiting` | Số request đang xếp hàng chờ tới lượt. Tăng dần = client gửi nhanh hơn server xử lý. |
| `peak_in_flight`, `rejected_busy` | Đỉnh tải và số request đã bị trả `503 server_busy` kể từ lần khởi động này. |
| `rss_mb` | Bộ nhớ thực đang dùng. Tăng đều không giảm = nguy cơ bị OOM killer giết. |
| `uptime_seconds` | Nhỏ bất thường = process vừa chết và được systemd chạy lại. |

`/health` **không đi qua hàng đợi**, nên vẫn trả lời ngay cả khi server đầy tải —
monitoring không bị báo động giả khi đang bận.

---

## 6. Mã lỗi

Client nên xử lý theo `error_code` (chuỗi ổn định), không theo `message`:

| `error_code`            | HTTP  | Ý nghĩa & cách xử lý ở client                                                     |
| ----------------------- | ----- | --------------------------------------------------------------------------------- |
| `unauthorized`          | `401` | Thiếu/sai API key → kiểm tra header, không retry                                   |
| `missing_source`        | `400` | Không gửi `file` lẫn `url` → lỗi lập trình ở client                                |
| `ambiguous_source`      | `400` | Gửi cả `file` và `url` → chỉ gửi một                                               |
| `invalid_parameter`     | `400` | Tham số sai giá trị (ví dụ `language=de`) → sửa tham số                            |
| `invalid_request`       | `422` | Body/field sai kiểu, không parse được form                                          |
| `empty_file`            | `400` | File upload rỗng, hoặc link trả về 0 byte                                           |
| `invalid_url`           | `400` | URL sai scheme, không có host, chứa credential, có ký tự lạ, quá dài, hoặc domain không resolve được |
| `blocked_url`           | `400` | URL trỏ tới địa chỉ nội bộ/loopback → bị chặn có chủ đích (SSRF)                    |
| `upstream_fetch_failed` | `424` | Không tải được file từ `url`: nguồn trả 403/404/5xx, timeout, hoặc redirect vòng. Message có kèm HTTP status gốc. **Không phải lỗi của server này** — sửa/đổi link chứ retry thường vô ích |
| `file_too_large`        | `413` | Vượt `MARKITDOWN_API_MAX_UPLOAD_MB` → không retry                                   |
| `unsupported_format`    | `415` | Không phải ảnh (với 3 endpoint ảnh), hoặc định dạng không được hỗ trợ. Message nói rõ content-type/tên file nhận được |
| `conversion_failed`     | `422` | File đúng định dạng nhưng hỏng/không đọc được                                       |
| `cloud_vlm_failed`      | `502` | Cloud VLM lỗi: sai key, không kết nối được, model từ chối. Có thể retry              |
| `server_busy`           | `503` | Server đã đủ số lượt chuyển đổi đồng thời và hàng đợi chờ quá lâu. **Nên retry** sau `Retry-After` giây (header có sẵn trong response) — ảnh chưa hề được xử lý |
| `missing_dependency`    | `501` | Server thiếu thư viện cho định dạng đó → việc của người vận hành                    |
| `file_not_found`        | `404` | `file_id` không tồn tại hoặc link đã hết hạn → gọi lại endpoint để tạo file mới      |
| `internal_error`        | `500` | Lỗi không lường trước; chi tiết chỉ ghi vào log của server, không trả cho client     |

> [!IMPORTANT]
> **Vì sao `upstream_fetch_failed` là `424` chứ không phải `5xx`:** khi API đứng sau
> Cloudflare (hoặc reverse proxy có trang lỗi riêng), **body của response 5xx bị thay**
> bằng trang lỗi của proxy — client nhận `error code: 502` dạng text, mất luôn
> `error_code` lẫn lý do, không phân biệt được "link ảnh chết" với "service chết".
> Mã 4xx đi qua nguyên vẹn, nên `424` giữ được thông tin cho client xử lý.
> Chỉ những lỗi mà **bản thân server này** hỏng (`internal_error`, `cloud_vlm_failed`)
> và trạng thái quá tải (`server_busy`, `503`) mới còn dùng 5xx — với các mã đó,
> riêng con số status đã đủ nghĩa để client quyết định retry.

Ví dụ 3 lỗi hay gặp nhất khi dùng `url`:

```json
{"status":"error","error_code":"unsupported_format",
 "message":"This endpoint only accepts images (.jpg, .jpeg, .png, .webp), but the source is not one: content type 'text/plain', file name 'notes.txt', and its content does not start with JPEG, PNG or WebP data.","data":null}

{"status":"error","error_code":"upstream_fetch_failed",
 "message":"The url could not be downloaded: the server answered HTTP 403. Check that the link is public and has not expired.","data":null}

{"status":"error","error_code":"invalid_url",
 "message":"Could not resolve the host 'khong-ton-tai.example' in the url.","data":null}
```

---

## 7. Ghi chú vận hành

- **Chọn cloud hay offline**: `/v1/bank` luôn chạy OCR offline, không bao giờ gọi
  cloud. `/v1/text` mặc định OCR offline, chỉ dùng cloud khi truyền `cloud=true`.
  `/v1/describe` luôn dùng cloud.
- **File tạm**: file kết quả nằm trong `MARKITDOWN_API_OUTPUT_DIR`, tự động bị xoá
  khi quá TTL (dọn dẹp mỗi lần có file mới được tạo) — không cần cron job.
- **Song song**: các handler là hàm đồng bộ nên Starlette chạy chúng trong
  threadpool; tác vụ nặng (OCR, PDF) không block event loop. Số lượt chạy đồng
  thời bị giới hạn — xem [mục 7.2](#72-chịu-tải-hàng-loạt-ảnh).
- **Sau reverse proxy**: đặt `MARKITDOWN_API_PUBLIC_URL` để `download_url` trả về
  domain công khai thay vì host nội bộ.
- **Về `url` và bảo mật**: lớp chặn kiểm tra IP tại thời điểm resolve DNS, nên vẫn
  còn khe hở lý thuyết với DNS rebinding (DNS đổi kết quả giữa lúc kiểm tra và lúc
  kết nối). Khi mở ra Internet nên đặt thêm egress firewall hoặc cho server đi qua
  proxy ra ngoài.

### 7.1 Những gì đã làm để client không "tiêm" được gì qua link/payload

| Rủi ro                                                       | Cách chặn                                                                                   |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| Ký tự điều khiển / CRLF trong `url` (làm bẩn log, header injection) | URL bị từ chối nếu chứa whitespace hoặc ký tự điều khiển, trước khi mở kết nối          |
| URL nhét credential (`user:pass@host`) để che host thật       | Từ chối `400 invalid_url`, và message **không** in lại password                              |
| URL dài vô hạn để làm phình log/response                      | Giới hạn 2048 ký tự                                                                          |
| Nội dung/`Content-Type`/`Content-Disposition` từ server lạ bị echo nguyên văn vào response | Mọi message đi qua `sanitize_message`: bỏ ký tự điều khiển, gộp thành 1 dòng, cắt còn 400 ký tự |
| Tên file từ remote dùng để ghi đĩa (`../../etc/passwd`)        | Chỉ lấy basename, cắt 128 ký tự, rồi lọc còn `[A-Za-z0-9._-]`; tên file trên đĩa luôn có prefix UUID |
| Đọc file ngoài thư mục output qua `file_id`                    | `file_id` phải khớp regex `^[0-9a-f]{32}__[A-Za-z0-9._-]+$`                                   |
| File kết quả (nội dung do client kiểm soát) bị browser render như HTML/JS | Luôn `Content-Disposition: attachment`, `Content-Type` do server quyết định, thêm `X-Content-Type-Options: nosniff` và `Content-Security-Policy: default-src 'none'; sandbox` |
| Dùng API làm proxy vào mạng nội bộ (SSRF)                      | Chặn địa chỉ non-public ở mọi redirect hop; chỉ `http/https`                                   |
| Traceback/nội dung nội bộ lọt ra ngoài khi có bug              | Lỗi 500 chỉ trả `internal_error` + câu chung; chi tiết ghi vào log server                     |
| Body/file khổng lồ làm hết RAM                                 | Cắt theo `MARKITDOWN_API_MAX_UPLOAD_MB` cả khi upload và khi tải từ `url`                      |

Lưu ý phạm vi: các lớp trên bảo vệ **server**. Nội dung văn bản trong file kết quả
vẫn là dữ liệu do người gửi kiểm soát — client khi render nội dung đó (ví dụ nhúng
vào HTML) vẫn phải escape như với mọi dữ liệu người dùng khác.
- **Test**: `make test-api` (hoặc `pytest packages/markitdown-api/tests`).

---

### 7.2 Chịu tải hàng loạt ảnh

Bối cảnh thực tế: một message queue phía client đẩy hàng trăm ảnh biên lai sang
`/v1/bank`. Mỗi ảnh tốn vài giây CPU đặc, nên nếu nhận hết cùng lúc thì mỗi
request giữ một thread + một ảnh đã giải mã trong RAM, và trên máy nhỏ process bị
kernel giết trước khi xong.

Cách server xử lý:

| Cơ chế | Chi tiết |
| ------ | -------- |
| **Giới hạn đồng thời** | Chỉ `MARKITDOWN_API_MAX_CONCURRENCY` lượt chuyển đổi chạy cùng lúc (mặc định = số core, tối thiểu 2). Request thứ N+1 xếp hàng chứ không chen vào. |
| **Trả 503 thay vì xếp hàng vô hạn** | Chờ quá `MARKITDOWN_API_QUEUE_TIMEOUT_SECONDS` → `503 server_busy` + header `Retry-After`. Client biết đường quay lại, hàng đợi không phình. |
| **Threadpool bị chặn trên** | Threadpool của Starlette (mặc định 40 thread) bị hạ xuống sát mức đồng thời cho phép, để không có đường nào lách qua giới hạn. |
| **Endpoint nhẹ không bị chặn** | `/health`, `/docs`, link tải file không đi qua hàng đợi. |
| **OCR offline chạy tuần tự** | Engine RapidOCR dùng chung được khoá 1 luồng tại một thời điểm — hai thread cùng gọi là crash native, mất cả process chứ không phải chỉ 1 request. |
| **Số thread ONNX** | Mặc định để ONNX Runtime dùng hết core — đúng vì OCR đã chạy tuần tự (đo trên máy 2 core: ép 1 thread làm mỗi ảnh chậm thêm ~60%, 21s so với 13s). Chỉ hạ bằng `MARKITDOWN_OCR_THREADS` khi máy còn chạy service quan trọng khác. |
| **Kết nối cloud VLM dùng lại** | Một `requests.Session` có connection pool cho cả process, kèm retry cho 429/5xx (tôn trọng `Retry-After`) — thay vì bắt tay TLS mới cho từng ảnh. |

Client nên chỉnh theo: gửi tuần tự hoặc song song vừa đủ, và **coi `503
server_busy` là "gửi lại sau"**, không phải "ảnh này hỏng".

Muốn tăng thông lượng khi máy còn dư CPU/RAM: nâng `MARKITDOWN_API_MAX_CONCURRENCY`
trong `.env` rồi restart. Với `/v1/bank` nút thắt nằm ở CPU của chính máy này, và
OCR đã bị khoá chạy tuần tự, nên tăng đồng thời chỉ giúp phần giải mã ảnh — muốn
nhanh hơn nữa thì hạ `MARKITDOWN_OCR_DET_MAX_SIDE` (xem mục 8).

### 7.3 Log & chẩn đoán khi service chết

| Nguồn | Có gì |
| ----- | ----- |
| Log ứng dụng (`make logs` / `make service-logs`) | Mỗi dòng có **thời điểm + mức log**. Mỗi request 1 dòng: `request=<id> POST /v1/bank -> 200 12345ms client=1.2.3.4 in_flight=2`. Lỗi 4xx/5xx được log kèm `error_code` và lý do — trước đây client nhận lỗi còn server không lưu gì. |
| Header `X-Request-ID` | Có trên mọi response. Client log lại id này là dò được đúng dòng log của server. |
| `<log>.crash` | Khi bật `MARKITDOWN_API_LOG_FILE`, `faulthandler` ghi traceback của **lỗi native** (segfault ONNX/OpenCV) vào file `<log_file>.crash` — loại lỗi mà bình thường không để lại dấu vết nào. |
| `kill -USR1 <pid>` | In stack của **mọi thread** đang chạy vào log — dùng khi server còn sống nhưng một request treo. |
| Cảnh báo bộ nhớ | Cứ `MARKITDOWN_API_MEMORY_CHECK_SECONDS` giây kiểm tra RSS; vượt `MARKITDOWN_API_MEMORY_WARN_MB` thì log WARNING. Bị OOM killer giết thì không log được gì, nên cảnh báo trước đó là dấu vết duy nhất. |
| journald (systemd) | Ghi lý do process thoát từ bên ngoài: `code=killed, signal=KILL` (OOM), `signal=SEGV` (lỗi native), `code=exited, status=1`. |

Bật ghi log ra file (khuyến nghị khi chạy nền):

```env
MARKITDOWN_API_LOG_FILE=/home/ubuntu/markitdown/.venv-api/markitdown-api.log
MARKITDOWN_API_LOG_MAX_MB=20
MARKITDOWN_API_LOG_BACKUPS=5
```

## 8. Model OCR offline

`/v1/bank` và `/v1/text` dùng RapidOCR + model PP-OCRv5 Latin chạy hoàn toàn
offline trên CPU.

Khâu **detect** (tìm vị trí chữ) chiếm 85–90% thời gian và chi phí tỉ lệ với số
pixel, nên nó chạy trên bản ảnh đã thu nhỏ (`MARKITDOWN_OCR_DET_MAX_SIDE`, mặc
định 960px cạnh dài). Khâu **recognize** (đọc chữ) vẫn cắt chữ từ ảnh gốc full
resolution, nên độ chính xác không giảm theo: đo trên bộ ảnh trong `test_img/`,
cả 20/20 trường vẫn đúng mà mỗi ảnh nhanh hơn ~3x. Ảnh chụp điện thoại nằm ngang
được tự động quay lại đúng chiều.

```bash
make models     # tải model, in ra đường dẫn cache
```

| Thông tin        | Giá trị                                                        |
| ---------------- | -------------------------------------------------------------- |
| Dung lượng       | ~92 MB (det + rec + dict)                                      |
| Nơi lưu          | `~/.cache/markitdown/ocr/ppocrv5-latin`                        |
| Đổi nơi lưu      | biến môi trường `MARKITDOWN_OCR_MODEL_DIR`                     |
| Tắt OCR offline  | `MARKITDOWN_DISABLE_LOCAL_OCR=1`                               |
| Cạnh dài khi detect | `MARKITDOWN_OCR_DET_MAX_SIDE` (mặc định `960`; hạ xuống `640` nhanh thêm ~1.9x, `0` = detect ở full resolution) |
| Số thread ONNX   | `MARKITDOWN_OCR_THREADS` (mặc định dùng hết core)               |
| Nguồn tải        | HuggingFace `monkt/paddleocr-onnx`                             |

Model được tải một lần rồi cache lại; nếu chưa tải trước, request đầu tiên sẽ tự
tải (chậm hơn). `make models` chỉ để làm sẵn bước đó.

> ⚠️ **Lưu ý quan trọng về `opencv-python`**: `rapidocr-onnxruntime` phụ thuộc
> `opencv-python`, thư viện này cần `libGL.so.1` — trên server headless (không có
> desktop) sẽ lỗi `ImportError: libGL.so.1` và OCR trả về rỗng **mà không báo lỗi**.
> Vì vậy `make setup-api` tự thay bằng `opencv-python-headless`. Nếu tự cài tay,
> hãy chạy: `pip uninstall opencv-python && pip install opencv-python-headless`
> (hoặc cài lib hệ thống: `sudo apt install -y libgl1 libglib2.0-0`).

Kiểm tra nhanh model đã hoạt động:

```bash
curl -s localhost:9890/health          # local_ocr_available phải là true
curl -s -X POST localhost:9890/v1/text -F "file=@test1.png"
```

---

## 9. Xử lý sự cố

| Hiện tượng                                                   | Nguyên nhân & cách xử lý                                                                         |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| `curl localhost:9890/health` không phản hồi                  | Server chưa chạy: `make status` để kiểm tra, `make start` để chạy, `make logs` để xem log         |
| **Đang chạy hàng loạt thì service tự stop, log không có gì** | Process bị giết từ bên ngoài (OOM killer) hoặc lỗi native → không kịp ghi log. Cài systemd ([mục 3.3](#33-chạy-dưới-systemd-khuyến-nghị-cho-production)) để tự chạy lại + `journalctl -u markitdown-api` xem `signal=KILL/SEGV`; bật `MARKITDOWN_API_LOG_FILE` để có file `.crash` và cảnh báo bộ nhớ |
| Client nhận `503 server_busy`                                | Đúng thiết kế: server đang đầy tải. Gửi lại sau `Retry-After` giây; muốn nhận nhiều hơn thì tăng `MARKITDOWN_API_MAX_CONCURRENCY` (xem [mục 7.2](#72-chịu-tải-hàng-loạt-ảnh)) |
| Client nhận `502` với body **HTML** (không parse được JSON)   | 502 đó do reverse proxy/Cloudflare sinh ra vì không gọi được server này — lỗi của server API luôn là JSON có `error_code`. Kiểm tra service còn sống không (`make status` / `make service-status`) |
| Một request treo rất lâu, chưa biết kẹt ở đâu                 | `kill -USR1 $(cat .venv-api/server.pid)` → stack của mọi thread được in vào log                    |
| `address already in use`                                      | Đã có process chiếm port: `make stop` (hoặc `pkill -f markitdown-api`), hoặc đổi `PORT` trong `.env` |
| `make status` báo `stopped` nhưng port vẫn có người nghe      | Có instance chạy ngoài pid file (ví dụ `make serve` ở terminal khác): `pkill -f markitdown-api`   |
| `/v1/text` trả file rỗng, `characters: 0`                     | Model OCR chưa tải hoặc lỗi `libGL` — chạy `make models`, xem cảnh báo ở [mục 8](#8-model-ocr-offline) |
| `/v1/bank` trả 6 trường rỗng                                  | Ảnh không phải biên lai chuyển tiền, hoặc OCR không đọc được chữ. Thử `POST /v1/text` để xem OCR đọc được gì |
| HTTP `502` với message về Cloud VLM                           | Sai/hết hạn `MARKITDOWN_VLM_API_KEY`, hoặc không kết nối được `MARKITDOWN_VLM_BASE_URL`           |
| HTTP `501`                                                    | Thiếu dependency cho định dạng đó — cài lại với `make setup-api API_EXTRAS=all,image-ocr`         |
| `download_url` trả về host nội bộ (docker, proxy)             | Đặt `MARKITDOWN_API_PUBLIC_URL=https://domain-cua-ban`                                            |
| Link tải trả `404`                                            | Link đã hết hạn theo `MARKITDOWN_API_FILE_TTL_MINUTES` — gọi lại endpoint để tạo file mới          |
| Gửi `url` bị `400` "non-public address"                       | Link trỏ tới localhost/LAN. Đúng như thiết kế; nếu thật sự cần thì `MARKITDOWN_API_ALLOW_PRIVATE_URLS=true` |
| Gửi `url` bị `415` dù đúng là ảnh                             | File thực tế không phải JPEG/PNG/WebP (ví dụ HEIC, SVG, hoặc link trả về trang HTML báo lỗi)       |
| Gửi `url` bị `502` "HTTP 403"                                 | Link cần quyền truy cập (private bucket / hết hạn signed URL) — server không có credential          |
| Mọi request trả `401`                                         | Thiếu/sai API key: kiểm tra `MARKITDOWN_API_KEYS` trong `.env` và header client gửi; `make restart` sau khi sửa `.env` |
| Đổi key trong `.env` mà client cũ vẫn phải chạy               | Khai báo 2 key: `MARKITDOWN_API_KEYS=key_cu,key_moi`, chuyển client xong thì bỏ key cũ              |
