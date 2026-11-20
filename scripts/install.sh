#!/bin/sh
# Installs the devdocs command line with uv (fetched with Astral's installer when
# missing), else pipx, else `pip install --user`; then runs `devdocs --version` and
# `devdocs doctor --offline` and prints a quick start. Running it again upgrades or
# reinstalls. Astral's installer runs from a temporary file, so a failed download
# runs nothing. POSIX sh; settings are listed by --help.
#
#   curl -LsSf https://raw.githubusercontent.com/bgantavya/devDocs/v4/scripts/install.sh | sh
#   sh scripts/install.sh --help

set -eu
unset CDPATH

TAG='devdocs-install:'
UV_INSTALLER='https://astral.sh/uv/install.sh'
DEFAULT_IFS=$IFS

usage() {
    cat <<'EOF'
usage: install.sh [--help]

Installs the devdocs command line from PyPI with uv, else pipx, else pip, then
runs `devdocs --version` and `devdocs doctor --offline`.

environment:
  DEVDOCS_VERSION         release to install, e.g. 4.0.0 (default: the latest)
  DEVDOCS_SOURCE          local path or URL to install from instead of PyPI
  DEVDOCS_PYTHON_VERSION  Python for devdocs, 3.11 or newer (default: 3.13)
  DEVDOCS_NO_UV_INSTALL   1: never install uv; without uv, use pipx, then pip
EOF
}

say() { printf '%s %s\n' "$TAG" "$*"; }
warn() { printf '%s warning: %s\n' "$TAG" "$*" >&2; }
die() { printf '%s error: %s\n' "$TAG" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

shell_word() {
    case "$1" in
        *' '*) printf '"%s"' "$1" ;;
        *) printf '%s' "$1" ;;
    esac
}

on_path() {
    case ":$PATH:" in
        *":$1:"* | *":$1/:"* | *":${1%/}:"*) return 0 ;;
    esac
    return 1
}

same_dir() {
    first=$(cd "$1" 2>/dev/null && pwd -P) || return 1
    second=$(cd "$2" 2>/dev/null && pwd -P) || return 1
    [ "$first" = "$second" ]
}

check_python_version() {
    case "$PY_VERSION" in
        [0-2] | [0-2].* | 3.[0-9] | 3.[0-9].* | 3.10 | 3.10.*)
            die "DEVDOCS_PYTHON_VERSION=$PY_VERSION: devdocs needs Python 3.11 or newer" ;;
    esac
}

