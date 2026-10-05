# Build the markitdown CLI and install it into ~/.local/bin, and run the HTTP API.
#
# `make install` installs the built wheel, `make dev` installs the working tree
# in editable mode so source edits take effect without reinstalling. Both
# recreate the pipx venv, which re-downloads the extras listed in EXTRAS.
#
# `make serve` runs the API server in the foreground; `make start|stop|restart|
# status|logs` manage it as a background process tracked by a pid file. Both do
# the one-time setup (venv, deps, offline OCR models) on first run.

PACKAGE_DIR := packages/markitdown
API_DIR     := packages/markitdown-api
DIST_DIR    := dist
PIPX        ?= pipx
UV          ?= uv
VENV_PYTHON := $(HOME)/.local/share/pipx/venvs/markitdown/bin/python

# The API server runs from a project-local venv rather than pipx, so `make serve`
# works on a machine that only has uv.
API_VENV    := .venv-api
API_PYTHON  := $(API_VENV)/bin/python
API_BIN     := $(API_VENV)/bin/markitdown-api
API_PID     ?= $(API_VENV)/server.pid
API_LOG     ?= $(API_VENV)/server.log

# Health URL of the background server, resolved from its own config (.env) when a
# recipe runs — not at parse time, since the venv may not exist yet.
API_HEALTH_URL = $$($(API_PYTHON) -c 'from markitdown_api import load_config; c = load_config(); h = "127.0.0.1" if c.host in ("0.0.0.0", "::") else c.host; print("http://%s:%d/health" % (h, c.port))' 2>/dev/null)

# Expand to the pid of the background server, or nothing when it is not running.
API_RUNNING_PID = $$(pid=$$(cat $(API_PID) 2>/dev/null); kill -0 $$pid 2>/dev/null && echo $$pid)

# Extras installed alongside the CLI. Override per invocation, e.g.
#   make install EXTRAS=all,image-ocr
EXTRAS ?= all,image-ocr,image-caption

# Extras for the API venv. `image-caption` is left out on purpose: it pulls torch
# (~2 GB) and the server describes images through the cloud VLM anyway.
API_EXTRAS ?= all,image-ocr

# Supervised deployment: same server, run by systemd so a crash or an
# out-of-memory kill is restarted and recorded instead of leaving the service
# down with an empty log. MEMORY_MAX caps the service, not the machine.
SERVICE_NAME    ?= markitdown-api
SERVICE_UNIT    := /etc/systemd/system/$(SERVICE_NAME).service
SERVICE_MEMORY_MAX ?= 2G

# rapidocr depends on opencv-python, whose cv2 links libGL.so.1 at import time and
# fails on any machine without a desktop stack. opencv-python-headless is the same
# module without that link, but both wheels write the same cv2/ directory, so the
# swap only sticks when it runs after the install — declaring it as a dependency
# leaves whichever wheel unpacked last in place. $(1) is the venv's python.
define use_headless_cv2
	@if $(UV) pip list --python $(1) 2>/dev/null | grep -q '^opencv-python[[:space:]]'; then \
		echo "Replacing opencv-python with opencv-python-headless (no libGL needed)"; \
		$(UV) pip uninstall --python $(1) opencv-python >/dev/null; \
		$(UV) pip install --python $(1) --quiet --force-reinstall opencv-python-headless; \
	fi
endef

.DEFAULT_GOAL := help
.PHONY: help build install dev uninstall test clean where serve start stop restart status logs setup-api models test-api install-service uninstall-service service-status service-logs

help:  ## List the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  EXTRAS = $(EXTRAS)"

build:  ## Build the wheel and sdist into dist/
	@rm -rf $(DIST_DIR)
	@if command -v uv >/dev/null 2>&1; then \
		uv build --out-dir $(DIST_DIR) $(PACKAGE_DIR); \
	else \
		$(PIPX) run build --outdir $(DIST_DIR) $(PACKAGE_DIR); \
	fi

