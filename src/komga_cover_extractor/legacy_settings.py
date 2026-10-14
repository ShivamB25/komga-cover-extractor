"""Safe, data-only migration of the legacy ``settings.py`` source.

The legacy application historically loaded settings by importing an arbitrary Python
module.  This boundary deliberately does not do that.  It parses a bounded source
string with :mod:`ast`, decodes only literal syntax, and translates the one reviewed
``Keyword`` value shape used by the original settings file.
"""

from __future__ import annotations

import ast
import math
from typing import Final, NoReturn

from .config import LEGACY_SETTING_NAMES

_MAX_SOURCE_BYTES: Final = 1_048_576
_MAX_LITERAL_DEPTH: Final = 32

_ALIAS_TO_CANONICAL: Final = {"resturcture_when_renaming": "restructure_when_renaming"}
_ACCEPTED_SETTING_NAMES: Final = LEGACY_SETTING_NAMES | frozenset(
    _ALIAS_TO_CANONICAL.values()
)


class MigrationError(ValueError):
    """Raised when a legacy settings source cannot be safely migrated.

    ``name``, ``line`` and ``reason`` are deliberately structured so callers can
    present an actionable diagnostic without exposing the rejected source text or a
    submitted credential.
    """

    name: str
    line: int
    reason: str

    def __init__(self, name: str, line: int, reason: str) -> None:
        self.name = name
        self.line = line
        self.reason = reason
        super().__init__(f"cannot migrate {name!r} on line {line}: {reason}")


class _LiteralError(ValueError):
    """Internal safe-decoder failure with no source-value payload."""


def _fail(name: str, line: int, reason: str) -> NoReturn:
    raise MigrationError(name, line, reason)


def _is_name(node: ast.AST, expected: str) -> bool:
    return isinstance(node, ast.Name) and node.id == expected


def _is_self_attribute(node: ast.AST, attribute: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attribute
        and _is_name(node.value, "self")
    )


def _is_plain_argument(argument: ast.arg, expected: str) -> bool:
    return (
        argument.arg == expected
        and argument.annotation is None
        and argument.type_comment is None
    )


