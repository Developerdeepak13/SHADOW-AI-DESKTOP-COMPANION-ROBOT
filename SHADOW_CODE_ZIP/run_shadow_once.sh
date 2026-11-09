#!/bin/bash
set -u
cd "$(dirname "$0")"
source .venv/bin/activate
exec python main.py
