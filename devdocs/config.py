"""Run configuration, resolved from flags, environment, and `devdocs.toml`.

Precedence is fixed and total: an explicit flag beats the environment, which
beats the config file, which beats the built-in defaults. Nothing merges
silently, so `Config.origin_of` can name the layer behind every setting. The
API key has its own record, `KeySource`, and that is the only source `devdocs
doctor` reports.

The file is looked for from the project path that a flag or DEVDOCS_PATH gives,
else from the current directory, up to a repo root or the home directory; see
`find_config_file`. A relative `path` is read from where it was given: in the
file, from the directory that holds the file; as a flag or a variable, from the
current directory. A relative `out`, from any layer, is read from the project.

Paths are checked too, early enough for a command to refuse them before it
creates anything: the project root must be an existing directory, and the
output directory can be neither the root nor above it, where the docs would
mix with the project's own files.

The API key is resolved separately by `resolve_api_key` because it has one
extra step at the bottom: a shared demo key, if `devdocs.demo` sets one. None
ships, so a command that needs a key refuses without one. A demo key's quota is
shared by every user of it, so it warns and is never a silent default.
"""

from __future__ import annotations

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path, PurePath
from stat import S_ISDIR
from typing import Any

from devdocs.errors import ConfigError
from devdocs.render import FORMATS, PAGE_SIZES, PDF_ENGINES

__all__ = [
    "Config",
    "KeySource",
    "key_variables",
    "load_config",
    "project_root",
    "resolve_api_key",
    "search_start",
]

CONFIG_FILENAME = "devdocs.toml"

