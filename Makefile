APP    := elegoo-print-recorder
LABEL  := io.github.shreyashguptas.$(APP)
LOG    := /Library/Logs/$(APP)/recorder.log
RUN    := uv run --quiet $(APP)

.PHONY: install check status logs run uninstall test deps

install: deps ## set up the printer and install the background recorder
	@uv sync --quiet
	@echo "Your Mac password is needed once (firewall + background service)."
	@sudo ./scripts/allow-firewall.sh "$$(uv run --quiet python -c 'import sys;print(sys.executable)')" \
	  "$$(command -v ffmpeg)" "$$(command -v ffprobe)"
	@$(RUN) setup --write .setup-config.json
	@sudo UV="$$(command -v uv)" ./scripts/install-service.sh "$(CURDIR)" .setup-config.json
	@rm -f .setup-config.json
	@$(RUN) check

check: ## check the camera feed
	@$(RUN) check

status: ## show the print and recording status
	@$(RUN) status

logs: ## follow the recorder's log
	@tail -n 30 -f $(LOG)

run: deps ## record in this window instead of in the background
	@if launchctl print system/$(LABEL) >/dev/null 2>&1; then \
	  echo "The background recorder is installed and already running. See: make status"; exit 1; fi
	@$(RUN) setup --write .setup-config.json && $(RUN) --config .setup-config.json -v run

uninstall: ## remove the background recorder (keeps your recordings)
	@sudo ./scripts/uninstall-service.sh

test:
	@uv run pytest -q

deps:
	@command -v uv >/dev/null || { echo "Install uv first:  brew install uv"; exit 1; }
	@command -v ffmpeg >/dev/null || { echo "Install ffmpeg first:  brew install ffmpeg"; exit 1; }
