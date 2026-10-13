#!/bin/sh
set -eu

root=$(realpath "$(dirname "$0")/..")
if [ "$#" -ne 1 ] || [ "${1#/}" = "$1" ]; then
    printf '%s\n' 'Usage: sh tools/verify_deployment.sh ABSOLUTE_NEW_OUTPUT_DIRECTORY' >&2
    exit 2
fi
out=$(realpath -m "$1")
case "$out" in
    "$root"|"$root"/*)
        printf '%s\n' 'Deployment proof output and wheel environments must be outside the checkout.' >&2
        exit 2
        ;;
esac
mkdir "$out"
mkdir "$out/home"
export HOME="$out/home"
export PYTHONUNBUFFERED=1
cd "$root"

uv sync --locked --all-extras --group legacy
uv run --locked --no-sync python tools/export_requirements.py --check
uv run --locked --no-sync ruff check src tools tests --exclude tests/fixtures/legacy
uv run --locked --no-sync mypy src tools/export_requirements.py tools/verify_distribution.py tools/verify_installed.py tools/verify_container.py

offline() {
    sudo --preserve-env=PATH,HOME,UV_CACHE_DIR,PYTHONUNBUFFERED,LD_LIBRARY_PATH unshare --net /bin/sh -eu -c '
        ip link set lo up
        uid=$1
        gid=$2
        shift 2
        exec setpriv --reuid "$uid" --regid "$gid" --init-groups "$@"
    ' sh "$(id -u)" "$(id -g)" "$@"
}
offline uv run --locked --no-sync --offline pytest tests --junitxml "$out/tests.xml"
set +e
offline uv run --locked --no-sync --offline python tools/characterize_legacy.py --mode all --output "$out/legacy.json" > "$out/legacy-stdout.json"
legacy_status=$?
set -e
if [ "$legacy_status" -ne 1 ]; then
    printf '%s\n' 'Frozen characterization must reproduce its known exit 1 before its report is accepted.' >&2
    exit 1
fi
uv run --locked --no-sync python tools/verify_distribution.py legacy "$out/legacy.json" > "$out/legacy-proof.json"
offline uv run --locked --no-sync --offline python tools/characterize_modern.py --output "$out/modern.json"
uv run --locked --no-sync python tools/verify_distribution.py modern "$out/modern.json" > "$out/modern-proof.json"
uv build --python "$root/.venv/bin/python" --no-build-isolation --out-dir "$out/dist"
uv run --locked --no-sync python tools/verify_distribution.py package "$out/dist" > "$out/distribution-proof.json"
uv venv --python 3.14 "$out/wheel-env"
uv pip sync --python "$out/wheel-env/bin/python" --require-hashes requirements.txt
set -- "$out/dist/"*.whl
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    printf '%s\n' 'Expected one freshly built wheel.' >&2
    exit 1
fi
wheel=$1
uv pip install --python "$out/wheel-env/bin/python" --no-deps "$wheel[all]"
uv pip check --python "$out/wheel-env/bin/python"
cd "$out"
offline "$out/wheel-env/bin/python" -I "$root/tools/verify_installed.py" \
    --source-root "$root" --expect-extra all --dependency-lock "$root/requirements.txt" > "$out/installed-proof.json"
for extra in core covers watch image-similarity conversion integrations; do
    dependency_lock="$out/requirements-$extra.txt"
    set -- --locked --format requirements.txt --no-default-groups --no-emit-project \
        --no-editable --no-header --no-annotate --output-file "$dependency_lock"
    if [ "$extra" != core ]; then
        set -- "$@" --extra "$extra"
    fi
    uv export --project "$root" "$@"
    environment="$out/$extra-env"
    uv venv --python "$out/wheel-env/bin/python" "$environment"
    uv pip sync --python "$environment/bin/python" --require-hashes "$dependency_lock"
    if [ "$extra" = core ]; then
        uv pip install --python "$environment/bin/python" --no-deps "$wheel"
    else
        uv pip install --python "$environment/bin/python" --no-deps "$wheel[$extra]"
    fi
    uv pip check --python "$environment/bin/python"
    set -- --source-root "$root" --capabilities-only --dependency-lock "$dependency_lock"
    if [ "$extra" != core ]; then
        set -- "$@" --expect-extra "$extra"
    fi
    offline "$environment/bin/python" -I "$root/tools/verify_installed.py" "$@" > "$out/installed-$extra-proof.json"
done
printf 'All-capability, fresh core and five isolated owning-extra proofs are in %s.\n' "$out"