def _is_reviewed_keyword_class(node: ast.ClassDef) -> bool:
    """Return whether *node* is exactly the legacy data-only ``Keyword`` class.

    Matching the complete small body matters: checking only the class name and
    constructor signature could silently bless a class containing arbitrary code.
    No node here is compiled or evaluated.
    """

    if (
        node.name != "Keyword"
        or node.bases
        or node.keywords
        or node.decorator_list
        or getattr(node, "type_params", [])
        or getattr(node, "type_comment", None) is not None
        or len(node.body) != 3
    ):
        return False
    methods = [item for item in node.body if isinstance(item, ast.FunctionDef)]
    if len(methods) != 3:
        return False
    init, to_string, to_repr = methods
    if [method.name for method in (init, to_string, to_repr)] != [
        "__init__",
        "__str__",
        "__repr__",
    ]:
        return False
    if any(
        method.decorator_list
        or method.returns is not None
        or method.type_comment is not None
        for method in (init, to_string, to_repr)
    ):
        return False

    init_args = init.args
    if (
        init_args.posonlyargs
        or init_args.vararg is not None
        or init_args.kwonlyargs
        or init_args.kwarg is not None
        or init_args.kw_defaults
        or len(init_args.args) != 4
        or not all(
            _is_plain_argument(argument, expected)
            for argument, expected in zip(
                init_args.args, ("self", "name", "score", "file_type"), strict=True
            )
        )
        or len(init_args.defaults) != 1
        or not isinstance(init_args.defaults[0], ast.Constant)
        or init_args.defaults[0].value != "both"
    ):
        return False
    if len(init.body) != 3 or not all(
        isinstance(item, ast.Assign) for item in init.body
    ):
        return False
    expected_assignments = ("name", "score", "file_type")
    for statement, attribute in zip(init.body, expected_assignments, strict=True):
        assert isinstance(statement, ast.Assign)
        if (
            len(statement.targets) != 1
            or not _is_self_attribute(statement.targets[0], attribute)
            or not _is_name(statement.value, attribute)
        ):
            return False

    def _only_self_argument(method: ast.FunctionDef) -> bool:
        arguments = method.args
        return (
            not arguments.posonlyargs
            and len(arguments.args) == 1
            and _is_plain_argument(arguments.args[0], "self")
            and arguments.vararg is None
            and not arguments.kwonlyargs
            and not arguments.kw_defaults
            and arguments.kwarg is None
            and not arguments.defaults
        )

    if not _only_self_argument(to_string) or not _only_self_argument(to_repr):
        return False
    if len(to_string.body) != 1 or not isinstance(to_string.body[0], ast.Return):
        return False
    rendered = to_string.body[0].value
    if not isinstance(rendered, ast.JoinedStr) or len(rendered.values) != 6:
        return False
    expected_text = ("Name: ", ", Score: ", ", File Type: ")
    for index, part in enumerate(rendered.values):
        if index % 2 == 0:
            if (
                not isinstance(part, ast.Constant)
                or part.value != expected_text[index // 2]
            ):
                return False
        else:
            if (
                not isinstance(part, ast.FormattedValue)
                or part.conversion != -1
                or part.format_spec is not None
                or not isinstance(part.value, ast.Attribute)
                or not _is_self_attribute(
                    part.value, ("name", "score", "file_type")[(index - 1) // 2]
                )
            ):
                return False

    if len(to_repr.body) != 1 or not isinstance(to_repr.body[0], ast.Return):
        return False
    representation = to_repr.body[0].value
    return (
        isinstance(representation, ast.Call)
        and _is_name(representation.func, "str")
        and len(representation.args) == 1
        and _is_name(representation.args[0], "self")
        and not representation.keywords
    )


def _decode_keyword(node: ast.Call, depth: int) -> dict[str, object]:
    if depth > _MAX_LITERAL_DEPTH:
        raise _LiteralError("literal nesting exceeds the safe limit")
    if not _is_name(node.func, "Keyword"):
        raise _LiteralError("only the reviewed Keyword constructor is supported")
    if any(isinstance(argument, ast.Starred) for argument in node.args):
        raise _LiteralError("starred Keyword arguments are not supported")
    if any(keyword.arg is None for keyword in node.keywords):
        raise _LiteralError("**Keyword arguments are not supported")

    positional_names = ("name", "score", "file_type")
    if len(node.args) > len(positional_names):
        raise _LiteralError("Keyword accepts at most name, score and file_type")
    fields: dict[str, object] = {}
    for field, argument in zip(
        positional_names[: len(node.args)], node.args, strict=True
    ):
        fields[field] = _decode_literal(argument, depth + 1)
    for keyword in node.keywords:
        # The None form was rejected above; this keeps the narrowing explicit to
        # type checkers without ever incorporating source text in an error.
        if keyword.arg is None:
            raise _LiteralError("**Keyword arguments are not supported")
        field = keyword.arg
        if field not in positional_names:
            raise _LiteralError(f"unsupported Keyword field {field!r}")
        if field in fields:
            raise _LiteralError(f"duplicate Keyword field {field!r}")
        fields[field] = _decode_literal(keyword.value, depth + 1)

    if "name" not in fields or "score" not in fields:
        raise _LiteralError("Keyword requires literal name and score")
    name = fields["name"]
    score = fields["score"]
    file_type = fields.get("file_type", "both")
    if not isinstance(name, str):
        raise _LiteralError("Keyword.name must be a string literal")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise _LiteralError("Keyword.score must be a numeric literal")
    if isinstance(score, float) and not math.isfinite(score):
        raise _LiteralError("Keyword.score must be finite")
    if (
        not isinstance(file_type, str)
        or file_type not in {"chapter", "volume", "both"}
    ):
        raise _LiteralError("Keyword.file_type must be chapter, volume or both")
    return {"name": name, "score": score, "file_type": file_type}


def _decode_literal(node: ast.AST, depth: int = 0) -> object:
    """Decode literals without compiling or evaluating any expression."""

    if depth > _MAX_LITERAL_DEPTH:
        raise _LiteralError("literal nesting exceeds the safe limit")
    if isinstance(node, ast.Constant):
        value: object = node.value
        if value is None or isinstance(value, (bool, int, float, str, bytes)):
            if isinstance(value, float) and not math.isfinite(value):
                raise _LiteralError("non-finite float literal is not supported")
            return value
        raise _LiteralError(f"constant type {type(value).__name__} is not supported")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _decode_literal(node.operand, depth + 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _LiteralError("numeric unary operators require numeric literals")
        return +value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.List):
        return [_decode_literal(item, depth + 1) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_decode_literal(item, depth + 1) for item in node.elts)
    if isinstance(node, ast.Set):
        members = [_decode_literal(item, depth + 1) for item in node.elts]
        try:
            return set(members)
        except TypeError as exc:
            raise _LiteralError("set members must be hashable literals") from exc
    if isinstance(node, ast.Dict):
        decoded: dict[object, object] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:
                raise _LiteralError("dictionary unpacking is not supported")
            key = _decode_literal(key_node, depth + 1)
            value = _decode_literal(value_node, depth + 1)
            try:
                decoded[key] = value
            except TypeError as exc:
                raise _LiteralError(
                    "dictionary keys must be hashable literals"
                ) from exc
        return decoded
    if isinstance(node, ast.Call):
        return _decode_keyword(node, depth)
    raise _LiteralError(f"expression {type(node).__name__} is not a safe literal")


def _assignment_parts(
    statement: ast.Assign | ast.AnnAssign,
) -> tuple[list[str], ast.AST]:
    if isinstance(statement, ast.AnnAssign):
        raise _LiteralError("annotations are not supported")
    targets = tuple(statement.targets)
    value = statement.value
    names = [target.id for target in targets if isinstance(target, ast.Name)]
    if len(names) != len(targets):
        raise _LiteralError("only simple-name assignments with values are supported")
    return names, value


def _statement_name(statement: ast.stmt) -> str:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return statement.name
    if isinstance(statement, ast.Import):
        if not statement.names:
            return "<module>"
        return statement.names[0].asname or statement.names[0].name
    if isinstance(statement, ast.ImportFrom):
        return statement.module or "<module>"
    return "<module>"


def _decode_source(source: str) -> ast.Module:
    if type(source) is not str:
        _fail("<source>", 0, "source must be a string")
    try:
        source_size = len(source.encode("utf-8"))
    except UnicodeError:
        _fail("<source>", 0, "source is not valid UTF-8 text")
    if source_size > _MAX_SOURCE_BYTES:
        _fail("<source>", 0, "source exceeds the 1 MiB migration limit")
    try:
        return ast.parse(source, filename="<legacy-settings>", mode="exec")
    except SyntaxError as exc:
        _fail(
            "<source>",
            int(getattr(exc, "lineno", 0) or 0),
            "source is not valid Python",
        )
    except (MemoryError, RecursionError, ValueError):
        _fail("<source>", 0, "source is not valid Python")


def migrate_settings(source: str) -> dict[str, object]:
    """Safely translate literal legacy settings into canonical primitive values.

    No code from *source* is imported, compiled, or evaluated.  The returned mapping
    is suitable for the configuration validator and TOML writer; every ``Keyword``
    object is represented by a plain ``dict``.
    """

    tree = _decode_source(source)
    values: dict[str, object] = {}
    seen_alias = False
    seen_canonical = False
    keyword_class_seen = False

    for statement in tree.body:
        if isinstance(statement, ast.ClassDef) and statement.name == "Keyword":
            if keyword_class_seen:
                _fail(
                    "Keyword",
                    statement.lineno,
                    "duplicate Keyword class definition",
                )
            if not _is_reviewed_keyword_class(statement):
                _fail(
                    "Keyword",
                    statement.lineno,
                    "Keyword class shape is not reviewed",
                )
            keyword_class_seen = True
            continue
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Expr) and isinstance(
            statement.value, ast.Constant
        ):
            if isinstance(statement.value.value, str):
                continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            try:
                names, value_node = _assignment_parts(statement)
            except _LiteralError as exc:
                assignment_name = (
                    statement.target.id
                    if isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    else "<assignment>"
                )
                _fail(assignment_name, statement.lineno, str(exc))
            for name in names:
                if name not in _ACCEPTED_SETTING_NAMES:
                    _fail(name, statement.lineno, "unknown legacy setting name")
                if name == "resturcture_when_renaming":
                    seen_alias = True
                elif name == "restructure_when_renaming":
                    seen_canonical = True
            if seen_alias and seen_canonical:
                _fail(
                    "restructure_when_renaming",
                    statement.lineno,
                    "legacy alias and canonical setting cannot both "
                    "occur in one source",
                )
            try:
                decoded = _decode_literal(value_node)
            except (
                MemoryError,
                RecursionError,
                TypeError,
                ValueError,
            ) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, _LiteralError)
                    else "literal is not safely representable"
                )
                _fail(
                    names[0] if names else "<assignment>",
                    statement.lineno,
                    reason,
                )
            for name in names:
                if name == "cache_each_root_for_each_path_in_paths_at_beginning_toggle":
                    if type(decoded) is not bool:
                        _fail(name, statement.lineno, "retired eager-cache control must be a boolean")
                    continue
                values[_ALIAS_TO_CANONICAL.get(name, name)] = decoded
            continue

        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            reason = "imports are not supported during safe migration"
        elif isinstance(
            statement,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
        ):
            reason = "callable definitions are not settings data"
        elif isinstance(statement, ast.ClassDef):
            reason = "only the reviewed Keyword class is supported"
        else:
            reason = f"top-level {type(statement).__name__} is not supported"
        _fail(
            _statement_name(statement),
            getattr(statement, "lineno", 0),
            reason,
        )

    return values


__all__ = ["MigrationError", "migrate_settings"]
