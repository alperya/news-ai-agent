.PHONY: help install test run deploy clean

help:
	@echo "Available commands:"
	@echo "  make install    - Install with pip3"
	@echo "  make test       - Run tests"
	@echo "  make run        - Run pipeline (dry-run)"
	@echo "  make run-live   - Run with posting"
	@echo "  make clean      - Clean files"

install:
	python3 -m pip install --upgrade pip
	pip3 install -r requirements.txt
	@echo "✅ Installed"

test:
	pytest tests/ -v

run:
	@echo "🚀 Running (DRY RUN)..."
	python3 main.py --dry-run --max-posts 5

run-live:
	@echo "⚠️  WARNING: This will ACTUALLY POST to social media!"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read line
	@bash -c 'export $$(cat .env | grep -v "^\#" | xargs) && python3 main.py --no-dry-run --max-posts 3'

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "🧹 Cleaned"

deploy:
	@chmod +x deploy.sh
	@./deploy.sh
