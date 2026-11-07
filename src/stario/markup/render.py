"""Render HTML trees.

The walk runs in `stario_cython.markup`: a depth-first C loop that copies
tag strings and escaped text into one UTF-8 buffer. `render_bytes` returns
that buffer as bytes; `render` decodes it once. This module
keeps the public names.
"""

from stario_cython.markup import render, render_bytes, render_nodes_to_string

__all__ = ["render", "render_bytes", "render_nodes_to_string"]
