#!/usr/bin/env bash
# Build the normal Release CLI in an isolated prefix, then test it without a display.
# This does not create a GUI-free executable or install a web service.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: prepare_headless_linux.sh ACTION ABSOLUTE_WORK_DIR [JOBS]

Actions, in order:
  configure-deps  Generate the dependency build; no dependency compilation.
  build-deps      Download and build Orca's own dependencies (may take hours).
  configure-app  Generate the Release app build with tools and CLI tests.
  build-app      Build OrcaSlicer and OrcaSlicer_profile_validator.
  test           Run the real RequiresApp tests without DISPLAY/Wayland.

WORK_DIR must be outside the source checkout. Keep it at the same absolute path
between runs: wx-config embeds its installation prefix. JOBS defaults to 2;
dependencies build one at a time, with JOBS compile processes within each one.
Requires Linux, CMake >= 3.22, Ninja, Python 3, Git, a C++ toolchain and the
system packages used by build_linux.sh's Ubuntu 24.04 builder. Use that builder
or an equivalent pinned container. This script does not install system packages.
Logs and toolchain/source metadata remain under WORK_DIR/logs. The test action
creates a fresh report under WORK_DIR/probes; skipped or missing tests fail.
EOF
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then usage; exit 0; fi
if [[ $# -lt 2 || $# -gt 3 ]]; then usage >&2; exit 2; fi
action=$1
work_dir=$2
jobs=${3:-2}
case "$action" in
    configure-deps|build-deps|configure-app|build-app|test) ;;
    *) usage >&2; exit 2 ;;
esac
[[ $(uname -s) == Linux ]] || { echo 'This helper supports Linux only.' >&2; exit 2; }
[[ $work_dir == /* && $jobs =~ ^[1-9][0-9]*$ ]] || { usage >&2; exit 2; }
for tool in cmake ctest ninja python3 git; do command -v "$tool" >/dev/null; done
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work_dir=$(python3 - "$source_dir" "$work_dir" <<'PY'
import pathlib, sys
source, work = (pathlib.Path(p).resolve() for p in sys.argv[1:])
if source == work or source in work.parents or work in source.parents:
    sys.exit('WORK_DIR must be separate from the source checkout and its parents.')
print(work)
PY
)
mkdir -p "$work_dir"
marker="$work_dir/.orca-headless-source"
if [[ -e $marker ]]; then
    [[ $(cat "$marker") == "$source_dir" ]] || { echo 'WORK_DIR belongs to another source checkout.' >&2; exit 2; }
else
    [[ -z $(ls -A "$work_dir") ]] || { echo 'Use a new empty WORK_DIR for the first action.' >&2; exit 2; }
    printf '%s\n' "$source_dir" > "$marker"
fi
mkdir -p "$work_dir/logs"
log=$(mktemp "$work_dir/logs/${action}.XXXXXX.log")
exec > >(tee "$log") 2>&1
trap 'rc=$?; echo "action_exit_code=$rc"' EXIT
printf 'action=%s\nsource=%s\nwork=%s\njobs=%s\n' "$action" "$source_dir" "$work_dir" "$jobs"
date -u '+utc=%Y-%m-%dT%H:%M:%SZ'
git -C "$source_dir" rev-parse HEAD HEAD:resources/profiles
git -C "$source_dir" status --short
cmake --version
ninja --version
"${CXX:-c++}" --version
df -h "$work_dir"
export CMAKE_BUILD_PARALLEL_LEVEL=$jobs
# ExternalProject patches must not discover an enclosing, unrelated Git repo.
# Configure from this directory too: deps/CMakeLists probes its current directory
# and otherwise adds --directory paths relative to the Orca source checkout.
export GIT_CEILING_DIRECTORIES="$(dirname -- "$work_dir")${GIT_CEILING_DIRECTORIES:+:$GIT_CEILING_DIRECTORIES}"
cd -- "$work_dir"
deps_dir="$work_dir/deps"
app_dir="$work_dir/app"
prefix="$deps_dir/OrcaSlicer_dep/usr/local"
run() { printf '%q ' "$@"; printf '\n'; "$@"; }

case "$action" in
    configure-deps)
        run cmake -S "$source_dir/deps" -B "$deps_dir" -G Ninja \
            -DCMAKE_BUILD_TYPE=Release -DSLIC3R_CAD=ON \
            -DDESTDIR="$deps_dir/OrcaSlicer_dep" -DDEP_DOWNLOAD_DIR="$work_dir/downloads"
        ;;
    build-deps)
        run cmake --build "$deps_dir" --parallel 1
        ;;
    configure-app)
        [[ -d $prefix ]] || { echo 'Build the isolated dependencies first.' >&2; exit 2; }
        run cmake -S "$source_dir" -B "$app_dir" -G 'Ninja Multi-Config' \
            -DCMAKE_CONFIGURATION_TYPES=Release -DCMAKE_PREFIX_PATH="$prefix" \
            -DDEP_BUILD_DIR="$deps_dir" -DSLIC3R_STATIC=ON -DSLIC3R_GUI=ON \
            -DSLIC3R_CAD=ON -DSLIC3R_PCH=OFF -DORCA_TOOLS=ON -DBUILD_TESTS=ON
        ;;
    build-app)
        run cmake --build "$app_dir" --config Release --parallel "$jobs" \
            --target OrcaSlicer OrcaSlicerSosCopy OrcaSlicer_profile_validator
        ;;
    test)
        mkdir -p "$work_dir/probes"
        probe_dir="$work_dir/probes/$(date -u +%Y%m%dT%H%M%S)-$$"
        run python3 "$source_dir/scripts/run_headless_tests.py" \
            --build-dir "$app_dir" --output-dir "$probe_dir" --config Release
        ;;
esac
