.PHONY: help setup dev-api dev-web ingest test test-api test-web evals up down logs diagram

help:
	@echo "make setup     - create backend venv + install frontend deps"
	@echo "make dev-api   - run the API locally with reload (http://localhost:8000)"
	@echo "make dev-web   - run the React dev server (http://localhost:5173, proxies /api)"
	@echo "make ingest    - (re)build the corpus index into var/index.db"
	@echo "make test      - backend + frontend tests (no network)"
	@echo "make evals     - run the golden + authorial evaluation (needs LLM_API_KEY in .env)"
	@echo "make up/down   - docker compose up --build / down"
	@echo "make diagram   - render docs/architecture.dot to svg/png (needs graphviz)"

setup:
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
	cd frontend && npm install

dev-api:
	cd backend && . .venv/bin/activate && uvicorn app.main:app --reload --port 8000

dev-web:
	cd frontend && npm run dev

ingest:
	cd backend && . .venv/bin/activate && python -m app.knowledge.ingest --force

test: test-api test-web

test-api:
	cd backend && . .venv/bin/activate && python -m pytest -q

test-web:
	cd frontend && npm test -- --run

evals:
	cd backend && . .venv/bin/activate && python -m scripts.run_evals $(ARGS)

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f api

diagram:
	dot -Tsvg docs/architecture.dot -o docs/architecture.svg && dot -Tpng docs/architecture.dot -o docs/architecture.png