resolve_spec() {
    if [ -n "${DEVDOCS_SOURCE:-}" ]; then
        if [ -n "${DEVDOCS_VERSION:-}" ]; then
            warn "DEVDOCS_VERSION is ignored because DEVDOCS_SOURCE is set"
        fi
        SPEC=$DEVDOCS_SOURCE
        case "$SPEC" in
            *://*)
                ORIGIN=$(printf '%s\n' "$SPEC" | sed 's#\(://\)[^/@]*@#\1***@#')  # hide credentials
                return 0 ;;
        esac
        [ -e "$SPEC" ] || die "DEVDOCS_SOURCE is neither a URL nor an existing path: $SPEC"
        if [ -d "$SPEC" ]; then
            SPEC=$(cd "$SPEC" && pwd)
        else
            case "$SPEC" in
                /*) ;;
                *) SPEC="$PWD/${SPEC#./}" ;;
            esac
        fi
        ORIGIN=$SPEC
    elif [ -n "${DEVDOCS_VERSION:-}" ]; then
        release=${DEVDOCS_VERSION#v}
        case "$release" in
            '' | *[!0-9A-Za-z.!+-]*)
                die "DEVDOCS_VERSION is not a version: $DEVDOCS_VERSION (e.g. 4.0.0)" ;;
        esac
        SPEC="devdocs==$release"
        ORIGIN="PyPI (version $release)"
    else
        SPEC='devdocs>=4'
        ORIGIN="PyPI (latest release)"
    fi
}

find_uv() {
    if have uv; then
        command -v uv
        return 0
    fi
    for candidate in \
        "${UV_INSTALL_DIR:+$UV_INSTALL_DIR/uv}" \
        "${UV_INSTALL_DIR:+$UV_INSTALL_DIR/bin/uv}" \
        "${XDG_BIN_HOME:+$XDG_BIN_HOME/uv}" \
        "${XDG_DATA_HOME:+$XDG_DATA_HOME/../bin/uv}" \
        "$HOME/.local/bin/uv" \
        "$HOME/.cargo/bin/uv"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

python_ok() {
    case "$1" in
        /usr/bin/python3)  # macOS stub: without developer tools it opens an installer
            if [ "$(uname -s)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
                return 1
            fi ;;
    esac
    "$1" -I -c 'import sys; sys.exit(not (sys.version_info >= (3, 11) and sys.prefix == sys.base_prefix))' \
        >/dev/null 2>&1
}

find_python() {
    for name in "python$PY_VERSION" python3 python3.15 python3.14 python3.13 python3.12 \
        python3.11 python; do
        IFS=:
        set -f
        for dir in $PATH; do
            IFS=$DEFAULT_IFS
            set +f
            case "$dir" in /*) ;; *) continue ;; esac
            if [ -x "$dir/$name" ] && python_ok "$dir/$name"; then
                printf '%s\n' "$dir/$name"
                return 0
            fi
        done
        IFS=$DEFAULT_IFS
        set +f
    done
    return 1
}

install_uv() {
    if ! have curl && ! have wget; then
        warn "neither curl nor wget is available to download uv"
        return 1
    fi
    say "installing uv with Astral's installer ($UV_INSTALLER)"
    script=$(mktemp "${TMPDIR:-/tmp}/devdocs-uv.XXXXXX") || return 1
    trap 'rm -f "$script"' EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    status=0
    if have curl; then
        curl --proto '=https' --tlsv1.2 -LsSf "$UV_INSTALLER" -o "$script" || status=$?
    else
        wget -q -O "$script" "$UV_INSTALLER" || status=$?
    fi
    if [ "$status" -eq 0 ]; then
        sh "$script" || status=$?
    fi
    rm -f "$script"
    trap - EXIT HUP INT TERM
    return "$status"
}

install_with_uv() {
    uv_version=$("$UV" --version 2>/dev/null) || die "$UV does not run"
    uv_version=${uv_version%%" ("*}
    "$UV" tool dir --bin >/dev/null 2>&1 \
        || die "$uv_version at $UV is too old for \`uv tool\`; update it (uv self update) and run this again"
    say "installing devdocs from $ORIGIN with $uv_version and Python $PY_VERSION"
    "$UV" tool install --python "$PY_VERSION" --force --reinstall --upgrade "$SPEC" \
        || die "uv could not install devdocs (see above)"
    BIN_DIR=$("$UV" tool dir --bin) || die "uv did not report its executable directory"
    if [ "$UV_FRESH" = 1 ] && same_dir "${UV%/*}" "$BIN_DIR" \
        && [ -z "${UV_NO_MODIFY_PATH:-}${INSTALLER_NO_MODIFY_PATH:-}" ]; then
        UPDATE_PATH="open a new terminal (uv's installer added it to your shell profile)"
        return 0
    fi
    if [ "$(command -v uv 2>/dev/null || true)" = "$UV" ]; then
        uv_word=uv
    else
        uv_word=$(shell_word "$UV")
    fi
    UPDATE_PATH="run \`$uv_word tool update-shell\` and open a new terminal"
}

install_with_pipx() {
    PYTHON_BIN=$(find_python) \
        || die "no Python 3.11 or newer on PATH for pipx to install devdocs with; install Python 3.11+ or uv (https://docs.astral.sh/uv/), then run this again"
    case "$ORIGIN" in
        *'://***@'*)
            die "pipx can print the credentials in DEVDOCS_SOURCE; use a URL without them (pip reads ~/.netrc), or install uv (https://docs.astral.sh/uv/) and run this again" ;;
    esac
    venvs=$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null) || venvs=
    [ -n "$venvs" ] || venvs=${PIPX_HOME:-$HOME/.local/pipx}/venvs
    if [ -d "$venvs/devdocs" ] \
        && ! "$venvs/devdocs/bin/python" -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
            >/dev/null 2>&1 \
        && (cd / && pipx uninstall devdocs) >/dev/null 2>&1; then  # --force keeps its Python; old pipx takes ./devdocs for a path
        say "removed the previous pipx install of devdocs: its Python is missing or older than 3.11"
    fi
    say "installing devdocs from $ORIGIN with pipx and $PYTHON_BIN"
    pipx install --force --python "$PYTHON_BIN" "$SPEC" \
        || die "pipx could not install devdocs (see above)"
    BIN_DIR=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null) || BIN_DIR=
    [ -n "$BIN_DIR" ] || BIN_DIR=${PIPX_BIN_DIR:-$HOME/.local/bin}
    UPDATE_PATH="run \`pipx ensurepath\` and open a new terminal"
}

install_with_pip() {
    PYTHON_BIN=$(find_python) || PYTHON_BIN=
    if [ -z "$PYTHON_BIN" ]; then
        if [ "${DEVDOCS_NO_UV_INSTALL:-}" = 1 ]; then
            die "no uv, no pipx and no Python 3.11 or newer on PATH; install Python 3.11+, or unset DEVDOCS_NO_UV_INSTALL to let this script install uv"
        fi
        die "no uv, no pipx and no Python 3.11 or newer on PATH; install uv (https://docs.astral.sh/uv/) or Python 3.11+, then run this again"
    fi
    "$PYTHON_BIN" -I -m pip --version >/dev/null 2>&1 \
        || die "$PYTHON_BIN has no pip; install pipx or uv and run this again"
    say "installing devdocs from $ORIGIN with pip --user and $PYTHON_BIN"
    "$PYTHON_BIN" -P -m pip install --user --upgrade --force-reinstall \
        --no-warn-script-location --disable-pip-version-check "$SPEC" \
        || die "pip could not install devdocs (see above); a Python marked externally managed (PEP 668) refuses --user installs: install pipx or uv, then run this again"
    BIN_DIR=$("$PYTHON_BIN" -I -c 'import sysconfig; print(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))') \
        || die "$PYTHON_BIN did not report where pip puts commands"
    UPDATE_PATH='add it to PATH in your shell profile and open a new terminal'
}

check_install() {
    COMMAND="$BIN_DIR/devdocs"
    [ -x "$COMMAND" ] || die "the install finished, but $COMMAND is missing"
    version_line=$("$COMMAND" --version </dev/null 2>&1) || die "$COMMAND does not run: $version_line"
    say "installed $version_line at $COMMAND"
    say "checking the setup with \`devdocs doctor --offline\`"
    status=0
    report=$("$COMMAND" doctor --offline </dev/null 2>&1) || status=$?
    printf '%s\n' "$report" | sed 's/^./    &/'
    case "$status" in
        0) say "doctor found nothing that blocks a run" ;;
        1) say "doctor reports something to fix before a run (lines marked !!), usually the API key; see the quick start" ;;
        *) warn "doctor exited with status $status; devdocs is installed, but fix what it reports" ;;
    esac
}

check_path() {
    found=$(command -v devdocs 2>/dev/null) || found=
    if [ -n "$found" ] && same_dir "${found%/*}" "$BIN_DIR"; then
        return 0
    fi
    if [ -n "$found" ]; then
        warn "\`devdocs\` on PATH runs $found, not the one just installed; remove that one (for v3: pip uninstall devDocs) or put $BIN_DIR first on PATH"
    fi
    if ! on_path "$BIN_DIR"; then
        say "$BIN_DIR is not on PATH yet: $UPDATE_PATH"
        say "for this terminal only: export PATH=\"$BIN_DIR:\$PATH\""
    fi
}

quick_start() {
    cat <<'EOF'

Quick start
  1. Get an API key:
       Gemini  https://aistudio.google.com/apikey
       Claude  https://console.anthropic.com/
  2. Export it (add the line to your shell profile to keep it):
       export GEMINI_API_KEY='your-key'
       export ANTHROPIC_API_KEY='your-key' DEVDOCS_PROVIDER=anthropic    # for Claude
  3. Document a project:
       cd your-project
       devdocs estimate    # free: prices the run, sends nothing
       devdocs generate    # writes the docs to docs/
EOF
}

main() {
    case "${1:-}" in
        '') ;;
        -h | --help) usage; return 0 ;;
        *) usage >&2; printf '%s error: unknown argument: %s\n' "$TAG" "$1" >&2; exit 2 ;;
    esac
    [ -n "${HOME:-}" ] || die "HOME is not set"

    PY_VERSION=${DEVDOCS_PYTHON_VERSION:-3.13}
    check_python_version
    resolve_spec

    UV_FRESH=0
    UV=$(find_uv) || UV=
    if [ -z "$UV" ]; then
        if [ "${DEVDOCS_NO_UV_INSTALL:-}" = 1 ]; then
            say "uv is not installed and DEVDOCS_NO_UV_INSTALL=1; using pipx or pip"
        elif install_uv && UV=$(find_uv); then
            UV_FRESH=1
        else
            UV=
            warn "could not install uv; trying pipx, then pip"
        fi
    fi

    if [ -n "$UV" ]; then
        install_with_uv
    elif have pipx; then
        install_with_pipx
    else
        install_with_pip
    fi

    check_install
    check_path
    quick_start
}

main "$@"
