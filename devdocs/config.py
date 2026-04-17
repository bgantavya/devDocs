"""Run configuration, resolved from flags, environment, and `devdocs.toml`.

Precedence is fixed and total: an explicit flag beats the environment, which
beats the config file, which beats the built-in defaults. Nothing merges
silently, so `devdocs doctor` can always name where a value came from.

The API key is resolved separately by `resolve_api_key` because it has one
extra step at the bottom: a bundled demo key that lets someone try the tool
without signing up. That key's quota is shared by every user of it, so it warns
and is never a silent default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from devdocs.errors import ConfigError

__all__ = ["Config", "KeySource", "load_config", "resolve_api_key"]

CONFIG_FILENAME = "devdocs.toml"

# Per-provider key lookup, in order. First non-empty wins.
_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "gemini": ("DEVDOCS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("DEVDOCS_API_KEY", "ANTHROPIC_API_KEY"),
}


@dataclass(frozen=True)
class KeySource:
    """Where a key came from, so `doctor` can report it without echoing it."""

    origin: str          # "flag" | "env" | "file" | "demo"
    detail: str = ""     # env var name, or config path
    shared: bool = False # True for the demo key: quota is not yours alone


@dataclass
class Config:
    """Everything a run needs, after precedence has been applied.

    Constructed by `load_config`; not read from disk directly so that tests can
    build one literally.
    """

    path: Path = Path(".")
    out: Path = Path("docs")
    provider: str = "gemini"
    quality: str = "cheap"           # "cheap" | "balanced" | "high"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_cost: float = 0.50           # USD ceiling; 0 disables the guard
    max_requests: int = 0            # 0 means unlimited
    concurrency: int = 4
    free_tier: bool = True           # pace requests to stay inside free limits
    per_file: bool = False           # v3-compatible one-doc-per-file layout
    formats: tuple[str, ...] = ("markdown",)
    api_key: str = ""
    key_source: KeySource | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def origin_of(self, name: str) -> str:
        """Report which layer supplied `name` ("flag", "env", "file", "default")."""
        return self.sources.get(name, "default")


_QUALITY = {"cheap", "balanced", "high"}
_PROVIDERS = {"gemini", "anthropic"}
_FORMATS = {"markdown", "html", "pdf"}


def find_config_file(start: Path) -> Path | None:
    """Walk up from `start` looking for devdocs.toml, stopping at the repo root.

    Walking up means a subdirectory run picks up the project's config instead
    of silently using defaults.
    """
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():
            break
    return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path} is not valid TOML: {err}") from err
    except OSError as err:
        raise ConfigError(f"cannot read {path}: {err}") from err

    section = data.get("devdocs", data)
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: [devdocs] must be a table")
    return section


def _coerce(name: str, value: Any, current: Any) -> Any:
    """Convert a TOML value to the dataclass field's type, or explain why not."""
    if isinstance(current, Path):
        return Path(str(value))
    if isinstance(current, tuple):
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        if isinstance(value, list):
            return tuple(str(item) for item in value)
        raise ConfigError(f"{name} must be a list or comma-separated string")
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{name} must be true or false")
        return value
    if isinstance(current, int) and not isinstance(current, bool):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{name} must be an integer")
        return value
    if isinstance(current, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{name} must be a number")
        return float(value)
    return str(value)


def _validate(config: Config) -> None:
    if config.provider not in _PROVIDERS:
        raise ConfigError(
            f"unknown provider {config.provider!r}",
            hint=f"choose one of: {', '.join(sorted(_PROVIDERS))}",
        )
    if config.quality not in _QUALITY:
        raise ConfigError(
            f"unknown quality {config.quality!r}",
            hint=f"choose one of: {', '.join(sorted(_QUALITY))}",
        )
    unknown = set(config.formats) - _FORMATS
    if unknown:
        raise ConfigError(
            f"unknown format(s): {', '.join(sorted(unknown))}",
            hint=f"choose from: {', '.join(sorted(_FORMATS))}",
        )
    if config.max_cost < 0:
        raise ConfigError("max_cost cannot be negative")
    if config.concurrency < 1:
        raise ConfigError("concurrency must be at least 1")


def load_config(
    overrides: dict[str, Any] | None = None,
    *,
    config_file: Path | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Build a Config from defaults, then the file, then flags.

    `overrides` holds parsed CLI flags and wins over everything. A None value
    means the flag was absent, so it never masks a configured value. `env` is
    injectable so tests need not mutate os.environ.
    """
    env = os.environ if env is None else env
    config = Config()
    known = {f.name for f in fields(Config)} - {"sources", "key_source"}

    path_hint = Path(str((overrides or {}).get("path") or "."))
    file_path = config_file or find_config_file(path_hint)
    if file_path is not None:
        for key, value in _read_toml(file_path).items():
            name = key.replace("-", "_")
            if name not in known:
                raise ConfigError(
                    f"{file_path}: unknown option {key!r}",
                    hint=f"valid options: {', '.join(sorted(known))}",
                )
            setattr(config, name, _coerce(name, value, getattr(config, name)))
            config.sources[name] = "file"

    for name, value in (overrides or {}).items():
        if value is None or name not in known:
            continue
        setattr(config, name, _coerce(name, value, getattr(config, name)))
        config.sources[name] = "flag"

    _validate(config)
    return config


def resolve_api_key(
    config: Config,
    *,
    explicit: str = "",
    env: dict[str, str] | None = None,
    demo_key: str | None = None,
) -> tuple[str, KeySource]:
    """Find a usable API key and say where it came from.

    Order: flag, then the provider's environment variables, then the config
    file, then the bundled demo key. The demo key is last and is reported as
    shared so the caller can warn; it exists so a first run needs no signup.
    """
    env = os.environ if env is None else env

    if explicit.strip():
        return explicit.strip(), KeySource("flag")

    for var in _ENV_KEYS.get(config.provider, ()):
        value = env.get(var, "").strip()
        if value:
            return value, KeySource("env", var)

    if config.api_key.strip():
        return config.api_key.strip(), KeySource("file", CONFIG_FILENAME)

    if demo_key:
        return demo_key, KeySource("demo", shared=True)

    raise ConfigError(
        f"no API key for provider {config.provider!r}",
        hint=f"pass --api-key or set ${_ENV_KEYS[config.provider][1]}",
    )
