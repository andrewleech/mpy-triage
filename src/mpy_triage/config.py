"""Configuration management for mpy-triage."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class EmbeddingConfig:
    """Embedding model configuration."""

    model_id: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_dim: int = 1024
    query_prefix: str = (
        "Instruct: Find duplicate GitHub issues about the same bug or feature\n"
        "Query: "
    )
    document_prefix: str = ""
    max_seq_length: int = 32768
    device: str | None = field(default=None)

    def __post_init__(self):
        if self.device is None:
            try:
                import torch

                self.device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                self.device = "cpu"


@dataclass
class SummarizeConfig:
    """Summarization backend configuration."""

    backend: str = "claude"  # "claude" or "local"
    local_url: str = "http://localhost:8080"
    local_model: str = "qwen3.5-4b"
    timeout: int = 300


@dataclass
class RetrievalConfig:
    """Retrieval pipeline configuration."""

    top_k_initial: int = 100
    top_k_rerank: int = 20
    top_k_assess: int = 5
    reranker_model: str = "BAAI/bge-reranker-large"
    rrf_k: int = 60


@dataclass
class TriageConfig:
    """Top-level triage configuration."""

    project_root: Path = field(default_factory=lambda: Path(__file__).parent.parent.parent)
    db_path: Path = field(default=None)
    schema_path: Path = field(default=None)
    prompts_dir: Path = field(default=None)
    micropython_path: Path = field(default=None)
    repos: list = field(default_factory=lambda: [
        "micropython/micropython",
        "micropython/micropython-lib",
    ])
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    summarize: SummarizeConfig = field(default_factory=SummarizeConfig)

    def __post_init__(self):
        if self.db_path is None:
            self.db_path = self.project_root / "data" / "triage.db"
        if self.schema_path is None:
            self.schema_path = self.project_root / "schema.sql"
        if self.prompts_dir is None:
            self.prompts_dir = self.project_root / "prompts"
        if self.micropython_path is None:
            candidate = self.project_root / "micropython"
            if candidate.is_dir():
                self.micropython_path = candidate


_config: Optional[TriageConfig] = None


def get_config() -> TriageConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = TriageConfig()
    return _config


def set_config(config: TriageConfig) -> None:
    """Set the global configuration instance."""
    global _config
    _config = config


def clean_env() -> dict:
    """Copy os.environ, removing keys starting with CLAUDECODE.

    Used when spawning claude subprocesses to prevent recursion
    when running inside Claude Code.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE")}
