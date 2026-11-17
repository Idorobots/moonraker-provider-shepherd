PYTHON ?= python3
GUILE ?= guile

.PHONY: check check-python check-helper check-integration
check: check-python check-helper check-integration

check-python:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

check-helper:
	GUILE_AUTO_COMPILE=0 $(GUILE) -L scheme tests/helper.scm

check-integration:
	$(PYTHON) tests/integration.py
