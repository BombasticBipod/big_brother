#!/usr/bin/env bash
# Pull the builder model. Starts Ollama only if it is not already up, and stops
# only a server this script started. Progress prints live and goes to logs/.
set -euo pipefail
MODEL="${1:-qwen2.5-coder:3b}"
cd "$(dirname "$0")/.."
mkdir -p logs
LOG="logs/pull_model_$(date +%Y%m%d_%H%M%S).log"
started=""
if ! curl -sf http://127.0.0.1:11434/api/version >/dev/null; then
  ollama serve >>"$LOG" 2>&1 &
  started=$!
  trap '[ -n "$started" ] && kill "$started"' EXIT
  for _ in $(seq 30); do curl -sf http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
fi
echo "pulling $MODEL (log: $LOG)"
ollama pull "$MODEL" 2>&1 | tee -a "$LOG"
ollama list | tee -a "$LOG"
