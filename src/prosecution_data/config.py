"""Configuration loading with secret-safe defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
import tomllib


@dataclass(frozen=True)
class AppConfig:
    output_root: Path
    sample_size: int = 100
    random_seed: int = 42
    odp_base_url: str = "https://api.uspto.gov"
    timeout_seconds: float = 30.0
    max_retries: int = 4
    requests_per_minute: float = 30.0
    fail_fast: bool = False
    min_native_characters: int = 80
    min_printable_ratio: float = 0.85
    api_key: str | None = field(default=None, repr=False)


def load_config(path: Path | None, env: Mapping[str, str]) -> AppConfig:
    """Load non-secret TOML settings and the API key from the environment."""

    data: dict[str, object] = {}
    base = Path.cwd()
    if path is not None:
        path = Path(path)
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        base = path.parent

    output_value = str(data.get("output_root", "data"))
    output_root = Path(output_value)
    if not output_root.is_absolute():
        output_root = base / output_root

    return AppConfig(
        output_root=output_root,
        sample_size=int(data.get("sample_size", 100)),
        random_seed=int(data.get("random_seed", 42)),
        odp_base_url=str(data.get("odp_base_url", "https://api.uspto.gov")),
        timeout_seconds=float(data.get("timeout_seconds", 30.0)),
        max_retries=int(data.get("max_retries", 4)),
        requests_per_minute=float(data.get("requests_per_minute", 30.0)),
        fail_fast=bool(data.get("fail_fast", False)),
        min_native_characters=int(data.get("min_native_characters", 80)),
        min_printable_ratio=float(data.get("min_printable_ratio", 0.85)),
        api_key=env.get("USPTO_API_KEY") or None,
    )
