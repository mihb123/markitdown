# markitdown-api

HTTP API server for [MarkItDown](https://github.com/microsoft/markitdown). Each CLI
feature is exposed as its own endpoint:

| Endpoint            | CLI equivalent    | Response                        |
| ------------------- | ----------------- | ------------------------------- |
| `POST /v1/bank`     | `markitdown -b`   | receipt fields as JSON          |
| `POST /v1/text`     | `markitdown -t`   | link to the extracted text file |
| `POST /v1/describe` | `markitdown -d`   | link to the description file    |
| `POST /v1/documents`| `markitdown FILE` | link to the markdown file       |
| `GET /v1/files/{id}`| —                 | downloads a generated file      |

Install and run with one command — the first run creates `.venv-api`, installs the
dependencies and downloads the offline OCR models, then starts the server on
`$PORT` (default 9890):

```bash
make start      # background; also: make stop | restart | status | logs
make serve      # foreground
```

Full usage guide, request/response examples and configuration keys:
[`docs/API.md`](../../docs/API.md). Interactive docs are served at `/docs`.
