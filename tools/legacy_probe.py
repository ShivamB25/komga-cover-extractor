"""Run the original parser assertions and corpus in a disposable sandbox.

This module intentionally has no dependency on the legacy application at import time.  The
legacy module is loaded only by :func:`run_probe`, after the caller has proved that it is the
sandbox child and supplied the read-only ``/repo`` plus writable ``/work`` layout.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import csv
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
import platform
import sys
import tempfile
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SANDBOX_MARKER = "KCE_CHARACTERIZATION_SANDBOX"
SCHEMA_VERSION = 1
EXPECTED_CORPUS_ROWS = 44_714
EXPECTED_EXCLUSIONS = 249
ORIGINAL_FILES = (
    "komga_cover_extractor.py",
    "settings.py",
    "tests.py",
    "manga_novel_dataset.csv",
)
DEPENDENCY_NAMES = (
    "beautifulsoup4",
    "discord-webhook",
    "filetype",
    "lxml",
    "numpy",
    "opencv-python-headless",
    "pillow",
    "py7zr",
    "rarfile",
    "regex",
    "requests",
    "scandir",
    "scikit-image",
    "titlecase",
    "Unidecode",
    "watchdog",
)


class SafeKeyword:
    """The reviewed, data-only equivalent of settings.Keyword."""

    __slots__ = ("name", "score", "file_type")

    def __init__(self, name: str, score: int | float, file_type: str = "both") -> None:
        self.name = name
        self.score = score
        self.file_type = file_type

    def __str__(self) -> str:
        return f"Name: {self.name}, Score: {self.score}, File Type: {self.file_type}"

    def __repr__(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class SettingsIssue:
    name: str
    line: int
    reason: str
    expression: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "line": self.line,
            "reason": self.reason,
            "expression": self.expression,
        }


@dataclass(slots=True)
class SafeSettings:
    """Result of parsing settings without executing any settings code."""

    values: dict[str, object]
    rejected: list[SettingsIssue]
    source_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.source_error is None and not self.rejected

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "values": sorted(self.values),
            "rejected": [issue.as_dict() for issue in self.rejected],
            "source_error": self.source_error,
        }


class _LiteralError(ValueError):
    pass


def _expression_text(source: str, node: ast.AST) -> str:
    text = ast.get_source_segment(source, node)
    return text if text is not None else ast.dump(node, include_attributes=False)


def _decode_keyword(node: ast.Call, source: str, depth: int) -> SafeKeyword:
    if depth > 32:
        raise _LiteralError("literal nesting exceeds the safe limit")
    if not isinstance(node.func, ast.Name) or node.func.id != "Keyword":
        raise _LiteralError("only the reviewed Keyword constructor is supported")
    if any(isinstance(argument, ast.Starred) for argument in node.args):
        raise _LiteralError("starred Keyword arguments are not supported")
    if any(keyword.arg is None for keyword in node.keywords):
        raise _LiteralError("**Keyword arguments are not supported")

    values: dict[str, object] = {}
    positional_names = ("name", "score", "file_type")
    if len(node.args) > len(positional_names):
        raise _LiteralError("Keyword accepts at most name, score and file_type")
    for positional_name, argument in zip(positional_names, node.args):
        values[positional_name] = _decode_literal(argument, source, depth + 1)
    for keyword in node.keywords:
        assert keyword.arg is not None
        if keyword.arg not in positional_names:
            raise _LiteralError(f"unsupported Keyword field {keyword.arg!r}")
        if keyword.arg in values:
            raise _LiteralError(f"duplicate Keyword field {keyword.arg!r}")
        values[keyword.arg] = _decode_literal(keyword.value, source, depth + 1)

    if "name" not in values or "score" not in values:
        raise _LiteralError("Keyword requires literal name and score")
    name_value = values["name"]
    score_value = values["score"]
    file_type_value = values.get("file_type", "both")
    if not isinstance(name_value, str):
        raise _LiteralError("Keyword.name must be a string literal")
    if isinstance(score_value, bool) or not isinstance(score_value, (int, float)):
        raise _LiteralError("Keyword.score must be a numeric literal")
    if isinstance(score_value, float) and not math.isfinite(score_value):
        raise _LiteralError("Keyword.score must be finite")
    if not isinstance(file_type_value, str) or file_type_value not in {
        "chapter",
        "volume",
        "both",
    }:
        raise _LiteralError("Keyword.file_type must be chapter, volume or both")
    return SafeKeyword(name_value, score_value, file_type_value)


def _decode_literal(node: ast.AST, source: str, depth: int = 0) -> object:
    """Decode only literal AST nodes and the reviewed Keyword call shape.

    In particular, names, attribute access, comprehensions, binary operations, f-strings and
    every other call are rejected.  No Python expression is compiled or evaluated here.
    """

    if depth > 32:
        raise _LiteralError("literal nesting exceeds the safe limit")
    if isinstance(node, ast.Constant):
        literal_value = node.value
        if literal_value is None or isinstance(
            literal_value, (bool, int, float, str, bytes)
        ):
            if isinstance(literal_value, float) and not math.isfinite(literal_value):
                raise _LiteralError("non-finite float literal is not supported")
            return literal_value
        raise _LiteralError(
            f"constant type {type(literal_value).__name__} is not supported"
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        numeric_value = _decode_literal(node.operand, source, depth + 1)
        if isinstance(numeric_value, bool) or not isinstance(
            numeric_value, (int, float)
        ):
            raise _LiteralError("numeric unary operators require numeric literals")
        return +numeric_value if isinstance(node.op, ast.UAdd) else -numeric_value
    if isinstance(node, ast.List):
        return [_decode_literal(item, source, depth + 1) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_decode_literal(item, source, depth + 1) for item in node.elts)
    if isinstance(node, ast.Set):
        set_values = [_decode_literal(item, source, depth + 1) for item in node.elts]
        try:
            return set(set_values)
        except TypeError as exc:
            raise _LiteralError("set members must be hashable literals") from exc
    if isinstance(node, ast.Dict):
        result: dict[object, object] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                raise _LiteralError("dictionary unpacking is not supported")
            decoded_key = _decode_literal(key_node, source, depth + 1)
            decoded_value = _decode_literal(value_node, source, depth + 1)
            try:
                result[decoded_key] = decoded_value
            except TypeError as exc:
                raise _LiteralError(
                    "dictionary keys must be hashable literals"
                ) from exc
        return result
    if isinstance(node, ast.Call):
        return _decode_keyword(node, source, depth)
    raise _LiteralError(f"expression {type(node).__name__} is not a safe literal")


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    return []


def _is_reviewed_keyword_class(node: ast.ClassDef) -> bool:
    if (
        node.bases
        or node.decorator_list
        or node.keywords
        or getattr(node, "type_params", [])
    ):
        return False
    methods = [item for item in node.body if isinstance(item, ast.FunctionDef)]
    if [method.name for method in methods] != [
        "__init__",
        "__str__",
        "__repr__",
    ]:
        return False
    if len(node.body) != 3 or len(methods) != 3:
        return False
    init, to_string, to_repr = methods
    if any(method.decorator_list for method in (init, to_string, to_repr)):
        return False
    init_args = init.args
    if (
        init_args.posonlyargs
        or [arg.arg for arg in init_args.args] != ["self", "name", "score", "file_type"]
        or init_args.vararg is not None
        or init_args.kwarg is not None
        or init_args.kwonlyargs
        or len(init_args.defaults) != 1
        or not isinstance(init_args.defaults[0], ast.Constant)
        or init_args.defaults[0].value != "both"
    ):
        return False
    return (
        [arg.arg for arg in (*to_string.args.posonlyargs, *to_string.args.args)]
        == ["self"]
        and [arg.arg for arg in (*to_repr.args.posonlyargs, *to_repr.args.args)]
        == ["self"]
        and not to_string.args.defaults
        and not to_repr.args.defaults
        and to_string.args.vararg is None
        and to_string.args.kwarg is None
        and to_repr.args.vararg is None
        and to_repr.args.kwarg is None
    )


def parse_safe_settings(source: str, *, filename: str = "settings.py") -> SafeSettings:
    """Parse reviewed literal settings without importing or evaluating ``settings.py``."""

    try:
        tree = ast.parse(source, filename=filename, mode="exec")
    except (SyntaxError, ValueError) as exc:
        issue = SettingsIssue(
            "<module>",
            int(getattr(exc, "lineno", 0) or 0),
            f"settings source is not valid Python: {type(exc).__name__}",
            str(exc),
        )
        return SafeSettings({}, [issue])

    values: dict[str, object] = {}
    rejected: list[SettingsIssue] = []
    for statement in tree.body:
        # The real settings file has a reviewed Keyword class. Its body is never executed;
        # constructors are decoded into SafeKeyword instances below.
        if isinstance(statement, ast.ClassDef) and statement.name == "Keyword":
            if not _is_reviewed_keyword_class(statement):
                rejected.append(
                    SettingsIssue(
                        "Keyword",
                        statement.lineno,
                        "Keyword class shape is not reviewed",
                        _expression_text(source, statement),
                    )
                )
            continue
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Expr) and isinstance(
            statement.value, ast.Constant
        ):
            if isinstance(statement.value.value, str):
                continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets: Sequence[ast.expr]
            value_node: ast.AST | None
            if isinstance(statement, ast.Assign):
                targets = statement.targets
                value_node = statement.value
            else:
                targets = [statement.target]
                value_node = statement.value
            names = [name for target in targets for name in _target_names(target)]
            if value_node is None or not names or len(names) != len(targets):
                rejected.append(
                    SettingsIssue(
                        ",".join(names) or "<assignment>",
                        statement.lineno,
                        "only simple-name assignments are supported",
                        _expression_text(source, statement),
                    )
                )
                continue
            try:
                decoded = _decode_literal(value_node, source)
            except _LiteralError as exc:
                for name in names:
                    # A rejected later assignment must not leave an earlier value available to
                    # callers as though it were the final setting.
                    values.pop(name, None)
                    rejected.append(
                        SettingsIssue(
                            name,
                            statement.lineno,
                            str(exc),
                            _expression_text(source, value_node),
                        )
                    )
                continue
            for name in names:
                values[name] = decoded
            continue
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            reason = "imports are not executed during safe settings migration"
        elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            reason = "callable definitions are not settings data"
        else:
            reason = f"top-level {type(statement).__name__} is not supported"
        rejected.append(
            SettingsIssue(
                "<module>",
                getattr(statement, "lineno", 0),
                reason,
                _expression_text(source, statement),
            )
        )
    return SafeSettings(values, rejected)


def extract_safe_settings(path: str | os.PathLike[str]) -> SafeSettings:
    """Read and safely parse one settings source; never imports it."""

    source_path = Path(path)
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return SafeSettings(
            {},
            [],
            source_error=f"cannot read {source_path}: {type(exc).__name__}: {exc}",
        )
    return parse_safe_settings(source, filename=str(source_path))


def _settings_module(report: SafeSettings, path: Path) -> types.ModuleType:
    module = types.ModuleType("settings")
    module.__file__ = str(path)
    module.__package__ = ""
    module.__dict__["Keyword"] = SafeKeyword
    for name, value in report.values.items():
        setattr(module, name, value)
    return module


class _BoundedCapture(io.TextIOBase):
    """Text stream retaining only a bounded diagnostic prefix."""

    def __init__(self, limit: int = 16_384) -> None:
        self.limit = limit
        self._chunks: list[str] = []
        self._size = 0
        self.dropped = 0

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        remaining = self.limit - self._size
        if remaining > 0:
            kept = text[:remaining]
            self._chunks.append(kept)
            self._size += len(kept)
        self.dropped += max(0, len(text) - max(remaining, 0))
        return len(text)

    def getvalue(self) -> str:
        return "".join(self._chunks)

    def flush(self) -> None:
        return None


def _diagnostic_text(
    exc: BaseException | None, output: _BoundedCapture, error: _BoundedCapture
) -> str:
    pieces: list[str] = []
    if exc is not None:
        message = str(exc)
        pieces.append(f"{type(exc).__name__}{(': ' + message) if message else ''}")
    stdout = output.getvalue()
    stderr = error.getvalue()
    if stdout:
        pieces.append(f"stdout={stdout!r}")
    if stderr:
        pieces.append(f"stderr={stderr!r}")
    dropped = output.dropped + error.dropped
    if dropped:
        pieces.append(f"diagnostics_truncated={dropped}")
    return "; ".join(pieces) or "completed without diagnostics"


def _invoke(function: Callable[..., object]) -> tuple[str, str]:
    output = _BoundedCapture()
    error = _BoundedCapture()
    exception: BaseException | None = None
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
        try:
            function()
        except (
            BaseException
        ) as exc:  # one bad legacy assertion must not stop the inventory
            exception = exc
    status = "fail" if exception is not None else "pass"
    return status, _diagnostic_text(exception, output, error)


def _source_hashes(repo: Path) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for relative in ORIGINAL_FILES:
        path = repo / relative
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[relative] = digest.hexdigest()
        except OSError:
            result[relative] = None
    return result


def _dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in DEPENDENCY_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
        except Exception as exc:
            versions[name] = f"error:{type(exc).__name__}"
    return versions


def _empty_result(
    mode: str, detail: str, *, status: str = "blocked"
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "source_hashes": {},
        "dependency_versions": {},
        "tests": [{"name": "probe", "status": status, "detail": detail}],
        "corpus": None,
        "adaptations": [],
    }


def _parse_mode(arguments: Sequence[str]) -> tuple[str | None, str | None]:
    mode = "all"
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-h", "--help"}:
            return None, "usage: legacy_probe.py [--mode fast|corpus|all]"
        if argument == "--mode":
            index += 1
            if index >= len(arguments):
                return None, "--mode requires fast, corpus or all"
            mode = arguments[index]
        elif argument.startswith("--mode="):
            mode = argument.split("=", 1)[1]
        else:
            return None, f"unsupported argument {argument!r}"
        index += 1
    if mode not in {"fast", "corpus", "all"}:
        return None, f"unsupported mode {mode!r}"
    return mode, None


def _assert_nodes(
    tree: ast.Module,
) -> tuple[list[ast.FunctionDef], list[ast.FunctionDef]]:
    asserted: list[ast.FunctionDef] = []
    non_asserted: list[ast.FunctionDef] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        target = (
            asserted
            if any(isinstance(child, ast.Assert) for child in ast.walk(node))
            else non_asserted
        )
        target.append(node)
    return asserted, non_asserted


def _runner_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        if not (
            isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__"
        ):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                names.add(child.func.id)
    return names


def _exec_function(
    node: ast.FunctionDef,
    namespace: dict[str, object],
    *,
    filename: str,
    name: str | None = None,
) -> Callable[..., object]:
    function_name = name or node.name
    copied = copy.deepcopy(node)
    if copied.name != function_name:
        copied.name = function_name
    module = ast.Module(body=[copied], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, filename, "exec")
    exec(code, namespace)
    candidate = namespace.get(function_name)
    if not callable(candidate):
        raise TypeError(f"compiled {function_name!r} is not callable")
    return candidate


def _load_main(
    repo: Path, settings: SafeSettings, settings_path: Path
) -> tuple[types.ModuleType | None, str]:
    source_path = repo / "komga_cover_extractor.py"
    module_name = f"_legacy_probe_main_{os.getpid()}_{id(settings)}"
    synthetic_settings = _settings_module(settings, settings_path)
    previous_settings = sys.modules.get("settings")
    sys.modules["settings"] = synthetic_settings
    module: types.ModuleType | None = None
    output = _BoundedCapture()
    error = _BoundedCapture()
    exception: BaseException | None = None
    try:
        spec = importlib.util.spec_from_file_location(module_name, source_path)
        if spec is None or spec.loader is None:
            return (
                None,
                "cannot construct an import specification for the original main",
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            try:
                spec.loader.exec_module(module)
            except BaseException as exc:
                exception = exc
    except BaseException as exc:
        exception = exc
    finally:
        sys.modules.pop(module_name, None)
        if previous_settings is None:
            sys.modules.pop("settings", None)
        else:
            sys.modules["settings"] = previous_settings
    if exception is not None:
        return None, _diagnostic_text(exception, output, error)
    if module is None:
        return None, "original main import produced no module"
    return module, _diagnostic_text(None, output, error)


def _fixture_root(work: Path) -> tempfile.TemporaryDirectory[str]:
    # The basename mirrors the checkout root so root-fallback parsing remains comparable while
    # every actual file operation is directed below /work.
    holder = tempfile.TemporaryDirectory(prefix="legacy-probe-", dir=str(work))
    (Path(holder.name) / "komga-cover-extractor").mkdir()
    return holder


def _test_entry(
    name: str, status: str, detail: str, **extra: object
) -> dict[str, object]:
    entry: dict[str, object] = {"name": name, "status": status, "detail": detail}
    entry.update(extra)
    return entry


def _blocked_test_entries(
    asserted: Iterable[ast.FunctionDef],
    non_asserted: Iterable[ast.FunctionDef],
    detail: str,
) -> list[dict[str, object]]:
    result = [_test_entry(node.name, "blocked", detail) for node in asserted]
    result.extend(_test_entry(node.name, "blocked", detail) for node in non_asserted)
    return result


def _run_fast(
    repo: Path,
    work: Path,
    settings_report: SafeSettings,
    tests_source: str,
    tests_tree: ast.Module,
    asserted: list[ast.FunctionDef],
    non_asserted: list[ast.FunctionDef],
) -> tuple[list[dict[str, object]], list[dict[str, object]], str | None]:
    holder = _fixture_root(work)
    fixture_root = Path(holder.name) / "komga-cover-extractor"
    settings_path = repo / "settings.py"
    module, import_detail = _load_main(repo, settings_report, settings_path)
    if module is None:
        holder.cleanup()
        return (
            _blocked_test_entries(asserted, non_asserted, import_detail),
            [],
            import_detail,
        )

    # Explicitly fence all state used by pure tests.  No original test that mutates download roots
    # is invoked; its absence is reported below as an intentional not_asserted exercise.
    setattr(module, "paths", [])
    setattr(module, "download_folders", [])
    setattr(module, "ROOT_DIR", str(fixture_root))
    setattr(module, "LOGS_DIR", str(fixture_root / "logs"))
    setattr(module, "ADDONS_DIR", str(fixture_root / "addons"))
    setattr(
        module, "cached_paths_path", str(fixture_root / "logs" / "cached_paths.txt")
    )
    setattr(module, "paths_with_types", [])
    setattr(module, "download_folders_with_types", [])
    setattr(module, "cached_paths", [])

    namespace = dict(module.__dict__)
    runner_names = _runner_names(tests_tree)
    entries: list[dict[str, object]] = []
    for node in asserted:
        try:
            function = _exec_function(node, namespace, filename=str(repo / "tests.py"))
            status, detail = _invoke(function)
        except BaseException as exc:
            status, detail = (
                "blocked",
                f"cannot compile original assertion function: {type(exc).__name__}: {exc}",
            )
        if node.name not in runner_names:
            detail = f"dormant original runner entry; {detail}"
        entries.append(_test_entry(node.name, status, detail))

    # The original no-assertion exercises are deliberately not invoked.  rename_files would read
    # a missing /dl tree and logs/test_file_list.txt, while sorting discards its result and asserts
    # nothing.  Reporting them is safer and more truthful than turning a no-op into a pass.
    no_assertion_detail = (
        "no assertions in original function; not invoked. The rename companion requires missing "
        "/dl/Manga & Novels and logs/test_file_list.txt fixtures; the sorting companion discards "
        "its sorted result."
    )
    entries.extend(
        _test_entry(node.name, "not_asserted", no_assertion_detail)
        for node in non_asserted
    )

    adaptations: list[dict[str, object]] = []
    # Adaptation 1: the source test expects ignored_folder_names=['test'], while safe settings keep
    # the unchanged default [] for the raw result.
    adapted_module, adapted_import_detail = _load_main(
        repo, settings_report, settings_path
    )
    if adapted_module is not None:
        setattr(adapted_module, "ignored_folder_names", ["test"])
        adapted_namespace = dict(adapted_module.__dict__)
        ignored_node = next(
            (node for node in asserted if node.name == "test_remove_ignored_folders"),
            None,
        )
        if ignored_node is not None:
            try:
                function = _exec_function(
                    ignored_node, adapted_namespace, filename=str(repo / "tests.py")
                )
                status, detail = _invoke(function)
            except BaseException as exc:
                status, detail = (
                    "blocked",
                    f"cannot compile adaptation: {type(exc).__name__}: {exc}",
                )
            detail = f"adapted fixture policy ignored_folder_names=['test']; {detail}"
            entries.append(
                _test_entry(
                    "adapted:test_remove_ignored_folders", status, detail, adapted=True
                )
            )
            adaptations.append(
                {
                    "name": "test_remove_ignored_folders",
                    "status": status,
                    "detail": detail,
                }
            )
        else:
            adaptations.append(
                {
                    "name": "test_remove_ignored_folders",
                    "status": "blocked",
                    "detail": "original function definition was not found",
                }
            )
    else:
        adaptations.append(
            {
                "name": "test_remove_ignored_folders",
                "status": "blocked",
                "detail": f"fresh adapted main import blocked: {adapted_import_detail}",
            }
        )

    # Adaptation 2: preserve every expected assertion but repair only the known positional binding
    # (name, root, chapter_number).  The raw function above remains untouched and independently
    # records its signature failures.
    chapter_node = next(
        (node for node in asserted if node.name == "test_get_series_name_from_chapter"),
        None,
    )
    adapted_module, adapted_import_detail = _load_main(
        repo, settings_report, settings_path
    )
    if chapter_node is not None and adapted_module is not None:
        adapted_namespace = dict(adapted_module.__dict__)
        chapter_copy = copy.deepcopy(chapter_node)
        inserted = 0
        for child in ast.walk(chapter_copy):
            if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Name):
                continue
            if child.func.id != "get_series_name_from_chapter" or len(child.args) != 2:
                continue
            root_argument = ast.Constant(value=str(fixture_root))
            ast.copy_location(root_argument, child.args[0])
            child.args.insert(1, root_argument)
            inserted += 1
        if inserted == 0:
            adaptations.append(
                {
                    "name": "test_get_series_name_from_chapter",
                    "status": "blocked",
                    "detail": "no two-positional-argument calls were available to rebind",
                }
            )
        else:
            try:
                function = _exec_function(
                    chapter_copy,
                    adapted_namespace,
                    filename=str(repo / "tests.py"),
                )
                status, detail = _invoke(function)
            except BaseException as exc:
                status, detail = (
                    "blocked",
                    f"cannot compile adaptation: {type(exc).__name__}: {exc}",
                )
            detail = (
                "adapted fixture call binding name, fixture-root and chapter_number for "
                f"{inserted} original calls; {detail}"
            )
            entries.append(
                _test_entry(
                    "adapted:test_get_series_name_from_chapter",
                    status,
                    detail,
                    adapted=True,
                )
            )
            adaptations.append(
                {
                    "name": "test_get_series_name_from_chapter",
                    "status": status,
                    "detail": detail,
                }
            )
    elif chapter_node is None:
        adaptations.append(
            {
                "name": "test_get_series_name_from_chapter",
                "status": "blocked",
                "detail": "original function definition was not found",
            }
        )
    else:
        adaptations.append(
            {
                "name": "test_get_series_name_from_chapter",
                "status": "blocked",
                "detail": f"fresh adapted main import blocked: {adapted_import_detail}",
            }
        )
    holder.cleanup()
    return entries, adaptations, None


def _skip_list(tests_tree: ast.Module, source: str) -> list[str] | None:
    for node in tests_tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "validate_csv":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "skipped_files"
                for target in child.targets
            ):
                continue
            try:
                value = _decode_literal(child.value, source)
            except _LiteralError:
                return None
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return value
            return None
    return None


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return repr(value)


def _mismatch(
    row: int, file_name: str, field: str, expected: object, actual: object
) -> dict[str, object]:
    return {
        "row": row,
        "file_name": file_name,
        "field": field,
        "expected": _json_value(expected),
        "actual": _json_value(actual),
    }


def _blocked_corpus(detail: str) -> dict[str, object]:
    return {
        "status": "blocked",
        "rows": 0,
        "processed_rows": 0,
        "validated_rows": 0,
        "skipped_rows": 0,
        "skipped_files": [],
        "mismatches": [],
        "mismatch_counts": {},
        "detail": detail,
    }


def _run_corpus(
    repo: Path,
    main_module: types.ModuleType,
    fixture_root: Path,
    tests_tree: ast.Module,
    tests_source: str,
) -> dict[str, object]:
    csv_path = repo / "manga_novel_dataset.csv"
    skip_files = _skip_list(tests_tree, tests_source)
    if skip_files is None:
        return _blocked_corpus(
            "could not safely extract the original skipped_files literal"
        )
    if not csv_path.is_file():
        return _blocked_corpus(f"missing corpus prerequisite: {csv_path}")
    if len(skip_files) != EXPECTED_EXCLUSIONS or len(set(skip_files)) != len(
        skip_files
    ):
        exclusion_detail = (
            f"original exclusion literal has {len(skip_files)} entries (expected "
            f"{EXPECTED_EXCLUSIONS}) or duplicate names"
        )
    else:
        exclusion_detail = ""

    try:
        with csv_path.open("r", newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        return _blocked_corpus(f"cannot read corpus: {type(exc).__name__}: {exc}")

    mismatches: list[dict[str, object]] = []
    mismatch_counts: dict[str, int] = {}
    skipped_set = set(skip_files)
    skipped_rows: list[dict[str, object]] = []
    skipped_names_in_rows: set[str] = set()
    malformed_rows = 0
    for index, row in enumerate(rows, start=1):
        file_name_object = row.get("File Name")
        file_name = file_name_object if isinstance(file_name_object, str) else ""
        if file_name in skipped_set:
            skipped_rows.append(
                {"row": index, "name": file_name, "status": "not_asserted"}
            )
            skipped_names_in_rows.add(file_name)
            continue
        try:
            series_name = row["Series Name"]
            expected_file_type = row["File Type"]
            release_number_raw = row["Release Number"]
            if not isinstance(release_number_raw, str):
                raise ValueError("Release Number must be a string")
            normalized_release_number: object = release_number_raw
            if release_number_raw.startswith("["):
                normalized_release_number = main_module.get_min_and_max_numbers(
                    release_number_raw[1:-1]
                )
            expected_release = main_module.set_num_as_float_or_int(
                normalized_release_number
            )
            file_type = (
                "chapter"
                if not main_module.contains_volume_keywords(file_name)
                and main_module.contains_chapter_keywords(file_name)
                else "volume"
            )
            actual_release = main_module.set_num_as_float_or_int(
                main_module.get_release_number_cache(
                    file_name, chapter=file_type == "chapter"
                )
            )
            if not actual_release and main_module.is_one_shot(
                file_name, skip_folder_check=True
            ):
                actual_release = 1
            actual_series = (
                main_module.get_series_name_from_chapter(
                    file_name, str(fixture_root), actual_release
                )
                if file_type == "chapter"
                else main_module.get_series_name_from_volume(
                    file_name, str(fixture_root), test_mode=True
                )
            )
            values = (
                ("Release Number", expected_release, actual_release),
                ("Series Name", series_name, actual_series),
                ("File Type", expected_file_type, file_type),
            )
            for field_name, expected, actual in values:
                if actual != expected:
                    mismatches.append(
                        _mismatch(index, file_name, field_name, expected, actual)
                    )
                    mismatch_counts[field_name] = mismatch_counts.get(field_name, 0) + 1
        except Exception as exc:
            # Keep the row accounted for even if an unexpected legacy parser error occurs.
            malformed_rows += 1
            mismatches.append(
                _mismatch(
                    index,
                    file_name,
                    "row",
                    "valid original validator inputs",
                    f"{type(exc).__name__}: {exc}",
                )
            )
            mismatch_counts["row"] = mismatch_counts.get("row", 0) + 1
    status = "pass"
    detail_parts = [
        "reproduced original validate_csv normalization for Release Number, Series Name and File Type",
    ]
    if len(rows) != EXPECTED_CORPUS_ROWS:
        status = "blocked"
        detail_parts.append(f"read {len(rows)} rows; expected {EXPECTED_CORPUS_ROWS}")
    if exclusion_detail:
        status = "blocked"
        detail_parts.append(exclusion_detail)
    if mismatches and status != "blocked":
        status = "fail"
        detail_parts.append(f"{len(mismatches)} field mismatches")
    if malformed_rows:
        detail_parts.append(f"{malformed_rows} malformed rows")
    unused_fields = [
        "Release Year",
        "Release Part",
        "Index Number",
        "Release Group",
        "Publisher",
        "Subtitle",
        "Extras",
    ]
    skipped_files_report = [
        {
            "name": name,
            "status": "not_asserted",
            "matched": name in skipped_names_in_rows,
        }
        for name in skip_files
    ]
    return {
        "status": status,
        "rows": len(rows),
        "processed_rows": len(rows),
        "validated_rows": len(rows) - len(skipped_rows),
        "skipped_rows": len(skipped_rows),
        "skipped_files": skipped_files_report,
        "skipped_row_details": skipped_rows,
        "mismatches": mismatches,
        "mismatch_counts": mismatch_counts,
        "validated_fields": ["Release Number", "Series Name", "File Type"],
        "unused_metadata_fields": unused_fields,
        "detail": "; ".join(detail_parts),
    }


def _run_original_validator(
    repo: Path, main_module: types.ModuleType, tests_tree: ast.Module
) -> dict[str, object]:
    function_node = next(
        (
            node
            for node in tests_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "validate_csv"
        ),
        None,
    )
    class_node = next(
        (
            node
            for node in tests_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "IncorrectItem"
        ),
        None,
    )
    if function_node is None or class_node is None:
        return {
            "status": "blocked",
            "detail": "original validate_csv definition/class unavailable",
        }
    namespace = dict(main_module.__dict__)
    namespace["ROOT_DIR"] = str(repo)
    namespace["csv"] = csv
    namespace["os"] = os
    try:
        class_module = ast.Module(body=[copy.deepcopy(class_node)], type_ignores=[])
        ast.fix_missing_locations(class_module)
        exec(compile(class_module, str(repo / "tests.py"), "exec"), namespace)
        function = _exec_function(
            function_node, namespace, filename=str(repo / "tests.py")
        )
    except BaseException as exc:
        return {
            "status": "blocked",
            "detail": f"cannot load original validator: {type(exc).__name__}: {exc}",
        }
    status, detail = _invoke(function)
    return {
        "status": status,
        "detail": f"bounded original validate_csv capture; {detail}",
    }


def _run_mode(mode: str, repo: Path, work: Path) -> dict[str, object]:
    source_hashes = _source_hashes(repo)
    dependency_versions = _dependency_versions()
    base: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "source_hashes": source_hashes,
        "dependency_versions": dependency_versions,
        "tests": [],
        "corpus": None,
        "adaptations": [],
    }
    settings_path = repo / "settings.py"
    tests_path = repo / "tests.py"
    try:
        settings_report = extract_safe_settings(settings_path)
        tests_source = tests_path.read_text(encoding="utf-8")
        tests_tree = ast.parse(tests_source, filename=str(tests_path), mode="exec")
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        base["tests"] = [
            _test_entry(
                "probe",
                "blocked",
                f"cannot load source declarations: {type(exc).__name__}: {exc}",
            )
        ]
        return base

    asserted, non_asserted = _assert_nodes(tests_tree)
    if not settings_report.ok:
        detail = "safe settings migration rejected expressions"
        if settings_report.source_error:
            detail = f"{detail}: {settings_report.source_error}"
        elif settings_report.rejected:
            detail = f"{detail}: {', '.join(issue.name for issue in settings_report.rejected)}"
        base["tests"] = _blocked_test_entries(asserted, non_asserted, detail)
        base["adaptations"] = [
            {"name": "settings", "status": "blocked", "detail": detail}
        ]
        if mode in {"corpus", "all"}:
            base["corpus"] = _blocked_corpus(detail)
        return base

    if mode in {"fast", "all"}:
        test_entries, adaptations, fast_error = _run_fast(
            repo,
            work,
            settings_report,
            tests_source,
            tests_tree,
            asserted,
            non_asserted,
        )
        base["tests"] = test_entries
        base["adaptations"] = adaptations
        if fast_error is not None and mode == "fast":
            return base

    if mode in {"corpus", "all"}:
        holder = _fixture_root(work)
        fixture_root = Path(holder.name) / "komga-cover-extractor"
        module, import_detail = _load_main(repo, settings_report, settings_path)
        if module is None:
            base["corpus"] = _blocked_corpus(
                f"legacy main import blocked: {import_detail}"
            )
        else:
            setattr(module, "paths", [])
            setattr(module, "download_folders", [])
            setattr(module, "paths_with_types", [])
            setattr(module, "download_folders_with_types", [])
            setattr(module, "cached_paths", [])
            captured: dict[str, dict[str, object]] = {}
            capture_status, capture_detail = _invoke(
                lambda: captured.setdefault(
                    "corpus",
                    _run_corpus(repo, module, fixture_root, tests_tree, tests_source),
                )
            )
            if capture_status != "pass":
                corpus = _blocked_corpus(f"corpus execution failed: {capture_detail}")
            else:
                corpus = captured["corpus"]
                if capture_detail != "completed without diagnostics":
                    corpus["diagnostics"] = capture_detail
            corpus["legacy_validate_csv"] = _run_original_validator(
                repo, module, tests_tree
            )
            base["corpus"] = corpus
        holder.cleanup()
    return base


def _exit_status(result: Mapping[str, object]) -> int:
    statuses: list[str] = []
    tests = result.get("tests")
    if isinstance(tests, list):
        statuses.extend(
            str(item.get("status")) for item in tests if isinstance(item, Mapping)
        )
    adaptations = result.get("adaptations")
    if isinstance(adaptations, list):
        statuses.extend(
            str(item.get("status"))
            for item in adaptations
            if isinstance(item, Mapping) and item.get("status") not in {"not_asserted"}
        )
    corpus = result.get("corpus")
    if isinstance(corpus, Mapping):
        statuses.append(str(corpus.get("status")))
        legacy = corpus.get("legacy_validate_csv")
        if isinstance(legacy, Mapping):
            statuses.append(str(legacy.get("status")))
    if "blocked" in statuses:
        return 2
    if "fail" in statuses:
        return 1
    return 0


def run_probe(arguments: Sequence[str] | None = None) -> int:
    """Run the child protocol and emit exactly one JSON object on stdout."""

    args = list(sys.argv[1:] if arguments is None else arguments)
    mode, argument_error = _parse_mode(args)
    if mode is None:
        result = _empty_result("all", argument_error or "invalid arguments")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2

    repo = Path("/repo")
    work = Path("/work")
    missing: list[str] = []
    if os.environ.get(SANDBOX_MARKER) != "1":
        missing.append(f"{SANDBOX_MARKER}=1 marker")
    if not repo.is_dir():
        missing.append("/repo directory")
    if not work.is_dir():
        missing.append("/work directory")
    if missing:
        result = _empty_result(
            mode, "sandbox prerequisite missing: " + ", ".join(missing)
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2

    try:
        result = _run_mode(mode, repo, work)
        status = _exit_status(result)
    except BaseException as exc:
        result = _empty_result(
            mode,
            f"probe internal failure: {type(exc).__name__}: {exc}",
        )
        status = 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return status


if __name__ == "__main__":
    raise SystemExit(run_probe())
