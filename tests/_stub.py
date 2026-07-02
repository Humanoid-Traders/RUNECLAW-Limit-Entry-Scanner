"""Shared getagent stub + src loader for the network-free test suite (v0.9.4).

Extracted from the boilerplate every test file used to carry. New tests import
from here; the pre-v0.9.4 suites keep their inline copies on purpose -- they are
frozen regression pins of live incidents and are not churned for cosmetics.

Usage:
    from _stub import stub_getagent, load_src
    _trade = stub_getagent()
    load_src("features")
    execution = load_src("execution")
"""
import importlib.util
import sys
import types
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"


def stub_getagent():
    """Install a fresh getagent stub (data/trade/runtime) + the src package
    anchor, and return the trade stub module for per-test wiring."""
    g = types.ModuleType("getagent")
    sys.modules["getagent"] = g
    for sub in ("data", "trade", "runtime"):
        m = types.ModuleType("getagent." + sub)
        setattr(g, sub, m)
        sys.modules["getagent." + sub] = m
    pkg = types.ModuleType("src")
    pkg.__path__ = [str(_SRC)]
    sys.modules["src"] = pkg
    return sys.modules["getagent.trade"]


def load_src(name: str):
    """Load src/<name>.py as module 'src.<name>' against the current stubs."""
    full = "src." + name
    spec = importlib.util.spec_from_file_location(full, _SRC / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod
