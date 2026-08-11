.PHONY: init validate test reconcile status

init:
	PYTHONPATH=src python -m lca_project.cli init

validate:
	PYTHONPATH=src python -m lca_project.cli validate

test:
	pytest

reconcile:
	PYTHONPATH=src python -m lca_project.cli reconcile --once

status:
	PYTHONPATH=src python -m lca_project.cli status
