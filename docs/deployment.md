# Deployment

## GPU Host Setup for Embedding

The embedding stage benefits from a CUDA GPU. The embedding model (Qwen3-Embedding-0.6B) and cross-encoder reranker (BAAI/bge-reranker-large) both use PyTorch and will auto-detect CUDA availability.

### GPU vs CPU Performance

On CPU, embedding ~15k items takes many hours. On a CUDA GPU, the same operation completes in a fraction of the time. The reranker runs per-query (scoring ~20 candidates) and is fast on either device, but GPU is noticeably faster for interactive use.

If no GPU is available, the tool works on CPU. Set `--batch-size 1` if memory is constrained.

### CUDA Requirements

- NVIDIA GPU with CUDA support
- CUDA toolkit installed
- PyTorch with CUDA (installed via `uv sync` if the system has CUDA)

The `EmbeddingConfig.device` auto-detects: uses `cuda` if `torch.cuda.is_available()` returns true, otherwise falls back to `cpu`.

## Local LLM Backend for Summarization

The `summarize` stage can use a local llama.cpp server instead of Claude Haiku.

### Setup Script

`scripts/setup-llama-server.sh` automates the build and launch:

```bash
bash scripts/setup-llama-server.sh [--port PORT]
```

The script:
1. Clones and builds llama.cpp with CUDA support
2. Downloads Qwen3.5-4B Q4_K_M GGUF from Hugging Face
3. Starts `llama-server` on the specified port (default 8080)

Prerequisites: NVIDIA GPU with CUDA toolkit, cmake, git, python3, `huggingface-cli`.

Environment variables for customization:
- `LLAMA_DIR` — llama.cpp install path (default: `$HOME/llama.cpp`)
- `MODEL_DIR` — model download path (default: `$HOME/models`)
- `MODEL_REPO` — Hugging Face repo (default: `unsloth/Qwen3.5-4B-GGUF`)
- `MODEL_FILE` — GGUF filename (default: `Qwen3.5-4B-Q4_K_M.gguf`)

### Server Flags

The script passes `--reasoning-budget 0` to disable Qwen3.5's chain-of-thought mode. Without this flag, the model generates 2000+ internal reasoning tokens per request, wasting GPU time and producing no useful output.

Other flags: `--ctx-size 8192`, `--n-gpu-layers 99` (offload all layers to GPU).

### Running Summarization Against Local Server

```bash
uv run mpy-triage summarize --backend local --local-url http://gpu-host:8080
```

The local backend processes items sequentially (one at a time). The `--concurrency` flag has no effect with this backend.

### Quality Tradeoff

Haiku produces higher-quality summaries than the local Qwen3.5-4B model, particularly for specificity of error messages, file paths, and function names. See the evaluation results in [architecture.md](architecture.md#backend-evaluation). The local backend is suitable for experimentation or when Claude API access is unavailable.

## Claude CLI Requirements

The `summarize` (claude backend) and `assess` stages invoke `claude` as a subprocess:

```
claude --model haiku -p <prompt> --output-format json --json-schema <schema>
claude --model sonnet -p <prompt> --output-format json --json-schema <schema>
```

### Setup

1. Install the Claude CLI
2. Authenticate via OAuth: `claude login`
3. Verify: `claude --model haiku -p "test"`

The tool strips `CLAUDECODE*` environment variables before spawning subprocesses to prevent recursion when running inside Claude Code.

### Cost Expectations

- Summarization (Haiku): ~$0.001 per item, ~$15 for full corpus (~15k items)
- Assessment (Sonnet): runs per-query, costs depend on usage frequency

## Transferring the Database Between Hosts

The database file is at `data/triage.db`. A typical workflow uses one host for collection/summarization and another for embedding.

### rsync Workflow

From the collection host to the GPU host:

```bash
rsync -avP data/triage.db gpu-host:/path/to/mpy-github-triage/data/
```

After embedding on the GPU host, sync back:

```bash
rsync -avP gpu-host:/path/to/mpy-github-triage/data/triage.db data/
```

### WAL Mode

SQLite WAL mode is enabled. When transferring, also copy the WAL and SHM files if they exist:

```bash
rsync -avP data/triage.db data/triage.db-wal data/triage.db-shm gpu-host:/path/to/data/
```

Alternatively, run `PRAGMA wal_checkpoint(TRUNCATE)` before transfer to fold the WAL back into the main database file.

## Split-Host Workflow Example

A typical deployment splits work across two machines:

**Host A (CPU, internet access):**
```bash
uv run mpy-triage collect
uv run mpy-triage summarize
uv run mpy-triage assemble
rsync -avP data/triage.db* gpu-host:/path/to/data/
```

**Host B (GPU):**
```bash
uv run mpy-triage embed
rsync -avP data/triage.db* cpu-host:/path/to/data/
```

**Host A (triage queries):**
```bash
uv run mpy-triage issue 12345
```

Note: the `issue` and `pr` commands load the embedding model and reranker for search. These run faster on a GPU host but work on CPU.
