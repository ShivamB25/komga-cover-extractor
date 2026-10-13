"""Measure paired frozen-legacy and installed-wheel number parsing in the same sandbox.

Run with the installed wheel's Python 3.14 interpreter and the locked legacy
characterization dependencies. The bubblewrap namespace, generated filenames,
ordered numeric policy, interpreter and environment are identical for both sides.
This is a number-parser workload, not a book-organization or extraction benchmark.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, cast

_POLICY_FIELDS = (
    "volume_keywords", "chapter_keywords", "exclusion_keywords",
    "subtitle_exclusion_keywords",
)
_DEFAULT_SIZES = (10, 1000)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _installed_origin(source_root: Path) -> Path:
    spec = importlib.util.find_spec("komga_cover_extractor")
    if spec is None or spec.origin is None:
        raise ValueError("install the built application wheel into this interpreter first")
    origin = Path(spec.origin).resolve()
    if origin.is_relative_to(source_root.resolve()):
        raise ValueError("benchmark refuses a source-tree or editable application installation")
    distribution = importlib.metadata.distribution("komga-cover-extractor")
    direct = distribution.read_text("direct_url.json")
    if direct and json.loads(direct).get("dir_info", {}).get("editable") is True:
        raise ValueError("benchmark requires a wheel, not an editable installation")
    recorded = distribution.files
    if recorded is None or origin not in {Path(str(distribution.locate_file(item))).resolve() for item in recorded}:
        raise ValueError("resolved package is not owned by the installed wheel's file manifest")
    return origin


def _load_tool(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load required isolation tool {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module




def _numeric_policy(source: str) -> dict[str, list[str]]:
    tree = ast.parse(source)
    values: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in _POLICY_FIELDS:
                value = ast.literal_eval(node.value)
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError(f"frozen {target.id} is not an ordered string literal")
                if target.id in values:
                    raise ValueError(f"frozen {target.id} has multiple assignments")
                values[target.id] = value
    if set(values) != set(_POLICY_FIELDS):
        raise ValueError("frozen source does not provide the complete numeric policy")
    return values


def _fixtures(size: int) -> list[dict[str, object]]:
    rows = []
    for index in range(size):
        label = "".join(chr(65 + index // (26 ** power) % 26) for power in (3, 2, 1, 0))
        number = index % 997 + 1
        kind = "volume" if index % 2 == 0 else "chapter"
        token = f"v{number:02}" if kind == "volume" else f"c{number:03}"
        rows.append({"name": f"Benchmark {label} {token}.cbz", "kind": kind, "expected": number})
    return rows


def _runtime() -> dict[str, object]:
    gil = getattr(sys, "_is_gil_enabled", None)
    dependency_versions = {
        distribution.metadata["Name"]: distribution.version
        for distribution in importlib.metadata.distributions()
        if distribution.metadata["Name"]
    }
    return {
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "gil_enabled": gil() if gil is not None else None,
        "platform": platform.platform(), "machine": platform.machine(),
        "cpu_count": os.cpu_count(), "load_average": list(os.getloadavg()),
        "package_version": importlib.metadata.version("komga-cover-extractor"),
        "dependency_versions": dict(sorted(dependency_versions.items())),
    }


def _phase(
    rows: list[dict[str, Any]], parse: Callable[[str, str], Any], *,
    repeats: int, side: str, counters: dict[str, int], cache_info: Callable[[], Any] | None,
) -> dict[str, object]:
    before = cache_info() if cache_info is not None else None
    baseline = dict(counters)
    mismatches: list[dict[str, object]] = []
    digest = hashlib.sha256()
    calls = validated = failures = 0
    counters["active"] = 1
    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    try:
        for _ in range(repeats):
            for row in rows:
                calls += 1
                try:
                    number = parse(row["name"], row["kind"])
                    if side == "legacy":
                        correct = type(number) is int and number == row["expected"]
                        actual = str(number)
                    else:
                        correct = number.kind == "scalar" and number.values == (Decimal(row["expected"]),)
                        actual = str(number.values[0]) if number.kind == "scalar" else str(number.values)
                    digest.update(actual.encode("utf-8") + b"\n")
                    if correct:
                        validated += 1
                    else:
                        failures += 1
                        if len(mismatches) < 20:
                            mismatches.append({"name": row["name"], "expected": row["expected"], "actual": actual})
                except Exception as error:
                    failures += 1
                    if len(mismatches) < 20:
                        mismatches.append({"name": row["name"], "error_type": type(error).__name__})
    finally:
        cpu_seconds = (time.process_time_ns() - cpu_started) / 1_000_000_000
        elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
        counters["active"] = 0
    after = cache_info() if cache_info is not None else None
    if before is None:
        hits, misses = 0, calls
    else:
        assert after is not None
        hits, misses = after.hits - before.hits, after.misses - before.misses
    import resource

    return {
        "wall_seconds": elapsed, "cpu_seconds": cpu_seconds,
        "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "work_counts": {
            "unique_inputs": len(rows), "repetitions": repeats, "parser_api_calls": calls,
            "validated_outputs": validated, "failed_outputs": failures,
            "number_parser_evaluations": misses, "number_parser_cache_hits": hits,
            "filesystem_open_calls": counters["opens"] - baseline["opens"],
            "directory_scan_calls": counters["scans"] - baseline["scans"],
            "socket_connect_calls": counters["connects"] - baseline["connects"],
        },
        "output_sha256": digest.hexdigest(), "mismatches": mismatches,
    }


def _sandbox_child(side: str, manifest: Path, warm_repeats: int) -> int:
    if os.environ.get("KCE_CHARACTERIZATION_SANDBOX") != "1" or Path.cwd() != Path("/work"):
        raise ValueError("benchmark child may run only inside the existing characterization sandbox")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    rows = data["fixtures"]
    policy_values = data["policy"]
    captured = io.StringIO()
    errors = io.StringIO()
    setup_started = time.perf_counter_ns()
    cache_info = None
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(errors):
        if side == "legacy":
            legacy_probe = _load_tool(Path("/repo/tools/legacy_probe.py"), "_kce_benchmark_legacy_probe")

            settings = legacy_probe.extract_safe_settings("/repo/settings.py")
            if not settings.ok:
                raise ValueError("frozen safe settings could not be decoded")
            for name in (
                "log_to_file", "bookwalker_check", "watchdog_toggle",
                "uncheck_non_qbit_upgrades_toggle", "send_scan_request_to_komga_libraries_toggle",
            ):
                settings.values[name] = False
            settings.values.update({"discord_webhook_url": [], "bookwalker_webhook_urls": [], "new_volume_webhook": ""})
            arguments = sys.argv
            try:
                sys.argv = ["/repo/komga_cover_extractor.py"]
                module, detail = legacy_probe._load_main(Path("/repo"), settings, Path("/repo/settings.py"))
            finally:
                sys.argv = arguments
            if module is None:
                raise ValueError(f"frozen isolated module failed to load: {detail}")
            for name, value in policy_values.items():
                if list(getattr(module, name)) != value:
                    raise ValueError(f"legacy numeric policy differs for {name}")
            module.paths = []
            module.download_folders = []
            module.paths_with_types = []
            module.download_folders_with_types = []
            module.cached_paths = []
            module.ROOT_DIR = "/work"
            module.LOGS_DIR = "/work/logs"
            module.get_release_number.cache_clear()
            cache_info = module.get_release_number.cache_info

            def parse(name: str, kind: str) -> Any:
                return module.get_release_number_cache(name, chapter=kind == "chapter")
        else:
            from komga_cover_extractor.parsing.numbers import (
                NumberPolicy,
                parse_release_number,
            )

            origin = importlib.util.find_spec("komga_cover_extractor")
            if origin is None or origin.origin is None or Path(origin.origin).is_relative_to("/repo"):
                raise ValueError("modern benchmark did not resolve the installed wheel")
            policy = NumberPolicy(**{name: tuple(value) for name, value in policy_values.items()})

            def parse(name: str, kind: str) -> Any:
                return parse_release_number(name, kind, policy)
    setup_seconds = (time.perf_counter_ns() - setup_started) / 1_000_000_000
    frozen_hashes = {
        name: hashlib.sha256((Path("/repo") / name).read_bytes()).hexdigest()
        for name in ("komga_cover_extractor.py", "settings.py")
    }
    ready_monotonic_ns = time.perf_counter_ns()
    counters = {"active": 0, "opens": 0, "scans": 0, "connects": 0}

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if not counters["active"]:
            return
        if event == "open":
            counters["opens"] += 1
        elif event in {"os.scandir", "os.listdir"}:
            counters["scans"] += 1
        elif event == "socket.connect":
            counters["connects"] += 1
            raise RuntimeError("parser benchmark attempted a network connection")

    sys.addaudithook(audit)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(errors):
        cold = _phase(rows, parse, repeats=1, side=side, counters=counters, cache_info=cache_info)
        warm = _phase(rows, parse, repeats=warm_repeats, side=side, counters=counters, cache_info=cache_info)
    phase_counts = (
        cast(dict[str, int], cold["work_counts"]),
        cast(dict[str, int], warm["work_counts"]),
    )
    forbidden_io = any(
        counts[name]
        for counts in phase_counts
        for name in ("filesystem_open_calls", "directory_scan_calls", "socket_connect_calls")
    )
    status = "pass" if not forbidden_io and all(counts["failed_outputs"] == 0 for counts in phase_counts) else "fail"
    if captured.getvalue() or errors.getvalue():
        status = "fail"
    report = {
        "status": status, "side": side, "fixture_sha256": data["fixture_sha256"],
        "policy_sha256": data["policy_sha256"], "side_setup_wall_seconds": setup_seconds,
        "ready_monotonic_ns": ready_monotonic_ns, "frozen_source_hashes": frozen_hashes,
        "cold_unique_inputs": cold, "warm_repeated_inputs": warm,
        "unexpected_stdout": captured.getvalue()[:4096], "unexpected_stderr": errors.getvalue()[:4096],
        "runtime": _runtime(),
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if status == "pass" else 1


def benchmark(source_root: Path, *, sizes: Sequence[int], trials: int, warm_repeats: int, timeout: float) -> dict[str, object]:
    if platform.python_implementation() != "CPython" or sys.version_info[:3] != (3, 14, 6):
        raise ValueError("the paired benchmark requires CPython 3.14.6")
    if getattr(sys, "_is_gil_enabled", lambda: False)() is not True:
        raise ValueError("the paired benchmark requires the normal GIL build")
    if not sys.platform.startswith("linux"):
        raise ValueError("the frozen-isolation benchmark requires Linux bubblewrap")
    origin = _installed_origin(source_root)
    characterize_legacy = _load_tool(Path(__file__).resolve().with_name("characterize_legacy.py"), "_kce_benchmark_isolation")

    expected_root = Path(characterize_legacy.__file__).resolve().parents[1]
    if source_root.resolve() != expected_root:
        raise ValueError("source-root must be this tool's checkout with its fixed frozen allowlist")
    config = characterize_legacy._production_config()
    error = characterize_legacy._validate_config(config)
    if error is not None:
        raise ValueError(error)
    layout, error = characterize_legacy._interpreter_layout()
    if error is not None or layout is None:
        raise ValueError(error or "characterization interpreter layout is unavailable")
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise ValueError("required characterization sandbox executable bwrap is unavailable")
    fixture_root = source_root / "tests/fixtures/legacy"
    frozen_source = (fixture_root / "komga_cover_extractor.py").read_text(encoding="utf-8")
    policy = _numeric_policy(frozen_source)
    policy_hash = hashlib.sha256(_canonical(policy)).hexdigest()
    source_hashes = {
        item.source.name: hashlib.sha256(item.source.read_bytes()).hexdigest()
        for item in config.files
    }
    measurements: list[dict[str, Any]] = []
    summaries = []
    with tempfile.TemporaryDirectory(prefix="kce-parser-benchmark-") as temporary:
        manifest_path = Path(temporary) / "benchmark.json"
        child_target = PurePosixPath("/repo/tools/benchmark_refactor.py")
        child_config = replace(config, probe_target=child_target, files=(*config.files,
            characterize_legacy._AllowlistedFile(Path(__file__).resolve(), child_target),
            characterize_legacy._AllowlistedFile(manifest_path, PurePosixPath("/repo/benchmark.json")),
        ))
        for size in sizes:
            fixtures = _fixtures(size)
            fixture_hash = hashlib.sha256(_canonical(fixtures)).hexdigest()
            manifest_path.write_bytes(_canonical({"fixtures": fixtures, "fixture_sha256": fixture_hash,
                                                   "policy": policy, "policy_sha256": policy_hash}))
            error = characterize_legacy._validate_config(child_config)
            if error is not None:
                raise ValueError(error)
            command, error = characterize_legacy._sandbox_command(child_config, layout, bwrap)
            if error is not None or command is None:
                raise ValueError(error or "cannot construct the existing sandbox")
            command = [*command[:-3], "--setenv", "TZ", "UTC", "--setenv", "PYTHONHASHSEED", "0", *command[-3:]]
            pairs: dict[int, dict[str, dict[str, Any]]] = {}
            for trial in range(trials):
                order = ("legacy", "modern") if trial % 2 == 0 else ("modern", "legacy")
                pairs[trial] = {}
                for side in order:
                    started = time.perf_counter_ns()
                    code, stdout, stderr, timed_out, failure = characterize_legacy._run_process(
                        [*command, "--sandbox-child", side, "--manifest", "/repo/benchmark.json",
                         "--warm-repeats", str(warm_repeats)], timeout,
                    )
                    total = (time.perf_counter_ns() - started) / 1_000_000_000
                    if timed_out or failure is not None or code != 0:
                        raise ValueError(f"{side} sandbox failed with exit {code}: {failure or stderr.decode('utf-8', errors='replace')[:4096]}")
                    result = json.loads(stdout)
                    if result.get("status") != "pass" or stderr:
                        raise ValueError(f"{side} benchmark produced invalid work or diagnostics: {result}")
                    if result["fixture_sha256"] != fixture_hash or result["policy_sha256"] != policy_hash:
                        raise ValueError("paired child did not use the exact fixture and policy")
                    if result["frozen_source_hashes"] != {name: source_hashes[name] for name in ("komga_cover_extractor.py", "settings.py")}:
                        raise ValueError("sandbox did not read the exact frozen source files")
                    startup = (result["ready_monotonic_ns"] - started) / 1_000_000_000
                    if not 0 <= startup <= total:
                        raise ValueError("sandbox monotonic clock is not shared with the benchmark supervisor")
                    result.update({"size": size, "trial": trial + 1, "sequence": len(measurements) + 1,
                                   "sandbox_process_wall_seconds": total, "cold_startup_wall_seconds": startup})
                    measurements.append(result)
                    pairs[trial][side] = result
                left, right = pairs[trial]["legacy"], pairs[trial]["modern"]
                if left["runtime"] != right["runtime"]:
                    # Load average is sampled during each process, not an invariant.
                    differing = {key for key in left["runtime"] if key != "load_average" and left["runtime"][key] != right["runtime"][key]}
                    if differing:
                        raise ValueError(f"paired runtime differs for {sorted(differing)}")
                for phase_name in ("cold_unique_inputs", "warm_repeated_inputs"):
                    if left[phase_name]["output_sha256"] != right[phase_name]["output_sha256"]:
                        raise ValueError("paired parser outputs differ")
                    for key in ("parser_api_calls", "validated_outputs", "failed_outputs"):
                        if left[phase_name]["work_counts"][key] != right[phase_name]["work_counts"][key]:
                            raise ValueError(f"paired workload differs for {key}")
            for side in ("legacy", "modern"):
                samples = [pair[side] for pair in pairs.values()]
                summary: dict[str, object] = {"size": size, "side": side, "trials": trials, "fixture_sha256": fixture_hash}
                for key in ("cold_startup_wall_seconds", "side_setup_wall_seconds", "sandbox_process_wall_seconds"):
                    values = [sample[key] for sample in samples]
                    summary[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
                for phase_name in ("cold_unique_inputs", "warm_repeated_inputs"):
                    values = [sample[phase_name]["wall_seconds"] for sample in samples]
                    rss = [sample[phase_name]["process_peak_rss_bytes"] for sample in samples]
                    summary[phase_name] = {
                        "wall_seconds": {"median": statistics.median(values), "min": min(values), "max": max(values)},
                        "process_peak_rss_bytes": {"median": statistics.median(rss), "min": min(rss), "max": max(rss)},
                        "work_counts_per_trial": samples[0][phase_name]["work_counts"],
                    }
                summaries.append(summary)
    return {
        "schema_version": 1, "status": "pass", "acceptance_ids": ["D10"],
        "workload": "generated integral scalar release-number parsing, alternating volume and chapter filenames",
        "runtime": _runtime(), "installed_origin": str(origin), "source_hashes": source_hashes,
        "policy": policy, "policy_sha256": policy_hash,
        "environment": {"sandbox": "existing characterize_legacy._sandbox_command", "network": "unshared",
                        "cwd": "/work", "HOME": "/work/home", "TMPDIR": "/tmp", "LANG": "C.UTF-8",
                        "TZ": "UTC", "PYTHONHASHSEED": "0", "python_flags": ["-B"],
                        "isolation": "read-only allowlisted files and interpreter; no checkout application package mounted"},
        "summaries": summaries, "measurements": measurements,
        "limitations": [
            "No full application pipeline, archive decoding, image processing, filesystem publication or remote work is timed.",
            "Fixtures are generated filename observations, not 10 or 1000 decoded books and not full-corpus parity evidence.",
            "Cold phases use one pass of unique inputs in a fresh process. Measured cold startup includes namespace/interpreter/tool startup, manifest reading, frozen provenance hashing and side setup before numeric work. Side setup and entire process wall time are also reported separately.",
            "Warm phases issue identical API calls but preserve the legacy LRU cache and the modern uncached number API. Cache hits and actual evaluations are reported, not compared as equal parser work.",
            "RSS is Linux process-lifetime high-water RSS including imports, not incremental phase allocation or a whole process-tree measurement.",
            "Wall time includes output normalization and validation for both sides. No profiling or end-to-end fraction is measured, so no application speedup verdict is made.",
            "Load average is recorded per child. Both sides alternate on the same host but external scheduler and thermal noise are not eliminated.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--sizes", type=int, nargs="+", default=list(_DEFAULT_SIZES))
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--warm-repeats", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sandbox-child", choices=("legacy", "modern"), help=argparse.SUPPRESS)
    parser.add_argument("--manifest", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.warm_repeats <= 1000:
            raise ValueError("warm-repeats must be between 1 and 1000")
        if args.sandbox_child is not None:
            if args.manifest != Path("/repo/benchmark.json"):
                raise ValueError("benchmark child requires its fixed sandbox manifest")
            return _sandbox_child(args.sandbox_child, args.manifest, args.warm_repeats)
        if not 5 <= args.trials <= 100 or not 0 < args.timeout <= 3600:
            raise ValueError("use 5 to 100 paired trials and a positive timeout no greater than 3600 seconds")
        if not args.sizes or any(not 1 <= size <= 1000 for size in args.sizes) or len(set(args.sizes)) != len(args.sizes):
            raise ValueError("sizes must be distinct positive counts no greater than 1000")
        if args.output is not None and (args.output.exists() or args.output.is_symlink()):
            raise ValueError("benchmark output must be a new explicitly named file")
        report = benchmark(args.source_root.resolve(), sizes=args.sizes, trials=args.trials,
                           warm_repeats=args.warm_repeats, timeout=args.timeout)
        if args.output is not None:
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        print(json.dumps(report, sort_keys=True))
        return 0
    except (OSError, ValueError, ImportError, RuntimeError) as error:
        print(json.dumps({"schema_version": 1, "status": "blocked", "error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
