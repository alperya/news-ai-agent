#!/bin/bash
set -e

# Always run from project root
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# Load environment
export $(cat .env | grep -v '^#' | xargs)

# Run pipeline
python3 main.py "$@"
