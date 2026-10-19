#!/bin/bash
cd "$(dirname "$0")"
# 检查是否已在运行
if pgrep -f "server.py" > /dev/null; then
    echo "Web server is already running."
    exit 0
fi
nohup python3 server.py > web.log 2>&1 &
echo "Web server started on port 8765, PID: $!"
