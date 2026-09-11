"""Central configuration, resolved once from the environment.

Every tunable in the pipeline is read here and nowhere else.  Steps import
``settings``; they never call ``os.getenv`` themselves.  That keeps "what can I
change without editing code?" answerable by reading one file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var, accepting 1/true/yes/on in any casing."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` if unset or malformed."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to ``default`` if unset or malformed."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the runtime configuration."""

    gemini_api_key: str | None
    judge_model: str
    embed_model: str
    embed_dim: int
    db_path: Path
    stub_llm: bool
    stub_embed: bool
    top_k: int
    retry_k_multiplier: int
    max_workers: int
    chunk_target_chars: int
    chunk_overlap_ratio: float

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables (and .env, already loaded)."""
        raw_db = os.getenv("AVA_DB_PATH", "data/ava_eval.db")
        db_path = Path(raw_db)
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path

        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            judge_model=os.getenv("AVA_JUDGE_MODEL", "gemini-3.8-flash"),
            embed_model=os.getenv("AVA_EMBED_MODEL", "gemini-embedding-2"),
            embed_dim=_env_int("AVA_EMBED_DIM", 768),
            db_path=db_path,
            stub_llm=_env_bool("AVA_STUB_LLM", True),
            stub_embed=_env_bool("AVA_STUB_EMBED", True),
            top_k=_env_int("AVA_TOP_K", 5),
            retry_k_multiplier=_env_int("AVA_RETRY_K_MULTIPLIER", 3),
            max_workers=_env_int("AVA_MAX_WORKERS", 4),
            chunk_target_chars=_env_int("AVA_CHUNK_TARGET_CHARS", 1200),
            chunk_overlap_ratio=_env_float("AVA_CHUNK_OVERLAP_RATIO", 0.15),
        )


settings = Settings.from_env()
