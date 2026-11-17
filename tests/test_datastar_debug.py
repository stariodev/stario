"""Tests for stario.datastar.DebugInspector, the opt-in signal inspector."""

from stario.datastar import DebugInspector
from stario.markup import render


def test_inspector_renders_overlay():
    html = render(DebugInspector())
    assert "__stario_debug_inspector" in html
    assert "data-ignore-morph" in html
    assert "_stario_debug_open" in html
    assert "data-json-signals" in html


def test_inspector_has_no_side_effects():
    # Baked: the compiled tree can render twice identically.
    assert render(DebugInspector()) == render(DebugInspector())
