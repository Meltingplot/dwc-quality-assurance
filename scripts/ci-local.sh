#!/usr/bin/env bash
#
# Run the GitHub Actions CI pipeline (.github/workflows/ci.yml) locally.
#
# Everything is kept inside .ci-local/ (gitignored):
#   .ci-local/venv               Python virtualenv: pytest, pytest-cov, dsf-python 3.7.0b1
#   .ci-local/DuetWebControl     Meltingplot/DuetWebControl checkout for the package build
#   .ci-local/dist               built plugin ZIP
#   .ci-local/version-backup     plugin.json / package.json snapshots
#
# Usage:
#   scripts/ci-local.sh [stage ...]
#
# Stages:
#   python      pytest (venv, host Python >= 3.11)
#   matrix      pytest on Python 3.11/3.12/3.13 via Docker (the CI matrix)
#   frontend    npm ci + lint + vitest
#   build       DWC checkout + build-plugin-pkg -> QualityAssurance-<version>.zip
#   all         python + frontend + build (default)
#
# Env overrides:
#   DWC_REF=v3.7-dev    Meltingplot/DuetWebControl ref to build against
#   BUILD_DOCKER=0      never fall back to Docker when the host Node is < 22
#   PYTHON=python3      interpreter used to create the venv

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$ROOT/.ci-local"
VENV="$WORK/venv"
DIST="$WORK/dist"
DWC_DIR="$WORK/DuetWebControl"
DWC_REF="${DWC_REF:-v3.7-dev}"
PYTHON="${PYTHON:-python3}"
PY_MATRIX=(3.11 3.12 3.13)
PY_DEPS=("pytest>=8" "pytest-cov>=5" "dsf-python==3.7.0b1")

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

mkdir -p "$WORK"

stage_python() {
    step "Python tests (venv, $($PYTHON -V))"
    if [ ! -x "$VENV/bin/python" ]; then
        "$PYTHON" -m venv "$VENV"
    fi
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet "${PY_DEPS[@]}"
    cd "$ROOT"
    PYTHONPATH=dsf "$VENV/bin/python" -m pytest tests/ -v --tb=short --cov --cov-report=term-missing
    ok "Python tests passed"
}

stage_matrix() {
    step "Python matrix via Docker (${PY_MATRIX[*]})"
    command -v docker >/dev/null || die "docker not available"
    for v in "${PY_MATRIX[@]}"; do
        step "Python $v (docker)"
        docker run --rm \
            -v "$ROOT:/src:ro" \
            -w /tmp/work \
            -e PYTHONPATH=dsf \
            "python:$v-slim" \
            bash -c 'set -e
                     cp -r /src/. /tmp/work
                     rm -rf /tmp/work/.ci-local /tmp/work/node_modules
                     find /tmp/work -name __pycache__ -type d -prune -exec rm -rf {} +
                     pip install --quiet --root-user-action=ignore "pytest>=8" "pytest-cov>=5" "dsf-python==3.7.0b1"
                     pytest tests/ -q --tb=short' \
            || die "Python $v failed"
    done
    ok "Python matrix passed"
}

stage_frontend() {
    step "Frontend lint & tests (node $(node -v))"
    cd "$ROOT"
    npm ci
    npm run lint
    npm test
    ok "Frontend lint & tests passed"
}

node_major() {
    node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0
}

# Run a shell snippet under Node >= 22 (Vite 8), in a container if the host Node is older.
node22() {
    if [ "$(node_major)" -ge 22 ]; then
        bash -c "$1"
        return
    fi
    [ "${BUILD_DOCKER:-auto}" = "0" ] && die "the build needs Node >= 22 (have $(node -v))"
    command -v docker >/dev/null || die "the build needs Node >= 22 (have $(node -v)) and docker is not available"
    docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp/nodehome -e npm_config_cache=/tmp/nodehome/.npm \
        -v "$ROOT:$ROOT" -w "$PWD" node:22-slim bash -c "$1"
}

VERSION=""
stamp_version() {
    [ -n "$VERSION" ] && return 0
    local backup="$WORK/version-backup"
    mkdir -p "$backup"
    cp "$ROOT/plugin.json" "$ROOT/package.json" "$backup/"
    (cd "$ROOT" && node scripts/version.js --write >/dev/null)
    VERSION="$(node -p 'require("'"$ROOT"'/plugin.json").version')"
    step "Stamped version $VERSION"
}

# The builder writes dist/, pkg/ and the ZIPs into the plugin directory, i.e. the working tree
clean_build_outputs() {
    rm -rf "$ROOT/dist" "$ROOT/pkg"
    rm -f "$ROOT"/QualityAssurance-*.zip
}

cleanup() {
    if [ -n "$VERSION" ] && [ -f "$WORK/version-backup/plugin.json" ]; then
        cp "$WORK/version-backup/plugin.json" "$WORK/version-backup/package.json" "$ROOT/"
    fi
    clean_build_outputs
}
trap cleanup EXIT

stage_build() {
    step "Build package (Meltingplot/DuetWebControl $DWC_REF)"
    if [ -d "$DWC_DIR/.git" ]; then
        git -C "$DWC_DIR" fetch --depth 1 origin "$DWC_REF"
        git -C "$DWC_DIR" checkout --force FETCH_HEAD
    else
        git clone --depth 1 --branch "$DWC_REF" https://github.com/Meltingplot/DuetWebControl.git "$DWC_DIR"
    fi
    (cd "$DWC_DIR" && node22 "npm install")
    (cd "$ROOT" && npm ci)

    # pytest leaves bytecode behind; the builder skips __pycache__, but keep the tree clean
    find "$ROOT/dsf" -name __pycache__ -type d -prune -exec rm -rf {} +
    stamp_version
    clean_build_outputs

    (cd "$DWC_DIR" && node22 "node scripts/build-plugin-pkg.js '$ROOT'") || die "plugin build failed"
    bash "$ROOT/scripts/verify-package.sh" "$ROOT/QualityAssurance-$VERSION.zip" "$DWC_DIR"
    unzip -l "$ROOT/QualityAssurance-$VERSION.zip"
    mkdir -p "$DIST"
    rm -f "$DIST"/QualityAssurance-*.zip
    cp "$ROOT/QualityAssurance-$VERSION.zip" "$DIST/"
    ok "Built QualityAssurance-$VERSION.zip -> .ci-local/dist/"
}

stages=("$@")
[ ${#stages[@]} -eq 0 ] && stages=(all)
for s in "${stages[@]}"; do
    case "$s" in
        all)      stage_python; stage_frontend; stage_build ;;
        python)   stage_python ;;
        matrix)   stage_matrix ;;
        frontend) stage_frontend ;;
        build)    stage_build ;;
        *)        die "unknown stage: $s (python|matrix|frontend|build|all)" ;;
    esac
done

printf '\n'
ok "CI run complete: ${stages[*]}"
