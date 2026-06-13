.PHONY: run dry list
.DEFAULT_GOAL := run

LOG := $(HOME)/.cc-autoresume.log

run:
	@if command -v lnav >/dev/null 2>&1; then \
		./cc-autoresume.py 2>&1 | tee -a "$(LOG)" | lnav; \
	else \
		echo "lnav not installed; logging to $(LOG) (Ctrl-C to stop)"; \
		./cc-autoresume.py 2>&1 | tee -a "$(LOG)"; \
	fi

dry:
	./cc-autoresume.py --dry-run -v

list:
	./cc-autoresume.py --list
