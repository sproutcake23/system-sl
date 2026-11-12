"""The SpellChecker must be built once per process, not once per ranking."""

import spellchecker

from system_sl.core import priority_engine_new as engine


def test_spellchecker_is_loaded_once(monkeypatch):
    constructions = []

    class CountingSpellChecker(spellchecker.SpellChecker):
        def __init__(self, *args, **kwargs):
            constructions.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(spellchecker, "SpellChecker", CountingSpellChecker)
    monkeypatch.setattr(engine, "_spellchecker", None)

    first = engine.get_spellchecker()
    second = engine.get_spellchecker()

    assert first is second
    assert len(constructions) == 1
    # The analyzer delegates to the same shared instance.
    assert engine.TaskAnalyzer._get_spell(object.__new__(engine.TaskAnalyzer)) is first
