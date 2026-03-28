#!/usr/bin/env bash
# Setup script for llama.cpp server with Qwen3.5-4B on a GPU host.
#
# Prerequisites:
#   - NVIDIA GPU with CUDA toolkit installed
#   - cmake, git, python3, pip
#   - huggingface-cli (pip install huggingface-hub[cli])
#
# Usage:
#   bash scripts/setup-llama-server.sh [--port PORT] [--model MODEL_ID]
#
# Defaults:
#   PORT=8080
#   MODEL=Qwen3.5-4B Q4_K_M GGUF from unsloth

set -euo pipefail

PORT="${1:-8080}"
LLAMA_DIR="${LLAMA_DIR:-$HOME/llama.cpp}"
MODEL_DIR="${MODEL_DIR:-$HOME/models}"
MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.5-4B-GGUF}"
MODEL_FILE="${MODEL_FILE:-Qwen3.5-4B-Q4_K_M.gguf}"

echo "=== llama.cpp server setup ==="
echo "  LLAMA_DIR:  $LLAMA_DIR"
echo "  MODEL_DIR:  $MODEL_DIR"
echo "  MODEL_REPO: $MODEL_REPO"
echo "  MODEL_FILE: $MODEL_FILE"
echo "  PORT:       $PORT"
echo ""

# --- Step 1: Build llama.cpp ---
if [ -x "$LLAMA_DIR/build/bin/llama-server" ]; then
    echo "[1/3] llama.cpp already built at $LLAMA_DIR"
else
    echo "[1/3] Building llama.cpp with CUDA support..."
    if [ ! -d "$LLAMA_DIR" ]; then
        git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
    fi
    cd "$LLAMA_DIR"
    git pull --ff-only 2>/dev/null || true
    cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
    cmake --build build -j"$(nproc)" --target llama-server
    echo "  Built: $LLAMA_DIR/build/bin/llama-server"
fi

# --- Step 2: Download model ---
MODEL_PATH="$MODEL_DIR/$MODEL_FILE"
if [ -f "$MODEL_PATH" ]; then
    echo "[2/3] Model already downloaded at $MODEL_PATH"
else
    echo "[2/3] Downloading $MODEL_REPO/$MODEL_FILE..."
    mkdir -p "$MODEL_DIR"
    huggingface-cli download "$MODEL_REPO" "$MODEL_FILE" \
        --local-dir "$MODEL_DIR" \
        --local-dir-use-symlinks False
    echo "  Downloaded: $MODEL_PATH"
fi

# --- Step 3: Start server ---
echo "[3/3] Starting llama-server on port $PORT..."
echo "  Model: $MODEL_PATH"
echo "  GPU layers: all (-ngl 99)"
echo ""
echo "  API endpoint: http://localhost:$PORT/v1/chat/completions"
echo "  Health check: curl http://localhost:$PORT/health"
echo ""
echo "  To stop: kill \$(cat /tmp/llama-server.pid)"
echo ""

exec "$LLAMA_DIR/build/bin/llama-server" \
    --model "$MODEL_PATH" \
    --port "$PORT" \
    --n-gpu-layers 99 \
    --ctx-size 8192 \
    --threads "$(nproc)" \
    --log-disable
