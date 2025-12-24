.PHONY: build build-scheduler run run-scheduler test clean help

help:
	@echo "PA Power & Gas Rate Notifier - Makefile Commands"
	@echo ""
	@echo "  make build          - Build the main Docker image"
	@echo "  make build-scheduler - Build the scheduler Docker image"
	@echo "  make run            - Run a one-time check (requires .env file)"
	@echo "  make run-scheduler  - Run with periodic scheduling (requires .env file)"
	@echo "  make test           - Test the script locally (requires Python)"
	@echo "  make clean          - Clean up data directory"
	@echo "  make help           - Show this help message"

build:
	docker build -t pa-power-notifier .

build-scheduler:
	docker build -f Dockerfile.scheduler -t pa-power-notifier-scheduler .

run:
	docker-compose up

run-scheduler:
	docker run -d \
		--name pa-power-notifier \
		--env-file .env \
		-v $(PWD)/data:/data \
		pa-power-notifier-scheduler

test:
	python3 -m pip install -r requirements.txt
	python3 pa_power_notifier.py

clean:
	rm -rf data/*.json data/*.log

