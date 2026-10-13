"""Import-safe CLI boundary for configuration, invocation and owned outputs."""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import importlib.resources
import importlib.util
import json
import os
import stat
import sys
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Event, current_thread, main_thread
from types import FrameType
from typing import TYPE_CHECKING, Any, Never, TextIO, cast
from urllib.parse import urlsplit

from . import __version__
from .config import Configuration, ConfigurationError, load_config, validate_config
from .legacy_settings import MigrationError, migrate_settings

if TYPE_CHECKING:
    from .application.pipeline import RunRequest
    from .domain.decisions import RunResult
    from .domain.libraries import LibraryRoot


_MAX_SOURCE_BYTES = 1 << 20
_LEGACY_ROUTES = frozenset({"legacy", "legacy-qbit"})

_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("regex", "legacy-compatible regular expression engine"),
    ("tomli_w", "configuration and migration TOML serialization"),
    ("titlecase", "release title rendering"),
    ("unidecode", "release name transliteration"),
    ("PIL", "raster cover observation and encoding"),
    ("bs4", "BookWalker HTML parsing"),
    ("lxml", "bounded XML metadata parsing"),
    ("filetype", "native archive header observation"),
    ("py7zr", "7z conversion"),
    ("rarfile", "RAR conversion; native unrar is an additional prerequisite"),
    ("watchdog", "filesystem watch event intake"),
    ("requests", "scoped HTTP integrations"),
    ("numpy", "cover similarity preprocessing"),
    ("cv2", "cover similarity resizing"),
    ("skimage", "cover structural similarity comparison"),
)
_RESOURCE_NAMES: tuple[str, ...] = ("blank_black.png", "blank_white.jpg")

_BOOK_COMMANDS: tuple[tuple[str, str], ...] = (
    ("inspect", "inspect selected book metadata and identities without writing"),
    ("extract", "preview or apply verified book and series artwork publication"),
    ("organize", "preview or apply configured release organization"),
    ("convert", "preview or apply verified native archive conversion"),
    ("duplicates", "preview or apply ranked duplicate quarantine"),
    ("missing-releases", "report bounded local integer-volume gaps without writing"),
    ("rename-lists", "preview or apply rename dictionary import and maintenance"),
    ("releases", "compare local releases with read-only BookWalker observations"),
    ("run", "execute the complete configured pipeline; preview is the default"),
)
_LIFECYCLE_COMMANDS: tuple[tuple[str, str], ...] = (
    ("watch", "run the same pipeline through bounded transfer readiness and event intake"),
    ("qbit", "independently plan or authorize qBittorrent selection; never delete content"),
    ("serve", "supervise installed run/watch and the configured independent torrent helper"),
)
_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "komga_ip",
        "komga_port",
        "komga_login_email",
        "komga_login_password",
        "qbittorrent_ip",
        "qbittorrent_port",
        "qbittorrent_target_category",
        "qbittorrent_username",
        "qbittorrent_password",
        "discord_webhook_url",
        "bookwalker_webhook_urls",
        "new_volume_webhook",
    }
)


class _CliValidationError(ValueError):
    """A user-supplied command value failed validation."""


class _CliOperationalError(RuntimeError):
    """An expected filesystem or optional-runtime operation failed."""


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise _CliValidationError("invalid command arguments; use --help for supported options")


def _argument_path(value: str) -> Path:
    if not value.strip() or "\x00" in value:
        raise argparse.ArgumentTypeError("path must not be empty or contain NUL")
    return Path(value)


def _add_output_options(parser: argparse.ArgumentParser, *, scope: str) -> None:
    parser.add_argument("--json", dest=f"{scope}_json", action="store_true",
                        default=argparse.SUPPRESS, help="machine-readable JSON; watch streams JSONL")
    parser.add_argument("--plan-output", dest=f"{scope}_plan_output", type=_argument_path,
                        default=argparse.SUPPRESS, help="exclusive explicit informational plan output")
    parser.add_argument("--profile", dest=f"{scope}_profile", type=_argument_path,
                        default=argparse.SUPPRESS, help="exclusive profile output for extract or run")



