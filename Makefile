.PHONY: up verify test report simulate down

up:        ## start the seeded portal on :8080
	docker compose up --build

verify:    ## clean rebuild + run.py + isolation probes (+ tests if .venv exists)
	sh scripts/verify.sh

test:      ## run the pytest suite locally
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt && .venv/bin/pytest -q

report:    ## raw vs bias-corrected results for the fixture event
	docker compose exec portal python manage.py normalization_report

simulate:  ## ground-truth recovery simulation (about a minute)
	docker compose exec portal python manage.py normalization_simulation

down:
	docker compose down -v
