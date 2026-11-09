#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if curl -fsS --max-time 2 http://127.0.0.1:8765/api/state >/dev/null 2>&1; then
    open http://127.0.0.1:8765
    exit 0
fi
sleepwell_python=""
for candidate in .venv/bin/python /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 /usr/bin/python3; do
    if [ -x "$candidate" ] && "$candidate" -c 'import pandas, sklearn, pyarrow, lightgbm' >/dev/null 2>&1; then
        sleepwell_python="$candidate"
        break
    fi
done
if [ -z "$sleepwell_python" ]; then
    printf 'Python 환경을 먼저 준비하세요: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n'
    read -r -p 'Enter 키를 누르면 닫힙니다.'
    exit 1
fi
mkdir -p .local
nohup "$sleepwell_python" server.py > .local/server.log 2>&1 < /dev/null &
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 1 http://127.0.0.1:8765/api/state >/dev/null 2>&1; then
        open http://127.0.0.1:8765
        exit 0
    fi
    sleep 1
done
printf '서버 시작을 확인하지 못했습니다. .local/server.log를 확인하세요.\n'