def _add_shared_options(parser: argparse.ArgumentParser, *, scope: str) -> None:
    """Add shared options with independent root/subcommand destinations."""
    parser.add_argument(
        "--config",
        dest=f"{scope}_config",
        type=_argument_path,
        default=argparse.SUPPRESS,
        help="TOML configuration file (paths in it are relative to that file)",
    )
    parser.add_argument(
        "--set",
        dest=f"{scope}_set_values",
        action="append",
        metavar="KEY=VALUE",
        default=argparse.SUPPRESS,
        help="explicit override; VALUE is parsed as a TOML literal",
    )
    parser.add_argument(
        "--paths",
        dest=f"{scope}_paths",
        action="append",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="library root; repeat for multiple roots",
    )
    parser.add_argument(
        "--download-folders",
        dest=f"{scope}_download_folders",
        action="append",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="download root; repeat for multiple roots",
    )
    for flag in ("apply", "allow-remote", "allow-torrent-removal", "interactive"):
        parser.add_argument(f"--{flag}", dest=f"{scope}_{flag.replace('-', '_')}",
                            action="store_true", default=argparse.SUPPRESS)
    _add_output_options(parser, scope=scope)


def _build_parser() -> argparse.ArgumentParser:
    parser = _CliParser(
        prog="kce",
        description="Inspect, plan and explicitly authorize Komga Cover Extractor workflows.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
        help="show the package version and exit",
    )
    _add_shared_options(parser, scope="root")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser(
        "inspect-config",
        help="validate and print a redacted configuration as JSON",
    )
    _add_shared_options(inspect_parser, scope="command")

    for name, help_text in _BOOK_COMMANDS:
        book_parser = commands.add_parser(name, help=help_text)
        _add_shared_options(book_parser, scope="command")
        book_parser.add_argument("books", nargs="*", type=_argument_path)
        if name == "rename-lists":
            book_parser.add_argument("--import-from", type=_argument_path)
    recover_parser = commands.add_parser("recover", help="inspect or reconcile a recorded operation")
    _add_shared_options(recover_parser, scope="command")
    recover_parser.add_argument("operation_id")

    for name, help_text in _LIFECYCLE_COMMANDS:
        service_parser = commands.add_parser(name, help=help_text)
        _add_shared_options(service_parser, scope="command")
        if name != "serve":
            service_parser.add_argument("--once", action="store_true",
                                        help="finish one reconciliation or torrent poll")
        if name == "watch":
            service_parser.add_argument("--polling", action="store_true",
                                        help="use watchdog's polling event source")

    doctor_parser = commands.add_parser(
        "doctor",
        help="validate configuration and report conservative local capabilities as JSON",
    )
    _add_shared_options(doctor_parser, scope="command")

    migrate_parser = commands.add_parser(
        "migrate-config",
        help="safely translate literal legacy settings.py assignments to TOML",
    )
    _add_shared_options(migrate_parser, scope="command")
    migrate_parser.add_argument(
        "input",
        type=_argument_path,
        help="legacy settings.py path; it is parsed, never imported or executed",
    )
    migrate_parser.add_argument(
        "--output",
        type=_argument_path,
        default=None,
        help="exclusive 0600 TOML destination (never overwrites an existing file)",
    )
    return parser


def _parse_invocation(arguments: Sequence[str]) -> tuple[argparse.Namespace, RunRequest | None]:
    if arguments and arguments[0] in _LEGACY_ROUTES:
        from .compat.legacy_cli import parse_legacy_args

        supplemental = _CliParser(prog="kce " + arguments[0], add_help=False, allow_abbrev=False)
        _add_output_options(supplemental, scope="command")
        supplemental.add_argument("--set", dest="command_set_values", action="append",
                                  default=argparse.SUPPRESS, metavar="KEY=VALUE")
        namespace, historical = supplemental.parse_known_args(arguments[1:])
        request = parse_legacy_args(historical, addon=arguments[0] == "legacy-qbit")
        namespace.command = arguments[0]
        if request.config_path is not None:
            namespace.command_config = request.config_path
        return namespace, request
    return _build_parser().parse_args(arguments), None



