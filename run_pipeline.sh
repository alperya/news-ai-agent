#!/bin/bash
set -e

# Load environment
export $(cat .env | grep -v '^#' | xargs)

# Run pipeline
python3 main.py "$@"
