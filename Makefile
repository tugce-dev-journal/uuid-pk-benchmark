# Convenience wrapper. Plain commands are in the README if you don't use make.

.PHONY: up down run install reset logs psql

up:            ## start Postgres in the background
	docker compose up -d
	@echo "Waiting for Postgres to be healthy..."
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' uuid-pk-benchmark-db 2>/dev/null)" = "healthy" ]; do sleep 1; done
	@echo "Postgres is ready on localhost:5433"

install:       ## install the python dependency
	pip install -r requirements.txt

run:           ## run the benchmark (override e.g. make run ROWS=5000000)
	python benchmark.py

reset:         ## wipe the database volume for a clean re-run
	docker compose down -v

down:          ## stop the container (keeps the data volume)
	docker compose down

logs:          ## tail Postgres logs
	docker compose logs -f db

psql:          ## open a psql shell inside the container
	docker exec -it uuid-pk-benchmark-db psql -U postgres -d uuidbench