def _parse_toml_literal(key: str, literal: str) -> object:
    """Parse one ``--set`` value without evaluating arbitrary Python."""
    try:
        parsed = tomllib.loads(f"value = {literal}\n")
    except (tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        raise _CliValidationError(
            f"invalid TOML literal for --set key {key!r}"
        ) from exc
    # The wrapper assignment ensures exactly one result.  Keep this guard in
    # case the standard parser's behavior changes around unusual key syntax.
    if set(parsed) != {"value"}:
        raise _CliValidationError(f"invalid TOML literal for --set key {key!r}")
    return parsed["value"]


def _collect_overrides(namespace: argparse.Namespace) -> dict[str, object]:
    """Build CLI overrides, preserving root-to-command encounter order."""
    overrides: dict[str, object] = {}
    set_values = [
        *getattr(namespace, "root_set_values", []),
        *getattr(namespace, "command_set_values", []),
    ]
    for raw_item in set_values:
        if "=" not in raw_item:
            raise _CliValidationError(
                "--set requires KEY=VALUE, with VALUE written as a TOML literal"
            )
        key, literal = raw_item.split("=", 1)
        key = key.strip()
        if not key:
            raise _CliValidationError("--set requires a non-empty setting key")
        overrides[key] = _parse_toml_literal(key, literal.strip())

    # Dedicated repeated roots have higher precedence than --set roots.
    for key in ("paths", "download_folders"):
        values = [
            *getattr(namespace, f"root_{key}", []),
            *getattr(namespace, f"command_{key}", []),
        ]
        if values:
            overrides[key] = values
    return overrides


def _resolve_cli_path_overrides(overrides: Mapping[str, object]) -> dict[str, object]:
    """Resolve migration command path overrides from the invocation cwd."""
    resolved = dict(overrides)
    base = Path.cwd()
    for key in ("state_dir", "log_dir"):
        value = resolved.get(key)
        if isinstance(value, (str, Path)):
            if value == "":
                raise _CliValidationError(f"{key}: path must not be empty")
            resolved[key] = str((base / value).absolute())
    for key in ("paths", "download_folders"):
        value = resolved.get(key)
        if not isinstance(value, (list, tuple)):
            continue
        roots: list[object] = []
        for root in value:
            if not isinstance(root, (str, Path)):
                roots.append(root)
                continue
            if root == "":
                raise _CliValidationError(f"{key}: path entries must not be empty")
            root_path = Path(root)
            if root_path.is_absolute():
                roots.append(str(root_path.absolute()))
            else:
                roots.append(str((base / root_path).absolute()))
        resolved[key] = roots
    return resolved



def _load_namespace_config(
    namespace: argparse.Namespace,
    overrides: Mapping[str, object],
    *, environ: Mapping[str, str] | None = None,
) -> Configuration:
    """Load configuration through the stable public loader boundary."""
    config_path = getattr(
        namespace,
        "command_config",
        getattr(namespace, "root_config", None),
    )
    return load_config(
        path=config_path,
        environ=dict(os.environ) if environ is None else dict(environ),
        overrides=dict(overrides) or None,
        cwd=Path.cwd(),
    )


def _json_default(value: object) -> object:
    """Serialize path-like values without exposing arbitrary object reprs."""
    if isinstance(value, Path):
        return str(value)
    return "<unserializable>"


def _emit_json(payload: Mapping[str, object]) -> None:
    """Write one valid, UTF-8 JSON document to stdout."""
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        ),
        flush=True,
    )


def _safe_error_message(error: BaseException) -> str:
    """Return an actionable message while removing values currently in env."""
    message = str(error).strip()
    if not message:
        message = error.__class__.__name__
    # Configuration errors are required to name keys/reasons, not submitted
    # secrets.  This extra boundary scrub protects against an accidental value
    # in a future validator or an OSError that includes an environment-derived
    # path.
    for value in os.environ.values():
        if value and len(value) >= 3:
            message = message.replace(value, "<redacted>")
    return message


def _emit_diagnostic(command: str, message: str) -> None:
    print(f"{command}: {message}", file=sys.stderr)


def _json_failure(
    command: str, error: BaseException, *, exit_code: int = 2,
    reason: str = "invalid_cli_input",
) -> int:
    from .domain.decisions import Decision, RunResult

    message = _safe_error_message(error)
    result = RunResult((Decision("invalid_input" if exit_code == 2 else "failed", reason),),
                       failed=1 if exit_code == 1 else 0)
    payload = result.as_dict()
    payload.update({"command": command, "error": {"type": error.__class__.__name__, "message": message}})
    _emit_json(payload)
    _emit_diagnostic(command, message)
    return exit_code


def _config_values(config: Any, *, redact: bool) -> dict[str, object]:
    values = config.as_dict(redact=redact)
    if not isinstance(values, Mapping):
        raise _CliOperationalError("configuration serializer returned a non-mapping")
    return dict(values)


def _config_payload(config: Any, *, redact: bool) -> dict[str, object]:
    values = _config_values(config, redact=redact)
    source_values = getattr(config, "sources", {})
    if not isinstance(source_values, Mapping):
        raise _CliOperationalError("configuration source map is not a mapping")
    return {"config": values, "sources": dict(source_values)}


