.PHONY: build run run-once venv browser-install gas-plan-check electric-plan-check test clean help

help:
	@echo "PA Power & Gas Rate Notifier"
	@echo ""
	@echo "  make build              - Build the Docker image"
	@echo "  make run                - Run continuously (CHECK_INTERVAL_HOURS, default 24h)"
	@echo "  make run-once           - Single check then exit"
	@echo "  make venv               - Create .venv and install requirements (recommended on macOS)"
	@echo "  make browser-install    - npm install + Chrome for browser/ (Puppeteer, for Duquesne pages)"
	@echo "  make gas-plan-check     - Peoples Gas forecast + PA Gas Switch plans (needs credentials)"
	@echo "  make electric-plan-check - PA Power Switch plans ranked by your usage (see script header)"
	@echo "  make test               - Run notifier once locally (uses .venv)"
	@echo "  make clean              - Remove build artifacts (never touches data/)"

build:
	docker build -t pa-power-notifier .

run:
	docker compose up

run-once:
	CHECK_INTERVAL_HOURS=0 docker compose run --rm pa-power-notifier

venv:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

browser-install:
	cd browser && npm install && npx puppeteer browsers install chrome

gas-plan-check:
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	.venv/bin/python gas_plan_compare.py

electric-plan-check:
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	.venv/bin/python electric_plan_compare.py

test:
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	CHECK_INTERVAL_HOURS=0 .venv/bin/python pa_power_notifier.py

# data/ holds merge-only history (usage, bills, market snapshots) that utilities drop
# over time — it is deliberately not cleaned.
clean:
	rm -rf __pycache__ browser/dist
