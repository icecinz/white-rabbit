PYTHON ?= python3

.PHONY: check-python check-install open demo test

check-python:
	@$(PYTHON) -c "import sys; sys.exit('Python 3.11+ is required. Set PYTHON=python3.12 if necessary.') if sys.version_info < (3, 11) else None"

demo: check-python
	$(PYTHON) -B demo.py

check-install: check-python
	$(PYTHON) -B 原型/install_local.py check --source-dir "$(CURDIR)/原型" $(if $(NATIVE_CONFIG),--native-config "$(NATIVE_CONFIG)")

open: check-python
	$(PYTHON) -B "$(HOME)/.imcodes/one-ai/manage.py" open

test: check-python
	$(PYTHON) -B -m unittest discover -s 原型 -p 'test_*.py'
