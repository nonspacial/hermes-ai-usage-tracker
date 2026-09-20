"""Share one runtime when the agent and FastAPI load this plugin differently."""
import importlib.util
import sys
import threading
from pathlib import Path
_LOCK = threading.RLock()
def runtime():
    with _LOCK:
        name = '_hermes_ai_usage_ledger_v2'
        if name not in sys.modules:
            root = Path(__file__).resolve().parent / 'ledger_runtime'
            spec = importlib.util.spec_from_file_location(name, root / '__init__.py', submodule_search_locations=[str(root)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return sys.modules[name]
runtime()
