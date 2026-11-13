"""The decay comments must match the 3-day half-life they describe."""

from pathlib import Path

import system_sl.core.vector_converter as vc


def test_half_life_and_threshold_agree():
    source = Path(vc.__file__).read_text()
    assert "3-day half-life" in source
    assert "about 13 days" in source
    assert "0.5 = completed one half-life (3 days) ago" in source
