"""Keep offline-reference tests independent of the server's live policy.

Railway runs pytest before starting the server and passes live environment
variables to that subprocess. Tests opt into LUDO explicitly with monkeypatch;
the production application never imports or executes this fixture.
"""
import pytest


@pytest.fixture(autouse=True)
def reference_entry_policy(monkeypatch):
    monkeypatch.delenv('KAIROS_ENTRY_STRUCTURE', raising=False)
    monkeypatch.delenv('KAIROS_M15_PRIMARY_FVG', raising=False)
    monkeypatch.delenv('KAIROS_FVG_FORCE_SHADOW', raising=False)
