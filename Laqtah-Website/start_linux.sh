#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
command -v ffmpeg >/dev/null || echo 'NOTE: ffmpeg is required for high-quality merging.'
command -v deno >/dev/null || echo 'NOTE: Deno 2.3+ is recommended for YouTube.'
exec python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
