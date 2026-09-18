"""Load the standalone runtime under a unique package during mixed test runs."""

from pathlib import Path
import importlib.util
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "research_runtime_app"
if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        ROOT / "app" / "__init__.py",
        submodule_search_locations=[str(ROOT / "app")],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

