# backend/tests/test_smoke.py
from agent.market_intelligence import __version__

def test_module_imports():
    assert isinstance(__version__, str)