install: build  ## Build, then install the wheel as the `markitdown` command
	@wheel=$$(ls $(DIST_DIR)/*.whl | head -1); \
	echo "Installing $$wheel with extras [$(EXTRAS)]"; \
	$(PIPX) install --force "$$wheel[$(EXTRAS)]"
	$(call use_headless_cv2,$(VENV_PYTHON))
	@$(MAKE) --no-print-directory where

dev:  ## Install the working tree in editable mode (source edits apply live)
	$(PIPX) install --force --editable "$(PACKAGE_DIR)[$(EXTRAS)]"
	$(call use_headless_cv2,$(VENV_PYTHON))
	@$(MAKE) --no-print-directory where

serve: setup-api  ## Run the HTTP API server in the foreground (installs on first run)
	$(API_BIN) $(ARGS)

start: setup-api  ## Start the server in the background and wait until it answers
	@if systemctl is-active --quiet $(SERVICE_NAME) 2>/dev/null; then \
		echo "$(SERVICE_NAME) is already running under systemd — use 'sudo systemctl restart $(SERVICE_NAME)'."; \
		echo "Starting a second copy here would only fight it for the port."; \
		exit 1; \
	fi; \
	if [ -n "$(API_RUNNING_PID)" ]; then \
		echo "markitdown-api is already running (pid $$(cat $(API_PID)))"; \
		$(MAKE) --no-print-directory status; \
		exit 0; \
	fi; \
	url=$(API_HEALTH_URL); \
	if curl -sf -m 2 "$$url" >/dev/null 2>&1; then \
		echo "Something already answers on $$url — an untracked server (make serve?) or another service."; \
		echo "Free the port or change PORT in .env, then try again."; \
		exit 1; \
	fi; \
	nohup $(API_BIN) $(ARGS) >> $(API_LOG) 2>&1 & \
	pid=$$!; \
	echo $$pid > $(API_PID); \
	printf "Starting markitdown-api"; \
	for i in $$(seq 1 40); do \
		if ! kill -0 $$pid 2>/dev/null; then \
			echo; echo "markitdown-api exited during startup. Last log lines:"; \
			tail -5 $(API_LOG); rm -f $(API_PID); exit 1; \
		fi; \
		curl -sf -m 1 "$$url" >/dev/null 2>&1 && break; \
		printf "."; sleep 0.5; \
	done; \
	echo; \
	$(MAKE) --no-print-directory status

stop:  ## Stop the background server
	@pid=$(API_RUNNING_PID); \
	if [ -n "$$pid" ]; then \
		kill $$pid; \
		for i in $$(seq 1 20); do kill -0 $$pid 2>/dev/null || break; sleep 0.5; done; \
		kill -9 $$pid 2>/dev/null || true; \
		echo "markitdown-api stopped (pid $$pid)"; \
	else \
		echo "markitdown-api is not running"; \
	fi
	@rm -f $(API_PID)

restart:  ## Restart the background server
	@$(MAKE) --no-print-directory stop
	@$(MAKE) --no-print-directory start

status:  ## Show whether the background server is running, and its health response
	@pid=$(API_RUNNING_PID); \
	if [ -z "$$pid" ]; then echo "markitdown-api: stopped"; exit 0; fi; \
	url=$(API_HEALTH_URL); \
	echo "markitdown-api: running (pid $$pid)"; \
	echo "  url:    $${url%/health}"; \
	echo "  docs:   $${url%/health}/docs"; \
	echo "  log:    $(API_LOG)"; \
	printf "  health: "; \
	curl -sf -m 3 "$$url" || echo "no response (see $(API_LOG))"; \
	echo

logs:  ## Follow the background server log
	@tail -f $(API_LOG)

install-service: setup-api  ## Install and start the systemd unit (auto-restart on crash)
	@if [ -n "$(API_RUNNING_PID)" ]; then \
		echo "Stopping the nohup-managed server first so both do not fight for the port."; \
		$(MAKE) --no-print-directory stop; \
	fi
	@sed -e 's|__ROOT__|$(CURDIR)|g' \
		-e 's|__USER__|$(shell id -un)|g' \
		-e 's|__MEMORY_MAX__|$(SERVICE_MEMORY_MAX)|g' \
		deploy/markitdown-api.service | sudo tee $(SERVICE_UNIT) >/dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable --now $(SERVICE_NAME)
	@$(MAKE) --no-print-directory service-status

uninstall-service:  ## Stop and remove the systemd unit
	-sudo systemctl disable --now $(SERVICE_NAME)
	-sudo rm -f $(SERVICE_UNIT)
	sudo systemctl daemon-reload

service-status:  ## Show the systemd unit's state, including why it last exited
	@systemctl --no-pager --lines=0 status $(SERVICE_NAME) || true
	@url=$(API_HEALTH_URL); printf "  health: "; \
		curl -sf -m 3 "$$url" || echo "no response (see 'make service-logs')"; \
		echo

service-logs:  ## Follow the systemd unit's log (restarts and exit signals included)
	@journalctl -u $(SERVICE_NAME) -f -n 200

setup-api: $(API_BIN)  ## Create .venv-api, install the API server and fetch the offline models

$(API_BIN):
	$(UV) venv $(API_VENV)
	@# httpx is what starlette's TestClient runs on, so `make test-api` works too.
	VIRTUAL_ENV=$(API_VENV) $(UV) pip install -e "$(PACKAGE_DIR)[$(API_EXTRAS)]" \
		fastapi "uvicorn[standard]" python-multipart pytest httpx
	VIRTUAL_ENV=$(API_VENV) $(UV) pip install --no-deps -e "$(API_DIR)"
	$(call use_headless_cv2,$(API_PYTHON))
	@$(MAKE) --no-print-directory models

models: | $(API_BIN)  ## Download the offline OCR models into ~/.cache/markitdown
	@$(API_PYTHON) -c "from markitdown.converters._local_ocr import download_models; \
		print('Offline OCR models ready in', download_models())"

test-api: | $(API_BIN)  ## Run the API test suite
	@$(API_PYTHON) -m pytest $(API_DIR)/tests

uninstall:  ## Remove the installed CLI
	$(PIPX) uninstall markitdown

test:  ## Run the test suite with the installed CLI's interpreter
	@test -x $(VENV_PYTHON) || { echo "markitdown is not installed; run 'make dev' first"; exit 1; }
	@if $(VENV_PYTHON) -c "import pytest" >/dev/null 2>&1; then \
		cd $(PACKAGE_DIR) && $(VENV_PYTHON) -m pytest tests; \
	else \
		echo "pytest is not installed in the CLI venv; running the standalone tests"; \
		cd $(PACKAGE_DIR) && $(VENV_PYTHON) tests/test_bank_transfer.py; \
	fi

where:  ## Show the installed CLI path and version
	@command -v markitdown >/dev/null 2>&1 \
		&& { echo "markitdown -> $$(command -v markitdown)"; markitdown --version; } \
		|| echo "markitdown is not on PATH"

clean:  ## Remove build artifacts and the API venv
	rm -rf $(DIST_DIR) $(PACKAGE_DIR)/build $(PACKAGE_DIR)/*.egg-info $(API_VENV)
	find $(PACKAGE_DIR) -name __pycache__ -type d -prune -exec rm -rf {} +
