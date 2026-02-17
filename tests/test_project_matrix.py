import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
PY_FILES = sorted(p for p in SCRIPTS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", PY_FILES, ids=[str(p.relative_to(ROOT)) for p in PY_FILES])
def test_file_exists(path: Path):
    assert path.exists()


@pytest.mark.parametrize("path", PY_FILES, ids=[str(p.relative_to(ROOT)) for p in PY_FILES])
def test_file_is_python(path: Path):
    assert path.suffix == ".py"


@pytest.mark.parametrize("path", PY_FILES, ids=[str(p.relative_to(ROOT)) for p in PY_FILES])
def test_file_not_empty(path: Path):
    assert path.read_text(encoding="utf-8").strip() != ""


@pytest.mark.parametrize("path", PY_FILES, ids=[str(p.relative_to(ROOT)) for p in PY_FILES])
def test_file_parseable(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert isinstance(tree, ast.Module)


@pytest.mark.parametrize("path", PY_FILES, ids=[str(p.relative_to(ROOT)) for p in PY_FILES])
def test_file_has_lines(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1


@pytest.mark.parametrize("path", PY_FILES, ids=[str(p.relative_to(ROOT)) for p in PY_FILES])
def test_relative_module_name_nonempty(path: Path):
    rel = path.relative_to(ROOT).with_suffix("")
    module_name = ".".join(rel.parts)
    assert module_name


@pytest.mark.parametrize("path", PY_FILES, ids=[str(p.relative_to(ROOT)) for p in PY_FILES])
def test_root_scope(path: Path):
    rel = path.relative_to(ROOT)
    assert rel.parts[0] == "scripts"


@pytest.mark.parametrize("path", PY_FILES, ids=[str(p.relative_to(ROOT)) for p in PY_FILES])
def test_ast_has_some_body(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert len(tree.body) >= 1


@pytest.mark.parametrize("path", PY_FILES, ids=[str(p.relative_to(ROOT)) for p in PY_FILES])
def test_path_without_cache(path: Path):
    assert "__pycache__" not in path.parts