def _inspect_config(
    namespace: argparse.Namespace,
    overrides: Mapping[str, object],
) -> int:
    try:
        config = _load_namespace_config(namespace, overrides)
        payload = _config_payload(config, redact=True)
    except ConfigurationError as exc:
        return _json_failure("inspect-config", exc)
    except _CliOperationalError as exc:
        return _json_failure("inspect-config", exc, exit_code=1)
    _emit_json({"ok": True, "command": "inspect-config", **payload})
    return 0


def _probe_dependency(module_name: str, purpose: str) -> dict[str, object]:
    try:
        installed = importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        installed = False
    return {
        "installed": installed,
        "purpose": purpose,
        "runtime": "not-probed",
        "runtime_verified": False,
        "status": "installed" if installed else "missing",
    }


def _probe_resource(name: str) -> dict[str, object]:
    try:
        package_root = importlib.resources.files("komga_cover_extractor")
        present = package_root.joinpath("resources", name).is_file()
    except (ImportError, ModuleNotFoundError, OSError, TypeError):
        present = False
    return {
        "checked_via": "importlib.resources",
        "package": "komga_cover_extractor",
        "present": present,
        "runtime": "presence-only",
        "runtime_verified": False,
        "status": "present" if present else "missing",
    }


def _doctor(
    namespace: argparse.Namespace,
    overrides: Mapping[str, object],
) -> int:
    config: Any | None = None
    validation: dict[str, object]
    try:
        config = _load_namespace_config(namespace, overrides)
        validation = {"status": "valid", "runtime_verified": True}
    except ConfigurationError as exc:
        validation = {
            "status": "invalid",
            "runtime_verified": True,
            "error": _safe_error_message(exc),
        }

    try:
        payload: dict[str, object] = {
            "ok": validation["status"] == "valid",
            "command": "doctor",
            "validation": validation,
            "dependencies": {
                name: _probe_dependency(name, purpose)
                for name, purpose in _DEPENDENCIES
            },
            "resources": {
                name: _probe_resource(name) for name in _RESOURCE_NAMES
            },
            "commands": [name for name, _ in (*_BOOK_COMMANDS, *_LIFECYCLE_COMMANDS)] + ["recover"],
            "remote_probes": {
                "status": "not-probed",
                "runtime_verified": False,
                "reason": "doctor performs no network or credential probes",
            },
        }
        if config is None:
            payload["config"] = None
            payload["sources"] = {}
        else:
            payload.update(_config_payload(config, redact=True))
    except _CliOperationalError as exc:
        return _json_failure("doctor", exc, exit_code=1)

    _emit_json(payload)
    return 0 if validation["status"] == "valid" else 2


def _is_sensitive_key(key: object) -> bool:
    return key in _SECRET_NAMES

