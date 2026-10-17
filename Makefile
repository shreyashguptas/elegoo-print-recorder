APP    := elegoo-print-recorder
RUN    := uv run --quiet $(APP)
UNAME  := $(shell uname)

.PHONY: install check status logs run uninstall test deps nas

ifeq ($(UNAME),Darwin)
LABEL  := io.github.shreyashguptas.$(APP)
LOG    := /Library/Logs/$(APP)/recorder.log

install: deps ## set up the printer and install the background recorder
	@uv sync --quiet
	@echo "Your Mac password is needed once (firewall + background service)."
	@sudo ./scripts/allow-firewall.sh "$$(uv run --quiet python -c 'import sys;print(sys.executable)')" \
	  "$$(command -v ffmpeg)" "$$(command -v ffprobe)"
	@$(RUN) setup --write .setup-config.json
	@sudo UV="$$(command -v uv)" ./scripts/install-service.sh "$(CURDIR)" .setup-config.json
	@rm -f .setup-config.json
	@$(RUN) check

logs: ## follow the recorder's log
	@tail -n 30 -f $(LOG)

uninstall: ## remove the background recorder (keeps your recordings)
	@sudo ./scripts/uninstall-service.sh

running = launchctl print system/$(LABEL) >/dev/null 2>&1
else
install: deps ## set up the printer and install the recorder as a systemd service
	@uv sync --quiet
	@$(RUN) setup --write .setup-config.json $(if $(OUTPUT),--output-dir "$(OUTPUT)")
	@sudo UV="$$(command -v uv)" ./scripts/install-linux.sh "$(CURDIR)" .setup-config.json "$$(id -un)"
	@rm -f .setup-config.json
	@$(RUN) check

logs: ## follow the recorder's log
	@journalctl -u $(APP) -n 30 -f

uninstall: ## remove the service (keeps your recordings)
	@sudo ./scripts/uninstall-linux.sh

nas: ## mount a NAS share: make nas SHARE=//192.168.1.10/recordings NAS_USER=me
	@test -n "$(SHARE)" -a -n "$(NAS_USER)" || { echo 'Usage: make nas SHARE=//NAS-IP/share NAS_USER=user'; exit 1; }
	@sudo ./scripts/mount-nas-linux.sh "$(SHARE)" "$(NAS_USER)" "$$(id -un)"

running = systemctl is-active --quiet $(APP)
endif

check: ## check the camera feed
	@$(RUN) check

status: ## show the print and recording status
	@$(RUN) status

run: deps ## record in this window instead of in the background
	@if $(running); then echo "The background recorder is already running. See: make status"; exit 1; fi
	@$(RUN) setup --write .setup-config.json && $(RUN) --config .setup-config.json -v run

test:
	@uv run pytest -q

deps:
	@command -v uv >/dev/null || { echo "Install uv first:  $(if $(filter Darwin,$(UNAME)),brew install uv,curl -LsSf https://astral.sh/uv/install.sh | sh)"; exit 1; }
	@command -v ffmpeg >/dev/null || { echo "Install ffmpeg first:  $(if $(filter Darwin,$(UNAME)),brew install ffmpeg,sudo apt install ffmpeg)"; exit 1; }
