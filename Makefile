# Florida H3 Overview — build and deploy targets.
#
# Data under data/ is gitignored and never committed. deploy stages only the
# viewer (a static HTML/JS app) into dist/, ready to serve over HTTP from any
# static host (GitHub Pages, S3, a workstation, ...). The viewer must be served
# over HTTP: browsers block fetch() on file://.

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
	@echo "  make deploy   validate and stage a static deploy bundle in dist/"
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
	@rm -rf dist
	@mkdir -p dist
	cp viewer/florida.html dist/florida.html
	@echo "=== deploy bundle staged in dist/ ==="
	@echo "Serve it over HTTP (fetch is blocked on file://):"
	@echo "  cd dist && python -m http.server 8000"
	@echo "  open http://localhost:8000/florida.html?year=2017"
	@echo ""
	@echo "To publish to GitHub Pages, enable"
	@echo "  .github/workflows/pages.yml"
	@echo "or push the dist/ contents to the gh-pages branch manually."
	@echo ""
	@echo "Note: data/ is gitignored and local-only, so the deployed viewer shell"
	@echo "shows 'import a product first' until a catalog URL is supplied, e.g.:"
	@echo "  http://localhost:8000/florida.html?catalog=./catalog.json&year=2017"

# Alias kept so CI workflows that invoke `make publish` keep working.
publish: deploy

clean:
	rm -rf dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
