# Florida H3 Overview — build and deploy targets.
#
# Data under data/ is gitignored and never committed to main. deploy validates
# the repo, stages the viewer and the demo product into dist/, then pushes both
# to the gh-pages branch for GitHub Pages. The viewer must be served over HTTP:
# browsers block fetch() on file://.

PY ?= /home/tylar/miniconda3/bin/python
NODE ?= node
VIEWER_JS := /tmp/florida-viewer-check.js

.PHONY: help deploy publish test lint check serve clean fmt

help:
	@echo "Florida H3 Overview — targets:"
	@echo "  make help     show this help"
	@echo "  make check    run all validation (lint + test + viewer JS syntax)"
	@echo "  make test     run the pytest suite"
	@echo "  make lint     byte-compile every Python module"
	@echo "  make serve    serve the repo over HTTP for local viewer testing"
	@echo "  make deploy   validate and publish the viewer + demo data to the gh-pages branch"
	@echo "  make clean    remove dist/ and __pycache__"

check: lint test
	@$(MAKE) check-viewer-js

lint:
	$(PY) -m py_compile src/florida_h3/*.py tools/*.py
	@echo "lint: all Python modules compile"

test:
	PYTHONPATH=src $(PY) -m pytest tests/ -q

check-viewer-js:
	$(PY) -c "import re,pathlib; html=pathlib.Path('viewer/florida.html').read_text(); body=re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)[1][1]; pathlib.Path('$(VIEWER_JS)').write_text(body)"
	$(NODE) --check $(VIEWER_JS)
	@rm -f $(VIEWER_JS)
	@echo "lint: viewer JavaScript parses"

serve:
	@echo "serving http://127.0.0.1:8000/viewer/florida.html?year=2017"
	$(PY) -m http.server 8000 --bind 127.0.0.1

deploy: check
	@set -e; \
	bundle=$$(mktemp -d /tmp/florida-bundle.XXXXXX); \
	wt=$$(mktemp -d /tmp/florida-worktree.XXXXXX); rmdir "$$wt"; \
	cleanup() { git worktree remove --force "$$wt" >/dev/null 2>&1 || true; rm -rf "$$bundle" "$$wt"; }; \
	trap cleanup EXIT; \
	echo "=== deploy: staging viewer + data ==="; \
	mkdir -p "$$bundle/data/florida"; \
	cp viewer/florida.html "$$bundle/florida.html"; \
	( cd data/florida && tar cf - --exclude=./cache . ) | ( cd "$$bundle/data/florida" && tar xf - ); \
	touch "$$bundle/.nojekyll"; \
	git fetch origin 2>/dev/null || true; \
	git worktree add --detach "$$wt" HEAD >/dev/null; \
	if git rev-parse --verify origin/gh-pages >/dev/null 2>&1; then \
		git -C "$$wt" checkout -B gh-pages origin/gh-pages >/dev/null; \
	else \
		git -C "$$wt" checkout --orphan gh-pages >/dev/null; \
	fi; \
	git -C "$$wt" rm -rf . --ignore-unmatch >/dev/null 2>&1 || true; \
	git -C "$$wt" clean -fdx >/dev/null; \
	cp -r "$$bundle"/. "$$wt"/; \
	git -C "$$wt" add -A; \
	git -C "$$wt" commit -q -m "Deploy viewer + demo data $$(date -u +%Y-%m-%dT%H:%M:%SZ) [skip ci]"; \
	git -C "$$wt" push origin gh-pages; \
	echo "=== deployed to gh-pages ==="
	@echo "Open it: https://tylar.info/florida-h3-overview/florida.html?year=2017"
	@echo ""
	@echo "Note: data/ is gitignored locally but the demo product is deployed"
	@echo "to gh-pages with the viewer, so the catalog loads automatically."
	@echo ""
	@echo "For local testing, open:"
	@echo "  http://localhost:8000/viewer/florida.html?catalog=../data/florida/catalog.json&year=2017"
	@echo ""
	@echo "One-time: set GitHub Pages source to 'Deploy from a branch' -> gh-pages"
	@echo "in repository settings (Pages tab). After that, 'make deploy' is all you need."

# Alias kept so CI workflows that invoke `make publish` keep working.
publish: deploy

clean:
	rm -rf dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
