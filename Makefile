UV ?= $(shell command -v mise >/dev/null 2>&1 && echo "mise exec -- uv" || echo uv)
CSV ?= data/ma_transactions_500.csv
CONFIG ?= configs/default_target.yaml

.PHONY: install lint test check rank eval run app

install:
	$(UV) sync --frozen

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

test:
	$(UV) run --extra app pytest -q

check: lint test

rank:
	$(UV) run acquirer-id rank --csv $(CSV) --config $(CONFIG)

# Golden set 1 on the dev split. Holdout: make eval SPLIT=holdout -- run once, at the end.
SPLIT ?= dev
eval:
	$(UV) run acquirer-id backtest --csv $(CSV) --config $(CONFIG) --split $(SPLIT)

# End to end: rank, write 10 rationales (needs OPENAI_API_KEY), render output/<run_id>/report.md
run:
	$(UV) run acquirer-id run --csv $(CSV) --config $(CONFIG)

# Streamlit front end over the same runner. Installs the optional `app` extra on first use.
app:
	$(UV) run --extra app streamlit run src/acquirer_id/app.py
