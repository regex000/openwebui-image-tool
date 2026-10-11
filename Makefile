.PHONY: help init up down logs ps test smoke e2e reset

COMPOSE ?= docker compose

help:
	@echo "make init   - create .env with generated secrets"
	@echo "make up     - build and start Open WebUI + the image tool"
	@echo "make down   - stop the stack (keeps data)"
	@echo "make reset  - stop the stack and DELETE its volumes"
	@echo "make logs   - follow logs"
	@echo "make test   - unit tests (in Docker)"
	@echo "make smoke  - curl checks against the running tool server"
	@echo "make e2e    - full Open WebUI -> tool-call -> image test with a mock LLM"

init:
	./scripts/init-env.sh

up:
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

reset:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

test:
	docker build --target test -t owui-image-tool:test ./image-tool
	docker run --rm owui-image-tool:test

smoke:
	./scripts/smoke-test.sh

e2e:
	./e2e/run.sh
