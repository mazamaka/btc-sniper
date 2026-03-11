#!/bin/bash
# BTC Sniper — start/restart script
# Preserves portfolio data across restarts

cd "$(dirname "$0")"

# Kill existing process on port 8877
fuser -k 8877/tcp 2>/dev/null
sleep 1

# Activate venv and start
source .venv/bin/activate
nohup python main.py --web > /tmp/btc-sniper.log 2>&1 &

sleep 3
echo "BTC Sniper started (PID: $!)"
tail -5 /tmp/btc-sniper.log
