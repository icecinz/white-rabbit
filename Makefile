PYTHON ?= python3

.PHONY: check-python demo test

check-python:
	@$(PYTHON) -c "import sys; sys.exit('Python 3.11+ is required. Set PYTHON=python3.12 if necessary.') if sys.version_info < (3, 11) else None"

demo: check-python
	$(PYTHON) -B demo.py

test: check-python
	$(PYTHON) -B -m unittest discover -s 原型 -p 'test_*.py'
