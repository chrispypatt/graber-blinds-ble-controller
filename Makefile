PYTHON ?= python3

.PHONY: test check

test:
	$(PYTHON) -m pytest -q linux_controller/tests

check: test
	$(PYTHON) -m compileall -q linux_controller/graber linux_controller/tests
	sh -n deploy/install.sh
	git diff --check
