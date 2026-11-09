#!/bin/bash
# SHADOW supervisor: restart the Python process after a native crash (SIGSEGV = 139)
# or an unexpected non-zero exit. Stop cleanly with Ctrl+C twice.
set -u
cd "$(dirname "$0")"

while true; do
    echo "🚀 Starting SHADOW supervisor..."
    ./run_shadow_once.sh
    code=$?

    if [ "$code" -eq 0 ]; then
        echo "👋 SHADOW exited normally."
        exit 0
    fi

    if [ "$code" -eq 130 ]; then
        echo "🛑 SHADOW stopped by user."
        exit 130
    fi

    echo "⚠️ SHADOW exited with code $code. Restarting in 2 seconds..."
    sleep 2
done
