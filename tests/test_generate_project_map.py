from pathlib import Path

from tools.generate_project_map import (
    build_project_map,
    inspect_python_file,
    render_project_map,
    write_or_check,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_inspect_python_file_extracts_public_symbols_and_imports(tmp_path):
    module = tmp_path / "pkg" / "sample.py"
    _write(
        module,
        '''"""A sample module used by the project-map test."""

import core.clients
from .helper import HELP

PUBLIC_SETTING = True
_PRIVATE_SETTING = False

class Example:
    pass

async def fetch_data():
    return HELP

def _private_helper():
    return None
''',
    )

    metadata = inspect_python_file(module, "pkg/sample.py")

    assert metadata["module"] == "pkg.sample"
    assert metadata["summary"] == "A sample module used by the project-map test."
    assert metadata["_imports"] == ["core.clients", "pkg.helper"]
    assert [symbol["name"] for symbol in metadata["public_symbols"]] == [
        "PUBLIC_SETTING",
        "Example",
        "fetch_data",
    ]
    assert [symbol["kind"] for symbol in metadata["public_symbols"]] == [
        "constant",
        "class",
        "async_function",
    ]


def test_build_project_map_merges_overrides_entry_points_and_tests(tmp_path):
    _write(tmp_path / "pkg" / "__init__.py", '"""Example package."""\n')
    _write(tmp_path / "pkg" / "worker.py", "class Worker:\n    pass\n")
    _write(
        tmp_path / "tests" / "test_worker.py",
        "from pkg.worker import Worker\n\ndef test_worker():\n    assert Worker\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        '[tool.poetry.scripts]\nrun-worker = "pkg.worker:main"\n',
    )
    files = [
        "pkg/__init__.py",
        "pkg/worker.py",
        "pyproject.toml",
        "tests/test_worker.py",
    ]
    overrides = {
        "directories": {"pkg": {"purpose": "Run background work."}},
        "files": {"pkg/worker.py": {"stability": "internal"}},
    }

    document = build_project_map(tmp_path, files, overrides)
    directories = {item["path"]: item for item in document["directories"]}
    records = {item["path"]: item for item in document["files"]}

    assert directories["pkg"]["kind"] == "python_package"
    assert directories["pkg"]["purpose"] == "Run background work."
    assert records["pkg/worker.py"]["entry_points"] == ["run-worker"]
    assert records["pkg/worker.py"]["tests"] == ["tests/test_worker.py"]
    assert records["tests/test_worker.py"]["test_targets"] == ["pkg/worker.py"]
    assert records["pkg/worker.py"]["stability"] == "internal"
    assert document["stats"]["public_symbol_count"] == 2


def test_render_project_map_is_deterministic_yaml():
    document = {
        "schema_version": 1,
        "generated": True,
        "files": [{"path": "a.py", "symbols": ["Example"]}],
    }

    first = render_project_map(document)
    second = render_project_map(document)

    assert first == second
    assert first.startswith("# GENERATED FILE - DO NOT EDIT DIRECTLY.")
    assert 'path: "a.py"' in first
    assert 'symbols:\n      - "Example"' in first


def test_write_or_check_detects_and_repairs_stale_map(tmp_path):
    output = tmp_path / "project-map.yaml"

    assert write_or_check(output, "first\n", check=True) == 1
    assert not output.exists()
    assert write_or_check(output, "first\n", check=False) == 0
    assert output.read_text(encoding="utf-8") == "first\n"
    assert write_or_check(output, "first\n", check=True) == 0
    assert write_or_check(output, "second\n", check=True) == 1