def _nonempty_secret(value: object) -> bool:
    if value is None or value is False or value == 0 or value == "":
        return False
    if isinstance(value, Mapping):
        return any(_nonempty_secret(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_nonempty_secret(item) for item in value)
    return True


def _contains_nonempty_secret(raw: object, redacted: object, key: object = "") -> bool:
    """Detect non-empty credentials without printing either representation."""
    if _is_sensitive_key(key) and _nonempty_secret(raw):
        return True
    if isinstance(raw, Mapping):
        redacted_map = redacted if isinstance(redacted, Mapping) else {}
        for name, value in raw.items():
            if _contains_nonempty_secret(value, redacted_map.get(name), name):
                return True
        return False
    if isinstance(raw, (list, tuple)):
        redacted_values = redacted if isinstance(redacted, (list, tuple)) else ()
        return any(
            _contains_nonempty_secret(
                value,
                redacted_values[index] if index < len(redacted_values) else None,
                key,
            )
            for index, value in enumerate(raw)
        )
    if isinstance(raw, str):
        try:
            parsed = urlsplit(raw)
            if parsed.username or parsed.password:
                return True
        except ValueError:
            pass
    return False


def _toml_value(value: object) -> object:
    """Convert config values to the primitive tree accepted by tomli-w."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _toml_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_toml_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise _CliValidationError("configuration contains a value that cannot be written as TOML")


def _toml_dump(values: Mapping[str, object]) -> str:
    try:
        tomli_w = importlib.import_module("tomli_w")
    except (ImportError, ModuleNotFoundError) as exc:
        raise _CliValidationError(
            "tomli-w prerequisite is not installed for migrate-config"
        ) from exc
    dumps = getattr(tomli_w, "dumps", None)
    if not callable(dumps):
        raise _CliValidationError("installed tomli-w has no dumps function")
    try:
        dumped_value = dumps(_toml_value(values))
    except (TypeError, ValueError) as exc:
        raise _CliValidationError(
            "validated configuration could not be serialized as TOML"
        ) from exc
    if not isinstance(dumped_value, str):
        raise _CliValidationError("tomli-w returned non-text TOML output")
    dumped = dumped_value
    if not dumped.endswith("\n"):
        dumped += "\n"
    # tomli-w is deliberately used for publication, but round-trip parsing is
    # the final boundary check.  It catches unsupported values or a future
    # serializer change before stdout or a protected file is touched.
    try:
        parsed = tomllib.loads(dumped)
    except tomllib.TOMLDecodeError as exc:
        raise _CliValidationError(
            "serialized migration output is not valid TOML"
        ) from exc
    if not isinstance(parsed, dict):
        raise _CliValidationError("serialized migration output is not a TOML table")
    return dumped


@contextmanager
def _exclusive_output(path: Path) -> Iterator[TextIO]:
    from .infrastructure.filesystem import checked_path

    directory: int | None = None
    descriptor: int | None = None
    owned: os.stat_result | None = None
    complete = False
    try:
        try:
            checked_path(path.parent, path.parent)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            descriptor = os.open(
                path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=directory,
            )
            owned = os.fstat(descriptor)
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
            descriptor = None
        except FileExistsError:
            raise _CliValidationError("refusing to overwrite existing explicit output") from None
        except OSError:
            raise _CliOperationalError("cannot create protected explicit output") from None
        with stream:
            yield stream
            try:
                stream.flush()
                os.fsync(stream.fileno())
                checked_path(path.parent, path.parent)
                parent = path.parent.stat()
                held = os.fstat(directory)
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if ((parent.st_dev, parent.st_ino) != (held.st_dev, held.st_ino)
                        or (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino)
                        or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                    raise _CliOperationalError("explicit output identity changed")
                os.fsync(directory)
            except OSError:
                raise _CliOperationalError("cannot verify and flush explicit output") from None
        complete = True
    except OSError:
        raise _CliOperationalError("cannot write protected explicit output") from None
    finally:
        try:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                if directory is not None:
                    try:
                        if not complete and owned is not None:
                            try:
                                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                                if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                                    os.unlink(path.name, dir_fd=directory)
                                    os.fsync(directory)
                            except FileNotFoundError:
                                pass
                    finally:
                        os.close(directory)
        except OSError:
            raise _CliOperationalError("cannot settle protected explicit output") from None


def _write_exclusive(path: Path, content: str) -> None:
    with _exclusive_output(path) as stream:
        stream.write(content)


def _read_migration_source(path: Path) -> tuple[Path, str]:
    source = path if path.is_absolute() else Path.cwd() / path
    source = source.resolve()
    try:
        with source.open("rb") as stream:
            raw = stream.read(_MAX_SOURCE_BYTES + 1)
        if len(raw) > _MAX_SOURCE_BYTES:
            raise _CliValidationError("migration input exceeds the 1 MiB safety limit")
        text = raw.decode("utf-8")
    except _CliValidationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _CliValidationError("cannot read migration input") from exc
    return source, text


def _migrate_config(
    namespace: argparse.Namespace,
    overrides: Mapping[str, object],
) -> int:
    try:
        source_path, source_text = _read_migration_source(namespace.input)
        migrated = migrate_settings(source_text)
        if not isinstance(migrated, Mapping):
            raise _CliOperationalError("migration returned a non-mapping")
        values = dict(migrated)
        values.update(_resolve_cli_path_overrides(overrides))
        config = validate_config(values, base_dir=source_path.parent)
        raw_values = _config_values(config, redact=False)
        safe_values = _config_values(config, redact=True)
        if _contains_nonempty_secret(raw_values, safe_values):
            output = getattr(namespace, "output", None)
            if output is None:
                raise _CliValidationError(
                    "migration contains non-empty credentials; use --output for a protected 0600 file or move secrets to environment variables"
                )
        rendered = _toml_dump(raw_values)
        output = getattr(namespace, "output", None)
        if output is None:
            print(rendered, end="")
            return 0
        output_path = output if output.is_absolute() else Path.cwd() / output
        output_path = output_path.resolve()
        if output_path == source_path:
            raise _CliValidationError(
                "refusing to overwrite the migration input; choose a different --output"
            )
        _write_exclusive(output_path, rendered)
    except (MigrationError, ConfigurationError, _CliValidationError) as exc:
        _emit_diagnostic("migrate-config", _safe_error_message(exc))
        return 2
    except _CliOperationalError as exc:
        _emit_diagnostic("migrate-config", _safe_error_message(exc))
        return 1
    return 0


def _configured_request(
    namespace: argparse.Namespace, overrides: Mapping[str, object], legacy: RunRequest | None,
    shared: Mapping[str, object],
) -> tuple[Configuration, RunRequest]:
    from .application.pipeline import RunRequest
    from .application.serve import child_roots
    from .compat.legacy_cli import parse_docker_environment
    from .domain.decisions import RunAuthorization
    from .domain.libraries import LibraryRoot, normalize_library_roots

    environment = dict(os.environ)
    try:
        inherited = child_roots(environment)
    except ValueError as error:
        raise _CliValidationError(str(error)) from None
    historical = dict(legacy.config_overrides) if legacy is not None else {}
    explicit = {**historical, **overrides}
    for key in ("paths", "download_folders"):
        if key in historical:
            explicit[key] = historical[key]
    docker = parse_docker_environment(environment) if namespace.command == "serve" else None
    docker_values = dict(docker.config_overrides) if docker is not None else {}
    for key, value in docker_values.items():
        name = "KCE_" + key.upper()
        if name not in environment:
            literal = _toml_dump({"value": value}).removeprefix("value = ").removesuffix("\n")
            environment[name] = literal
    if namespace.command == "qbit":
        explicit["uncheck_non_qbit_upgrades_toggle"] = True
    config = _load_namespace_config(namespace, explicit, environ=environment)
    preferences: dict[str, tuple[LibraryRoot, ...]] = {}
    for key, role in (("paths", "library"), ("download_folders", "download")):
        if key in historical:
            assert legacy is not None
            preferences[role] = legacy.roots
        elif key in overrides:
            preferences[role] = ()
        elif key in docker_values and "KCE_" + key.upper() not in os.environ:
            assert docker is not None
            preferences[role] = docker.roots
        else:
            preferences[role] = inherited
            supplied = {root.path for root in inherited if role in root.roles}
            if supplied and supplied != set(getattr(config, key)):
                raise _CliValidationError("child roots must match the selected configuration")
    roots: list[LibraryRoot] = []
    for key, role in (("paths", "library"), ("download_folders", "download")):
        for path in getattr(config, key):
            hints = preferences[role]
            prior = next((root for root in hints if root.path == path and role in root.roles), None)
            if prior is None and key not in explicit:
                prior = next((root for group in preferences.values() for root in group if root.path == path), None)
            roots.append(
                LibraryRoot(path, prior.formats, prior.extensions, prior.library_types, (role,))
                if prior is not None else LibraryRoot(path, roles=(role,))
            )
    books = tuple(getattr(namespace, "books", ()))
    try:
        selected_roots = normalize_library_roots(tuple(roots))
        if not selected_roots and books:
            selected_roots = normalize_library_roots(tuple(LibraryRoot(book.absolute().parent) for book in books))
    except ValueError:
        raise _CliValidationError("conflicting_or_nested_roots") from None
    if legacy is not None:
        command = "qbit" if namespace.command == "legacy-qbit" else "watch" if config.watchdog_toggle else "run"
        authorization = legacy.authorization
        interactive, once = legacy.interactive, legacy.once
    else:
        command = namespace.command
        authorization = RunAuthorization(
            apply=bool(shared.get("apply", False)), allow_remote=bool(shared.get("allow_remote", False)),
            allow_torrent_removal=bool(shared.get("allow_torrent_removal", False)),
        )
        interactive, once = bool(shared.get("interactive", False)), bool(getattr(namespace, "once", False))
    return config, RunRequest(
        command, books=books, roots=selected_roots,
        authorization=authorization, interactive=interactive, once=once, watch=command == "watch",
        operation_id=getattr(namespace, "operation_id", None), import_from=getattr(namespace, "import_from", None),
        config_overrides=tuple(explicit.items()), config_path=cast(Path | None, shared.get("config")),
    )


@contextmanager
def _cancellation_signals(cancel: Event) -> Iterator[list[int]]:
    import signal

    received: list[int] = []
    previous: dict[signal.Signals, Callable[[int, FrameType | None], object] | int | None] = {}

    def shutdown(number: int, frame: FrameType | None) -> None:
        if not received:
            received.append(number)
        cancel.set()

    try:
        if current_thread() is main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.getsignal(number)
                signal.signal(number, shutdown)
        yield received
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _run_application(
    config: Configuration, request: RunRequest, *, profile_path: Path | None,
    output_path: Path | None, polling: bool,
) -> int:
    from .application.serve import notify_ready
    from .bootstrap import open_pipeline
    from .domain.decisions import Decision, RunAuthorization, RunResult
    from .infrastructure.observability import RunObservability, run_profiled
    from .infrastructure.state import StateStore
    from .infrastructure.transactions import TransactionExecutor
    from .integrations.http import HttpError

    result: RunResult | None = None
    received: list[int] = []
    try:
        with (_exclusive_output(output_path) if output_path is not None else nullcontext(None)) as output:
            with open_pipeline(config, request=request) as pipeline, _cancellation_signals(pipeline.cancel) as received:
                with RunObservability(
                    log_dir=config.log_dir, log_to_file=config.log_to_file,
                    authorization=request.authorization, cancel=pipeline.cancel.is_set,
                ) as observations:
                    executor = TransactionExecutor(
                        config.state_dir, tuple(root.path for root in pipeline.roots), cancel=pipeline.cancel,
                        max_stage_bytes=config.conversion_scratch_max_bytes,
                    )
                    journal_authorization = RunAuthorization(
                        apply=request.authorization.apply or request.authorization.allow_remote,
                    )

                    def journal_ready() -> Decision:
                        return executor.prepare_state_root(authorization=journal_authorization)

                    def ready() -> None:
                        if not pipeline.cancel.is_set():
                            notify_ready()
                            print(f"kce {request.command} ready", file=sys.stderr, flush=True)

                    streamed = False

                    def stream_result(current: RunResult) -> None:
                        nonlocal streamed
                        streamed = True
                        observations.record(current)
                        _emit_json({"command": request.command, "event": "job", **current.as_dict()})

                    if request.command == "watch":
                        import regex

                        from .application.watch import WATCH_OPTIONS, run_watch
                        from .infrastructure.filesystem import BOOK_SUFFIXES

                        ignored = frozenset(config.ignored_folder_names)
                        patterns = tuple(regex.compile(value, regex.IGNORECASE) for value in config.unacceptable_keywords)

                        def candidate(path: Path) -> bool:
                            root = next((root for root in pipeline.roots if root.contains(path)), None)
                            if root is None or any(part in ignored for part in path.relative_to(root.path).parts[:-1]):
                                return False
                            return path.suffix.lower() in BOOK_SUFFIXES or (
                                config.delete_unacceptable_files_toggle and any(pattern.search(path.name) for pattern in patterns)
                            )

                        result = run_watch(
                            pipeline, request, options={name: getattr(config, name) for name in WATCH_OPTIONS},
                            state=StateStore(config.state_dir), journal_ready=journal_ready,
                            stop=pipeline.cancel, polling=polling, on_ready=ready, on_result=stream_result,
                            candidate_filter=candidate, ignored_roots=(config.state_dir, config.log_dir),
                            install_signal_handlers=False,
                        )
                    elif request.command == "qbit":
                        from .application.torrents import OPTIONS, run_torrents
                        from .integrations.http import HttpClient
                        from .integrations.komga import komga_base_url

                        if not config.uncheck_non_qbit_upgrades_toggle:
                            result = RunResult((Decision("skipped", "qbit_filter_disabled"),), skipped=1)
                        else:
                            address = config.qbittorrent_ip
                            if "://" not in address:
                                address = f"http://{address}"
                            try:
                                with HttpClient(
                                    komga_base_url(address, config.qbittorrent_port),
                                    connect_timeout=config.http_connect_timeout_seconds,
                                    read_timeout=config.http_read_timeout_seconds, max_attempts=config.http_max_attempts,
                                    cancel=pipeline.cancel,
                                ) as client:
                                    result = run_torrents(
                                        client, pipeline.library_snapshot, parsing=pipeline.parsing,
                                        matching=pipeline.matching, ranking=pipeline.ranking,
                                        options={name: getattr(config, name) for name in OPTIONS},
                                        authorization=request.authorization, state=StateStore(config.state_dir),
                                        journal_ready=journal_ready, cancel=pipeline.cancel, once=request.once,
                                        on_ready=ready, policy_version=pipeline.policy_version,
                                    )
                            except HttpError as error:
                                result = RunResult((Decision(error.status, error.reason),))
                    else:
                        result = run_profiled(lambda: pipeline.run(request), profile_path)
                        if request.command == "run" and result.exit_code in {0, 3}:
                            ready()
                    if not streamed:
                        observations.record(result)
                if observations.failure is not None:
                    result = dataclasses.replace(result, decisions=(*result.decisions, observations.failure))
            if received and not any(item.status == "cancelled" for item in result.decisions):
                result = dataclasses.replace(result, decisions=(*result.decisions, Decision("cancelled", "shutdown_requested")))
            if output is not None:
                payload = result.as_dict()
                json.dump(
                    {"schema_version": 1, "purpose": "informational", "plans": payload["plans"],
                     "decisions": payload["decisions"], "counts": payload["counts"],
                     "deferred_deliveries": payload["deferred_deliveries"]},
                    output, ensure_ascii=False,
                )
                output.write("\n")
    except _CliOperationalError:
        if result is None:
            raise
        result = dataclasses.replace(result, decisions=(*result.decisions, Decision("publication_failed", "plan_output_failed", output_path)))
    assert result is not None
    payload = result.as_dict()
    if request.command == "watch":
        payload.update({"command": "watch", "event": "summary"})
    status = 128 + received[0] if received else result.exit_code
    payload.update({"ok": status == 0, "exit_code": status})
    _emit_json(payload)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    """Return typed command exit status; import never parses arguments or starts work."""
    from .compat.legacy_cli import LegacyArgumentError

    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments and arguments[0] in _LEGACY_ROUTES else "kce"
    try:
        namespace, legacy = _parse_invocation(arguments)
        command = namespace.command
        overrides = _collect_overrides(namespace)
        shared = {name.removeprefix("root_"): value for name, value in vars(namespace).items() if name.startswith("root_")}
        shared.update({name.removeprefix("command_"): value for name, value in vars(namespace).items() if name.startswith("command_")})
        if command in {"inspect-config", "doctor", "migrate-config"}:
            if shared.get("profile") is not None or shared.get("plan_output") is not None:
                raise _CliValidationError("profile and plan outputs require an application command")
            if command == "inspect-config":
                return _inspect_config(namespace, overrides)
            if command == "doctor":
                return _doctor(namespace, overrides)
            return _migrate_config(namespace, overrides)

        config, request = _configured_request(namespace, overrides, legacy, shared)
        from .infrastructure.filesystem import FilesystemError, checked_path

        protected = (*config.paths, *config.download_folders, config.state_dir, config.log_dir,
                     *(root.path for root in request.roots),
                     *(Path(os.path.abspath(book)).parent for book in request.books))
        outputs: dict[str, Path | None] = {"profile": None, "plan_output": None}
        for flag in outputs:
            value = shared.get(flag)
            if value is None:
                continue
            path = Path(os.path.abspath(cast(Path, value)))
            if path.exists() or path.is_symlink():
                raise _CliValidationError(f"{flag} output must not already exist")
            if any(path.is_relative_to(Path(os.path.abspath(root))) for root in protected):
                raise _CliValidationError(f"{flag} output must be outside selected books, libraries, state and logs")
            try:
                checked_path(path.parent, path.parent)
                if not stat.S_ISDIR(path.parent.lstat().st_mode):
                    raise _CliValidationError(f"{flag} output parent must be a directory")
                if not os.access(path.parent, os.W_OK | os.X_OK, effective_ids=True):
                    raise _CliValidationError(f"{flag} output parent must be writable")
            except FilesystemError:
                raise _CliValidationError(f"{flag} output requires an existing nonsymlink parent") from None
            except OSError:
                raise _CliValidationError(f"{flag} output parent cannot be read") from None
            outputs[flag] = path
        if outputs["profile"] is not None and request.command not in {"extract", "run"}:
            raise _CliValidationError("profile output is supported only for extract or run")
        if outputs["profile"] is not None and outputs["profile"] == outputs["plan_output"]:
            raise _CliValidationError("profile and plan output must be different files")
        if request.command == "serve":
            if outputs["plan_output"] is not None:
                raise _CliValidationError("serve children own their plans; request plan output on the selected command")
            from .application.serve import serve
            return serve(request, config=config, config_path=request.config_path)
        return _run_application(
            config, request, profile_path=outputs["profile"], output_path=outputs["plan_output"],
            polling=bool(getattr(namespace, "polling", False)),
        )
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    except LegacyArgumentError as error:
        from .domain.decisions import Decision, RunResult, Status
        result = RunResult((Decision(cast(Status, error.status), error.reason),))
        _emit_json({"command": command, **result.as_dict()})
        _emit_diagnostic(command, error.reason)
        return result.exit_code
    except (_CliValidationError, ConfigurationError) as error:
        return _json_failure(command, error)
    except _CliOperationalError as error:
        return _json_failure(command, error, exit_code=1, reason="cli_output_failed")
    except Exception as error:
        return _json_failure(command, RuntimeError(f"operational failure ({type(error).__name__})"),
                             exit_code=1, reason="cli_operational_failure")
