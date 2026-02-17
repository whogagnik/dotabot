import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
PY_FILES = sorted(p for p in SCRIPTS_DIR.rglob("*.py") if "__pycache__" not in p.parts)
MODULE_NAMES = [".".join(p.relative_to(ROOT).with_suffix("").parts) for p in PY_FILES]
ALLOW_SYSTEM_EXIT = {"scripts.ml.collect_minimap_data"}


@pytest.mark.parametrize("module_name,path", zip(MODULE_NAMES, PY_FILES), ids=MODULE_NAMES)
def test_import_scripts_module(module_name: str, path: Path):
    assert SCRIPTS_DIR.exists()
    assert path.exists()

    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        assert module_name in ALLOW_SYSTEM_EXIT
