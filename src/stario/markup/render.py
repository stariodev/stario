"""Render HTML trees.

The walk runs in `stario_cython.markup`: a depth-first C loop that appends
tag strings and escaped text to one list and joins it once. This module
keeps the public names.
"""

from stario_cython.markup import render, render_nodes_to_string

__all__ = ["render", "render_nodes_to_string"]
