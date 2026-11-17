"""Tests for stario.datastar.debug_inspector, the opt-in signal inspector."""

from stario.datastar import debug_inspector
from stario.markup import render


def test_debug_inspector_renders_overlay():
    html = render(debug_inspector())
    assert "__stario_debug_inspector" in html
    assert "data-ignore-morph" in html
    assert "_stario_debug_open" in html
    assert "data-json-signals" in html


def test_debug_inspector_has_no_side_effects():
    # Baked: the compiled tree can render twice identically.
    assert render(debug_inspector()) == render(debug_inspector())


def test_the_old_import_still_works_and_warns():
    import importlib
    import sys

    import pytest

    sys.modules.pop("stario.debug", None)
    with pytest.warns(DeprecationWarning, match="stario.datastar"):
        old = importlib.import_module("stario.debug")
    assert old.debug_inspector is debug_inspector
