"""Generate a deterministic, agent-friendly inventory of the repository."""

# Run with "poetry run python tools/generate_project_map.py" to generate the project map.

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT = "project-map.yaml"
DEFAULT_OVERRIDES = "project-map-overrides.json"


def _normalise_path(path: str | Path) -> str:
    """Return a repository-relative path with POSIX separators."""
    return Path(path).as_posix().removeprefix("./")


def discover_repository_files(root: Path, output: Path) -> list[str]:
    """Return tracked and non-ignored working-tree files.

    Including non-ignored untracked files means a newly created file appears in the
    map before it is staged. The output is included explicitly so that the map can
    describe itself on its first generation.
    """
    command = [
        "git",
        "-C",
        str(root),
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=False,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "project-map generation requires a Git working tree and the git CLI"
        ) from exc

    files = {
        _normalise_path(raw.decode("utf-8", errors="surrogateescape"))
        for raw in result.stdout.split(b"\0")
        if raw
    }
    files = {path for path in files if (root / path).is_file()}

    try:
        relative_output = output.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative_output = None
    if relative_output:
        files.add(relative_output)

    return sorted(files, key=str.casefold)


def load_overrides(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Load human-authored directory and file metadata."""
    if not path.exists():
        return {"directories": {}, "files": {}}

    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("Project-map overrides must be a JSON object")

    overrides: dict[str, dict[str, dict[str, Any]]] = {}
    for section in ("directories", "files"):
        raw_section = document.get(section, {})
        if not isinstance(raw_section, dict):
            raise ValueError(f"{section!r} overrides must be a JSON object")
        overrides[section] = {}
        for raw_path, metadata in raw_section.items():
            if not isinstance(metadata, dict):
                raise ValueError(f"Override for {raw_path!r} must be a JSON object")
            overrides[section][_normalise_path(raw_path)] = dict(metadata)
    return overrides


def _module_name(relative_path: str) -> str:
    path = Path(relative_path)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_import(
    imported_module: str | None,
    level: int,
    current_module: str,
    is_package: bool,
) -> str:
    if level == 0:
        return imported_module or ""

    package_parts = current_module.split(".") if current_module else []
    if not is_package and package_parts:
        package_parts.pop()
    trim = max(level - 1, 0)
    if trim:
        package_parts = package_parts[:-trim]
    if imported_module:
        package_parts.extend(imported_module.split("."))
    return ".".join(package_parts)


def inspect_python_file(path: Path, relative_path: str) -> dict[str, Any]:
    """Extract module metadata without executing the module."""
    try:
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=relative_path)
    except (OSError, SyntaxError, UnicodeError) as exc:
        return {"parse_error": f"{type(exc).__name__}: {exc}"}

    module_name = _module_name(relative_path)
    is_package = Path(relative_path).name == "__init__.py"
    imports: set[str] = set()
    symbols: list[dict[str, Any]] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_"):
                continue
            if isinstance(node, ast.ClassDef):
                kind = "class"
            elif isinstance(node, ast.AsyncFunctionDef):
                kind = "async_function"
            else:
                kind = "function"
            symbols.append({"name": node.name, "kind": kind, "line": node.lineno})
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and not target.id.startswith("_")
                    and target.id.isupper()
                ):
                    symbols.append(
                        {"name": target.id, "kind": "constant", "line": node.lineno}
                    )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import(
                node.module,
                node.level,
                module_name,
                is_package,
            )
            if resolved:
                imports.add(resolved)

    metadata: dict[str, Any] = {"module": module_name}
    docstring = ast.get_docstring(tree, clean=True)
    if docstring:
        summary = " ".join(docstring.split())
        metadata["summary"] = summary[:197] + "..." if len(summary) > 200 else summary
    if symbols:
        metadata["public_symbols"] = sorted(
            symbols, key=lambda item: (item["line"], item["name"])
        )
    metadata["_imports"] = sorted(imports, key=str.casefold)
    return metadata


def _file_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".yaml", ".yml", ".toml", ".json", ".ini"}:
        return "configuration"
    if suffix == ".md":
        return "documentation"
    if suffix in {".lock", ".txt"}:
        return "dependency"
    if suffix in {".prom"}:
        return "metrics"
    return suffix.removeprefix(".") or "file"


def _all_directories(files: Sequence[str]) -> list[str]:
    directories = {"."}
    for file_path in files:
        parent = Path(file_path).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return sorted(directories, key=lambda value: (value != ".", value.casefold()))


def _poetry_entry_points(root: Path) -> dict[str, list[str]]:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return {}

    # Only this small TOML section is needed. Parsing it directly keeps the
    # developer tool compatible with the project's supported Python 3.10, where
    # the standard-library tomllib module is not yet available.
    scripts: dict[str, str] = {}
    in_scripts_section = False
    for raw_line in pyproject.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("["):
            in_scripts_section = line == "[tool.poetry.scripts]"
            continue
        if (
            not in_scripts_section
            or not line
            or line.startswith("#")
            or "=" not in line
        ):
            continue
        raw_command, _, raw_target = line.partition("=")
        command = raw_command.strip().strip('"').strip("'")
        try:
            target = json.loads(raw_target.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(target, str):
            scripts[command] = target

    entry_points: dict[str, list[str]] = {}
    for command, target in scripts.items():
        module = target.partition(":")[0]
        relative_path = module.replace(".", "/") + ".py"
        entry_points.setdefault(relative_path, []).append(command)
    return entry_points


def _test_relationships(
    files: Sequence[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    python_sources = [
        path for path in files if path.endswith(".py") and not path.startswith("tests/")
    ]
    sources_by_stem: dict[str, list[str]] = {}
    for source in python_sources:
        sources_by_stem.setdefault(Path(source).stem, []).append(source)

    source_tests: dict[str, list[str]] = {}
    test_targets: dict[str, list[str]] = {}
    for test in files:
        test_path = Path(test)
        if not test.startswith("tests/") or not test_path.name.startswith("test_"):
            continue
        target_stem = test_path.stem.removeprefix("test_")
        candidates = sorted(sources_by_stem.get(target_stem, []), key=str.casefold)
        if not candidates:
            continue
        test_targets[test] = candidates
        for source in candidates:
            source_tests.setdefault(source, []).append(test)
    return source_tests, test_targets


def build_project_map(
    root: Path,
    files: Sequence[str],
    overrides: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Build the serialisable project-map document."""
    files = sorted({_normalise_path(path) for path in files}, key=str.casefold)
    directories = _all_directories(files)
    directory_set = set(directories)
    file_set = set(files)

    unknown_directories = set(overrides.get("directories", {})) - directory_set
    unknown_files = set(overrides.get("files", {})) - file_set
    if unknown_directories or unknown_files:
        messages = []
        if unknown_directories:
            messages.append(f"unknown directories: {sorted(unknown_directories)}")
        if unknown_files:
            messages.append(f"unknown files: {sorted(unknown_files)}")
        raise ValueError("Invalid project-map overrides; " + "; ".join(messages))

    local_roots = {
        Path(path).parts[0]
        for path in files
        if path.endswith(".py") and Path(path).parts
    }
    local_roots.update(
        Path(path).stem for path in files if path.endswith(".py") and "/" not in path
    )
    entry_points = _poetry_entry_points(root)
    source_tests, test_targets = _test_relationships(files)

    file_records: list[dict[str, Any]] = []
    python_count = 0
    symbol_count = 0
    for relative_path in files:
        record: dict[str, Any] = {
            "path": relative_path,
            "kind": _file_kind(relative_path),
        }
        if relative_path.endswith(".py"):
            python_count += 1
            python_metadata = inspect_python_file(root / relative_path, relative_path)
            raw_imports = python_metadata.pop("_imports", [])
            local_imports = [
                module
                for module in raw_imports
                if module.split(".", maxsplit=1)[0] in local_roots
            ]
            record.update(python_metadata)
            if local_imports:
                record["local_imports"] = local_imports
            symbol_count += len(record.get("public_symbols", []))
        if relative_path in entry_points:
            record["entry_points"] = sorted(
                entry_points[relative_path], key=str.casefold
            )
        if relative_path in source_tests:
            record["tests"] = sorted(source_tests[relative_path], key=str.casefold)
        if relative_path in test_targets:
            record["test_targets"] = test_targets[relative_path]
        record.update(overrides.get("files", {}).get(relative_path, {}))
        file_records.append(record)

    directory_records: list[dict[str, Any]] = []
    for directory in directories:
        prefix = "" if directory == "." else directory + "/"
        descendant_files = [path for path in files if path.startswith(prefix)]
        direct_init = f"{prefix}__init__.py"
        record = {
            "path": directory,
            "kind": "python_package" if direct_init in file_set else "directory",
            "file_count": len(descendant_files),
            "python_module_count": sum(
                path.endswith(".py") for path in descendant_files
            ),
        }
        record.update(overrides.get("directories", {}).get(directory, {}))
        directory_records.append(record)

    return {
        "schema_version": 1,
        "generated": True,
        "generated_by": "tools/generate_project_map.py",
        "source": "git tracked and non-ignored working-tree files",
        "stats": {
            "directory_count": len(directory_records),
            "file_count": len(file_records),
            "python_module_count": python_count,
            "public_symbol_count": symbol_count,
        },
        "directories": directory_records,
        "files": file_records,
    }


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _emit_yaml(value: Any, indent: int = 0) -> Iterable[str]:
    prefix = " " * indent
    if isinstance(value, Mapping):
        for key, item in value.items():
            rendered_key = (
                str(key) if str(key).replace("_", "").isalnum() else _yaml_scalar(key)
            )
            if isinstance(item, (Mapping, list)):
                if not item:
                    empty = "{}" if isinstance(item, Mapping) else "[]"
                    yield f"{prefix}{rendered_key}: {empty}"
                else:
                    yield f"{prefix}{rendered_key}:"
                    yield from _emit_yaml(item, indent + 2)
            else:
                yield f"{prefix}{rendered_key}: {_yaml_scalar(item)}"
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (Mapping, list)):
                if not item:
                    empty = "{}" if isinstance(item, Mapping) else "[]"
                    yield f"{prefix}- {empty}"
                else:
                    yield f"{prefix}-"
                    yield from _emit_yaml(item, indent + 2)
            else:
                yield f"{prefix}- {_yaml_scalar(item)}"
    else:
        yield f"{prefix}{_yaml_scalar(value)}"


