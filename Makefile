# Pipeline: data -> build -> test -> analyze -> report. Everything runs single-threaded and
# niced, because the dev laptop overheats. COOLDOWN pauses between heavy steps (0 in CI).
SHELL    := /bin/bash
PY       := .venv/bin/python
RUN      := PYTHONPATH=. $(if $(filter Darwin,$(shell uname)),taskpolicy -b nice -n 19,nice -n 19)
CXX      ?= c++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -pedantic
COOLDOWN ?= 60
SEED     ?= 42

SYNTH   := data/synth
DECODED := results/decoded

.PHONY: all venv data synth real build test analyze report seeds clean cooldown

all: data build test analyze report

venv: .venv/.ok
.venv/.ok: requirements.txt
	uv venv -q --python 3.12 .venv 2>/dev/null || python3 -m venv .venv
	uv pip install -q --python $(PY) -r requirements.txt 2>/dev/null || $(PY) -m pip install -q -r requirements.txt
	touch $@

data: synth real

synth: $(SYNTH)/can_log.txt
$(SYNTH)/can_log.txt: scripts/gen_can.py fh/can.py config/signals.csv | venv
	$(RUN) $(PY) scripts/gen_can.py --seed $(SEED) --out $(SYNTH)

# One FastF1 session, cached in data/cache. Needs network the first time only.
real: data/real/car_data.csv
data/real/car_data.csv: scripts/fetch_fastf1.py | venv
	$(RUN) $(PY) scripts/fetch_fastf1.py

build: build/can_decode build/test_can
build/can_decode: decoder/main.cpp decoder/can.cpp decoder/can.hpp
	@mkdir -p build
	$(RUN) $(CXX) $(CXXFLAGS) decoder/main.cpp decoder/can.cpp -o $@
build/test_can: tests/test_can.cpp decoder/can.cpp decoder/can.hpp
	@mkdir -p build
	$(RUN) $(CXX) $(CXXFLAGS) tests/test_can.cpp decoder/can.cpp -o $@

test: build | venv
	./build/test_can
	$(RUN) $(PY) tests/test_python.py

$(DECODED)/decoded.csv: build/can_decode $(SYNTH)/can_log.txt config/signals.csv
	@mkdir -p $(DECODED)
	$(RUN) ./build/can_decode $(SYNTH)/can_log.txt config/signals.csv $(DECODED)

analyze: $(DECODED)/decoded.csv
	$(RUN) $(PY) scripts/analyze.py --synth $(SYNTH) --decoded $(DECODED) --out results
	@$(MAKE) --no-print-directory cooldown
	$(RUN) $(PY) scripts/real_checks.py --out results

report: | venv
	$(RUN) $(PY) scripts/report.py --results results --out reports

# Re-run generator + decoder + analysis on other seeds, to check the headline numbers aren't luck.
SEEDS ?= 1 2
seeds: build/can_decode | venv
	@for s in $(SEEDS); do \
	  $(MAKE) --no-print-directory cooldown; \
	  $(RUN) $(PY) scripts/gen_can.py --seed $$s --out data/seed$$s > /dev/null && \
	  mkdir -p results/seed$$s/decoded && \
	  $(RUN) ./build/can_decode data/seed$$s/can_log.txt config/signals.csv results/seed$$s/decoded > /dev/null && \
	  echo "== seed $$s" && $(RUN) $(PY) scripts/analyze.py --synth data/seed$$s --decoded results/seed$$s/decoded \
	    --out results/seed$$s | sed -n '1,13p;/injected rule/p;/  found:/p' || exit 1; \
	done

cooldown:
	@if [ "$(COOLDOWN)" != "0" ]; then echo "cooling down $(COOLDOWN)s"; sleep $(COOLDOWN); fi

# Keeps data/cache (FastF1 downloads) so a rebuild does not hit the network again.
clean:
	rm -rf build results $(SYNTH) data/real data/seed*