# Per-provider key lookup, in order. First non-empty wins.
_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "gemini": ("DEVDOCS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("DEVDOCS_API_KEY", "ANTHROPIC_API_KEY"),
    "anthropic-sdk": ("DEVDOCS_API_KEY", "ANTHROPIC_API_KEY"),
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


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
    model: str = ""                  # pin one model for every call
    quality: str = "cheap"           # "cheap" | "balanced" | "high"
    context: str = "standard"        # "off" | "standard" | "semantic"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_cost: float = 0.50           # USD ceiling; 0 disables the guard
    max_requests: int = 0            # 0 means unlimited
    concurrency: int = 4
    free_tier: bool = True           # pace requests to stay inside free limits
    per_file: bool = False           # v3-compatible one-doc-per-file layout
    formats: tuple[str, ...] = ("markdown",)
    pdf_engine: str = "builtin"      # "builtin" | "chrome" | "weasyprint"
    pdf_split: bool = False          # one PDF per document, not one book
    page_size: str = "A4"            # "A4" | "Letter"
    name: str = ""                   # project name; else read from its manifest
    description: str = ""
    api_key: str = ""
    key_source: KeySource | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def origin_of(self, name: str) -> str:
        """Report which layer supplied `name` ("flag", "env", "file", "default").
        Not for `api_key`, which is resolved apart: see `KeySource`."""
        return self.sources.get(name, "default")


_QUALITY = {"cheap", "balanced", "high"}
_CONTEXT = {"off", "standard", "semantic"}
_PROVIDERS = set(_ENV_KEYS)
_FORMATS = set(FORMATS)
_PAGE_SIZES = {size.casefold(): size for size in PAGE_SIZES}   # "letter" -> "Letter"
_OWN_DIRECTORY = "give the docs a directory of their own, such as docs (the default)"
_EMPTY_PATH = {
    "path": f'use "." for the current directory, or in {CONFIG_FILENAME} for its own',
    "out": _OWN_DIRECTORY,
}


def _as_path(name: str, value: Any) -> Path:
    """`value` as a path, `~` expanded. Empty is refused, not read as ".": an
    unset variable must not stand for the current directory or the root."""
    if not isinstance(value, str | PurePath):
        raise ConfigError(f"{name} must be a string")
    text = str(value)
    if not text.strip():
        raise ConfigError(f"{name} is empty", hint=_EMPTY_PATH.get(name))
    if "\0" in text:
        raise ConfigError(f"{name} contains a NUL character")
    return Path(os.path.expanduser(text))


def project_root(path: str | PurePath) -> Path:
    """The project directory `path` names, resolved and `~` expanded.

    Raises ConfigError unless it is an existing directory devdocs can list
    and search, so a command can refuse a typo, or a root it would scan as
    empty, before it creates any state there.
    """
    root = _as_path("path", path)
    try:
        root = root.resolve()
        if not S_ISDIR(root.stat().st_mode):
            raise ConfigError(f"project root {root} is not a directory",
                              hint=f"pass the directory that holds it, {root.parent}")
        with os.scandir(root):   # stat passes a root it cannot list,
            pass
        os.stat(os.path.join(root, os.curdir))   # or search
    except (FileNotFoundError, NotADirectoryError):   # also a removed cwd
        raise ConfigError(f"project root {root} does not exist") from None
    except (OSError, RuntimeError) as err:   # no access; a symlink loop
        reason = getattr(err, "strerror", None) or err
        raise ConfigError(f"cannot open project root {root}: {reason}") from None
    return root


def _stat(path: Path) -> os.stat_result | None:
    """The status of what `path` will name once its missing directories are
    made, as `new/..` then names the one holding new; None if nothing yet."""
    try:
        return os.stat(os.path.realpath(path))
    except OSError:
        return None


def _check_out(root: Path, out: Path) -> None:
    """Refuse an `out` that is the project root or a directory above it,
    where the pages, the manifest and their pruning would mix with the
    project's own files. Compared by identity, not name, so `src/..`, a
    symlink and another case on a case-insensitive disk are caught too;
    and as it will be once made, so `new/..` is too."""
    made = _stat(root / out)
    if made is None:   # to be made, so neither the root nor above it
        return
    for directory in (root, *root.parents):
        there = _stat(directory)
        if there is None or not os.path.samestat(made, there):
            continue
        if directory == root:
            raise ConfigError(f"out is the project root, {root}", hint=_OWN_DIRECTORY)
        raise ConfigError(f"out {directory} contains the project root, {root}",
                          hint=_OWN_DIRECTORY)


def _denied_link(path: Path) -> bool:
    """Whether `path` is a link devdocs may not follow. The walk counts one as
    there. Skipping such a file could trade its budget for that of a file
    further up, so reading it refuses the run, as for any file it may not read;
    such a `.git` is a stop, since git will not look past one either.
    `Path.is_file` and `.exists` raise for one on 3.11 and 3.13 and say False
    on 3.15, so the walk does not ask them."""
    try:
        os.stat(path)
    except PermissionError:
        return os.path.islink(path)
    except OSError:
        pass
    return False


def find_config_file(start: Path, *, home: Path | None = None) -> Path | None:
    """Walk up from `start` looking for devdocs.toml, stopping at the repo root
    or the home directory.

    Walking up means a subdirectory run picks up the project's config instead
    of silently using defaults. A file in home is read only by a search that
    starts at home, even if home is a repo: read from below, a stray one would
    set the budget, the provider and even the project of every run with no
    nearer file or repo. Read from home, its `path` can still name a project
    below. With neither stop on the way, the walk runs to the filesystem root;
    a home that cannot be determined is no stop. `home` is injectable so tests
    need not mutate os.environ.
    """
    current = start.resolve()
    # expanduser, as Path.home() raises if none can be determined
    ceiling = _stat(Path(os.path.expanduser("~")) if home is None else home)
    for directory in (current, *current.parents):
        there = _stat(directory)
        at_home = (ceiling is not None and there is not None
                   and os.path.samestat(ceiling, there))
        if at_home and directory != current:
            break
        candidate = directory / CONFIG_FILENAME
        if os.path.isfile(candidate) or _denied_link(candidate):
            return candidate
        git = directory / ".git"
        if at_home or os.path.exists(git) or _denied_link(git):
            break
    return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (ValueError, RecursionError) as err:
        # TOMLDecodeError; not UTF-8; a number too long; nested too deep
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
        return _as_path(name, value)
    if isinstance(current, tuple):
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        if isinstance(value, list | tuple):   # TOML arrays; flags arrive as tuples
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
        try:
            return float(value)
        except OverflowError:   # an integer past any float: as 1e400 reads
            return math.inf
    return str(value)


def _from_env(name: str, raw: str, current: Any) -> Any:
    """Parse an environment string into the shape `_coerce` expects."""
    text = raw.strip()
    if isinstance(current, bool):
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise ConfigError(f"DEVDOCS_{name.upper()} must be true or false")
    if isinstance(current, int | float) and not isinstance(current, bool):
        try:
            return int(text) if isinstance(current, int) else float(text)
        except ValueError:
            raise ConfigError(f"DEVDOCS_{name.upper()} must be a number") from None
    return text


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
    if config.context not in _CONTEXT:
        raise ConfigError(
            f"unknown context {config.context!r}",
            hint=f"choose one of: {', '.join(sorted(_CONTEXT))}",
        )
    if config.context == "semantic" and config.provider != "gemini":
        raise ConfigError(
            "semantic context needs Gemini embeddings",
            hint='use context = "standard" with Anthropic, or provider = "gemini"',
        )
    if not config.formats:   # an empty flag, variable or list would write nothing
        raise ConfigError("no output format configured",
                          hint=f"choose one or more of: {', '.join(FORMATS)}")
    unknown = set(config.formats) - _FORMATS
    if unknown:
        raise ConfigError(
            f"unknown format(s): {', '.join(sorted(unknown))}",
            hint=f"choose from: {', '.join(sorted(_FORMATS))}",
        )
    if config.pdf_engine not in PDF_ENGINES:
        raise ConfigError(
            f"unknown PDF engine {config.pdf_engine!r}",
            hint=f"choose one of: {', '.join(PDF_ENGINES)}",
        )
    if config.page_size not in PAGE_SIZES:
        raise ConfigError(
            f"unknown page size {config.page_size!r}",
            hint=f"choose one of: {', '.join(PAGE_SIZES)}",
        )
    if "pdf" in config.formats and config.pdf_split and config.pdf_engine != "builtin":
        raise ConfigError(
            f"the {config.pdf_engine} PDF engine prints one book and cannot split it",
            hint="split with the builtin engine (--pdf-engine builtin), or print "
                 "one book (--no-split, or pdf_split = false)",
        )
    # TOML, argparse and float() all accept nan and inf.
    if not math.isfinite(config.max_cost):
        raise ConfigError(
            f"max_cost must be a number of dollars, not {config.max_cost}",
            hint="use 0 for no cost ceiling",
        )
    if config.max_cost < 0:
        raise ConfigError("max_cost cannot be negative")
    if config.max_requests < 0:
        raise ConfigError("max_requests cannot be negative",
                          hint="use 0 for no request limit")
    if config.concurrency < 1:
        raise ConfigError("concurrency must be at least 1")


def search_start(flagged: Any, environment: Mapping[str, str]) -> Path:
    """Where the search for the config file starts, and `devdocs init` writes
    one: the project path the flag gives, else DEVDOCS_PATH, else the current
    directory; a blank variable is unset. A `path` in the file cannot steer
    either. Checked here, so a typo is reported as one, not by a config file
    found above it."""
    if flagged is not None:
        return project_root(flagged)
    return project_root(environment.get("DEVDOCS_PATH", "").strip() or ".")


def load_config(
    overrides: dict[str, Any] | None = None,
    *,
    config_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Build a Config from defaults, then the file, the environment and flags.

    `overrides` holds parsed CLI flags and wins over everything. A None value
    means the flag was absent, so it never masks a configured value. `env` is
    injectable so tests need not mutate os.environ.
    """
    config = Config()
    known = {f.name for f in fields(Config)} - {"sources", "key_source"}
    environment: Mapping[str, str] = os.environ if env is None else env

    start = search_start((overrides or {}).get("path"), environment)
    file_path = config_file or find_config_file(start)
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
        if config.origin_of("path") == "file":
            config.path = file_path.parent / config.path

    # Keys are resolved apart by `resolve_api_key`, so never land in Config.
    for name in sorted(known - {"api_key"}):
        raw = environment.get(f"DEVDOCS_{name.upper()}", "")
        if not raw.strip():
            continue
        current = getattr(config, name)
        setattr(config, name, _coerce(name, _from_env(name, raw, current), current))
        config.sources[name] = "env"

    for name, value in (overrides or {}).items():
        if value is None or name not in known:
            continue
        setattr(config, name, _coerce(name, value, getattr(config, name)))
        config.sources[name] = "flag"

    config.page_size = _PAGE_SIZES.get(config.page_size.casefold(), config.page_size)
    _validate(config)
    _check_out(project_root(config.path), config.out)   # the file may move the root
    return config


def key_variables(provider: str) -> tuple[str, ...]:
    """The environment variables `resolve_api_key` reads for `provider`'s key,
    in order; none for a provider it does not know. A message naming the
    user's choices names these."""
    return _ENV_KEYS.get(provider, ())


def resolve_api_key(
    config: Config,
    *,
    explicit: str = "",
    env: Mapping[str, str] | None = None,
    demo_key: str | None = None,
) -> tuple[str, KeySource]:
    """Find a usable API key and say where it came from.

    Order: flag, then the provider's environment variables, then the config
    file, then `demo_key` if the caller has one, reported as shared so the
    caller can warn. None ships (see `devdocs.demo`), so with no key of the
    user's own this raises a ConfigError naming the flag and the variables.
    """
    environment: Mapping[str, str] = os.environ if env is None else env

    if explicit.strip():
        return explicit.strip(), KeySource("flag")

    for var in _ENV_KEYS.get(config.provider, ()):
        value = environment.get(var, "").strip()
        if value:
            return value, KeySource("env", var)

    if config.api_key.strip():
        return config.api_key.strip(), KeySource("file", CONFIG_FILENAME)

    if demo_key:
        return demo_key, KeySource("demo", shared=True)

    variables = ", ".join(f"${var}" for var in _ENV_KEYS[config.provider])
    raise ConfigError(
        f"no API key for provider {config.provider!r}",
        hint=f"pass --api-key or set one of: {variables}",
    )