def render_project_map(document: Mapping[str, Any]) -> str:
    header = (
        "# GENERATED FILE - DO NOT EDIT DIRECTLY.\n"
        "# Run: poetry run python tools/generate_project_map.py\n"
        "# Human-authored metadata belongs in project-map-overrides.json.\n"
    )
    return header + "\n".join(_emit_yaml(document)) + "\n"


def write_or_check(output: Path, content: str, check: bool) -> int:
    current = output.read_text(encoding="utf-8") if output.exists() else None
    if current == content:
        print(f"Project map is up to date: {output}")
        return 0
    if check:
        print(
            f"Project map is stale: {output}\n"
            "Run `poetry run python tools/generate_project_map.py` and commit "
            "the result.",
            file=sys.stderr,
        )
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    print(f"Generated project map: {output}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the generated map is stale",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to this script's repository)",
    )
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    parser.add_argument("--overrides", type=Path, default=Path(DEFAULT_OVERRIDES))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    overrides_path = (
        args.overrides if args.overrides.is_absolute() else root / args.overrides
    )
    try:
        files = discover_repository_files(root, output)
        overrides = load_overrides(overrides_path)
        document = build_project_map(root, files, overrides)
        content = render_project_map(document)
        return write_or_check(output, content, args.check)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Project-map generation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
