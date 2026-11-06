# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, freethreading_compatible=True
# cython: auto_pickle=False
"""Element builder and render walk for ``stario.markup``.

``Tag.__call__`` builds one ``Element`` (the ``(tag_start, attrs, children,
tail)`` tuple subclass from ``stario.markup.types``) and ``render`` walks a
tree into one string. Both run in C: per child one type-identity switch,
per attribute one dict lookup for the validated key and one scan for the
escape, no Python call. Text escaping returns the same object when nothing
needs escaping.

The Python modules ``stario.markup.tag``, ``.render`` and ``.escape`` re-export
from here. Python symbols this module needs (``Element``, ``SafeString``,
``Attrs``, ``BakeSlot``, ``AttrSlot``, ``validate_attribute_key``) are bound on
first use so the two packages can import each other.
"""

import re

from cpython.list cimport PyList_Append, PyList_GET_ITEM, PyList_GET_SIZE
from cpython.object cimport PyObject
from cpython.ref cimport Py_INCREF
from cpython.tuple cimport PyTuple_GET_ITEM, PyTuple_GET_SIZE

from stario.exceptions import StarioError

cdef extern from "Python.h":
    ctypedef struct PyTypeObject
    object PyType_GenericAlloc(object type, Py_ssize_t nitems)
    int PyUnicode_KIND(object o)
    void* PyUnicode_DATA(object o)
    Py_ssize_t PyUnicode_GET_LENGTH(object o)
    unsigned int PyUnicode_MAX_CHAR_VALUE(object o)
    object PyUnicode_New(Py_ssize_t size, unsigned int maxchar)
    int PyUnicode_1BYTE_KIND
    PyObject* PyDict_GetItemWithError(object d, object key) except? NULL
    int PyDict_Next(object d, Py_ssize_t* pos, PyObject** key, PyObject** value)

cdef extern from *:
    """
    #include <Python.h>

    /* Element(tuple) without tuple.__new__: allocate the subtype with four
       slots and fill them, as tuple_subtype_new does. */
    static PyObject* stario_new_element(
        PyObject* cls, PyObject* a, PyObject* b, PyObject* c, PyObject* d
    ) {
        PyObject* e = PyType_GenericAlloc((PyTypeObject*)cls, 4);
        if (e == NULL) {
            return NULL;
        }
        Py_INCREF(a); PyTuple_SET_ITEM(e, 0, a);
        Py_INCREF(b); PyTuple_SET_ITEM(e, 1, b);
        Py_INCREF(c); PyTuple_SET_ITEM(e, 2, c);
        Py_INCREF(d); PyTuple_SET_ITEM(e, 3, d);
        return e;
    }
    """
    object stario_new_element(object cls, object a, object b, object c, object d)


# ---------------------------------------------------------------------------
# Python symbols, bound on first use (see the module docstring).

cdef object _Element = None
cdef object _SafeString = None
cdef object _Attrs = None
cdef object _BakeSlot = None
cdef object _AttrSlot = None
cdef object _RawSlot = None
cdef object _slot_name = None
cdef object _validate_attribute_key = None
cdef object _Mapping = None
cdef bint _bound = False

# Validated attribute names: str -> str, so the hot path is one dict lookup.
cdef dict _VALID_KEYS = {}
cdef int _VALID_KEYS_MAX = 4096

cdef object _TAG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
cdef frozenset _EMPTY_MODES = frozenset({"normal", "void", "self_closing_when_empty"})
cdef object _GT = ">"
cdef int _MAX_DEPTH = 1000


cdef void _bind():
    global _Element, _SafeString, _Attrs, _BakeSlot, _AttrSlot, _RawSlot
    global _slot_name, _validate_attribute_key, _Mapping, _bound
    if _bound:
        return
    from collections.abc import Mapping
    from stario.markup.escape import validate_attribute_key
    from stario.markup.slots import AttrSlot, BakeSlot, RawSlot, slot_name
    from stario.markup.types import Attrs, Element, SafeString
    _Element = Element
    _SafeString = SafeString
    _Attrs = Attrs
    _BakeSlot = BakeSlot
    _AttrSlot = AttrSlot
    _RawSlot = RawSlot
    _slot_name = slot_name
    _validate_attribute_key = validate_attribute_key
    _Mapping = Mapping
    _bound = True


# ---------------------------------------------------------------------------
# Escaping

cdef object _escape_1byte(object s, bint quotes):
    """Escape a Latin-1 / ASCII ``str`` in one C pass; same object if clean."""
    cdef const unsigned char* src = <const unsigned char*>PyUnicode_DATA(s)
    cdef Py_ssize_t n = PyUnicode_GET_LENGTH(s)
    cdef Py_ssize_t i
    cdef Py_ssize_t extra = 0
    cdef Py_ssize_t w = 0
    cdef unsigned char c
    cdef unsigned char* dst
    cdef object out
    for i in range(n):
        c = src[i]
        if c == 38:
            extra += 4
        elif c == 60 or c == 62:
            extra += 3
        elif quotes and c == 34:
            extra += 5
    if extra == 0:
        return s
    out = PyUnicode_New(n + extra, PyUnicode_MAX_CHAR_VALUE(s))
    dst = <unsigned char*>PyUnicode_DATA(out)
    for i in range(n):
        c = src[i]
        if c == 38:
            dst[w] = 38; dst[w + 1] = 97; dst[w + 2] = 109; dst[w + 3] = 112; dst[w + 4] = 59
            w += 5
        elif c == 60:
            dst[w] = 38; dst[w + 1] = 108; dst[w + 2] = 116; dst[w + 3] = 59
            w += 4
        elif c == 62:
            dst[w] = 38; dst[w + 1] = 103; dst[w + 2] = 116; dst[w + 3] = 59
            w += 4
        elif quotes and c == 34:
            dst[w] = 38; dst[w + 1] = 113; dst[w + 2] = 117; dst[w + 3] = 111
            dst[w + 4] = 116; dst[w + 5] = 59
            w += 6
        else:
            dst[w] = c
            w += 1
    return out


cdef inline object c_escape_text(object s):
    if PyUnicode_KIND(s) == PyUnicode_1BYTE_KIND:
        return _escape_1byte(s, False)
    if "&" not in s and "<" not in s and ">" not in s:
        return s
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


cdef inline object c_escape_attr(object s):
    if PyUnicode_KIND(s) == PyUnicode_1BYTE_KIND:
        return _escape_1byte(s, True)
    if "&" not in s and "<" not in s and ">" not in s and '"' not in s:
        return s
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def escape_text(str s not None):
    """Escape `&`, `<`, `>` for text nodes (quotes stay literal).

    Returns the same object when nothing needs escaping; text nodes are
    mostly clean user copy, so no cache is kept.
    """
    return c_escape_text(s)


def escape_attribute_value(str s not None):
    """Escape for double-quoted attribute values.

    Single quotes stay literal: stario always emits attribute values inside
    double quotes, so only `&`, `<`, `>`, and `"` need escaping. This
    keeps Datastar/JS expressions with string literals readable on the wire.
    """
    return c_escape_attr(s)


def check_raw_text(str text not None, str tag not None):
    """Raise if ``text`` could end a raw-text element named ``tag`` early."""
    _check_raw_text(text, tag)


cdef int _check_raw_text(object text, object tag) except -1:
    cdef object lowered = text.lower()
    cdef bint script = tag == "script"
    if f"</{tag}" in lowered or (script and "<!--" in lowered):
        raise StarioError(
            f"Text inside <{tag}> would end the element early",
            context={"tag": tag, "text": text[:100]},
            help_text=(
                f"Raw text in <{tag}> is sent verbatim, so it must "
                f"not contain '</{tag}'"
                + (" or '<!--'" if script else "")
                + ". Build it differently, or pass a SafeString if you "
                "control the bytes."
            ),
            example='h.Script("const tag = \'<\\\\/script>\';")',
        )
    return 0


# ---------------------------------------------------------------------------
# Tag

cdef object _attrs_before_children_error(object tag_repr):
    return StarioError(
        "HTML attributes must be passed before children",
        context={"tag": tag_repr},
        help_text=(
            "Pass attribute mappings and Attrs fragments first, then child nodes."
        ),
        example='h.Div(classes("card"), h.Span("ok"))',
    )


cdef inline object _valid_key(object raw_key):
    """The validated attribute name: one dict hit, else the Python validator."""
    cdef PyObject* hit
    if type(raw_key) is str:
        hit = PyDict_GetItemWithError(_VALID_KEYS, raw_key)
        if hit != NULL:
            return <object>hit
    key = _validate_attribute_key(raw_key)
    if len(_VALID_KEYS) >= _VALID_KEYS_MAX:
        _VALID_KEYS.clear()
    _VALID_KEYS[key] = key
    return key


cdef object _attr_fragment(object key, object value):
    """`` key="value"`` for one scalar value, or None to omit (matches ``wire``)."""
    cdef object value_type = type(value)
    if value_type is str:
        return f' {key}="{c_escape_attr(value)}"'
    if value_type is _SafeString:
        return f' {key}="{value.rendered}"'
    if value is True:
        return f" {key}"
    if value is None or value is False:
        return None
    if value_type is int or value_type is float:
        return f' {key}="{value}"'
    raise StarioError(
        f"Invalid value type for attribute '{key}': {type(value).__name__}",
        context={
            "attribute": key,
            "value_type": type(value).__name__,
            "value": str(value)[:100],
        },
        help_text=(
            "HTML attributes support scalar values only: str, SafeString, int, "
            "float, bool, or None. Use helpers like classes(), styles(), or data() "
            "for common formatting."
        ),
        example='h.Div({"id": "main"})',
    )


cdef class Tag:
    """Callable factory for a single HTML or SVG element name.

    Instantiate with the constructor (e.g. `P = Tag("p")`). Prefer the
    built-in catalogs in `stario.markup.html` and `stario.markup.svg` where they
    exist. The factory caches opening/closing strings and, for tags with no
    arguments, a ready `SafeString` (avoid mutating these attributes after init).

    `empty="normal"` — no children renders `<tag></tag>`; children allowed.
    `empty="void"` — no children renders `<tag/>`; children are an error
    (HTML `br`, `img`, `input`, …).
    `empty="self_closing_when_empty"` — no children renders `<tag/>`; children
    allowed when present (SVG leaves like `circle`, `path`, filter primitives).

    `raw_text=True` marks a raw-text element (`script`, `style`): the browser
    does not decode entities inside it, so `str` children go on the wire
    verbatim instead of text-escaped. Text that could end the element early
    (`</script`, `<!--`) is an error.

    Calling the instance (`h.Div(...)`, `h.P("hi")`, etc.) builds a tree node;
    see `Tag.__call__`.
    """

    cdef readonly str name
    cdef readonly str empty_mode
    cdef readonly bint raw_text
    cdef readonly str tag_start
    cdef readonly str tag_start_no_attrs
    cdef readonly str closing_tag
    cdef readonly str no_children_close
    cdef readonly object empty
    cdef bint _void

    def __init__(
        self,
        str name not None,
        *,
        empty="normal",
        str prefix not None = "",
        bint raw_text=False,
    ):
        """Create a tag factory.

        `name` is the element name on the wire (e.g. `"div"`, `"circle"`).
        Use `empty="void"` for HTML void elements (children are an error), or
        `empty="self_closing_when_empty"` for SVG-style leaves that self-close
        only when called with no children. Optional `prefix` is prepended to the
        opening tag (e.g. `<!doctype html>` on `HtmlDocument`). `raw_text=True`
        is for `script` and `style`, whose text is not entity-decoded.
        """
        _bind()
        if not _TAG_NAME_RE.fullmatch(name):
            raise StarioError(
                f"Invalid tag name: {name!r}",
                context={"tag": name[:100]},
                help_text=(
                    "Tag names must start with an ASCII letter and then contain only "
                    "ASCII letters, digits, or hyphens."
                ),
                example='CustomElement = Tag("my-widget")',
            )
        if empty not in _EMPTY_MODES:
            raise StarioError(
                f"Invalid empty mode for <{name}>: {empty!r}",
                context={"tag": name, "empty": str(empty)},
                help_text=(
                    'Use empty="normal", empty="void", or '
                    'empty="self_closing_when_empty".'
                ),
            )
        self.name = name
        self.empty_mode = empty
        self.raw_text = raw_text
        self._void = empty == "void"
        self.tag_start = prefix + "<" + name
        self.tag_start_no_attrs = self.tag_start + ">"
        self.closing_tag = f"</{name}>"
        self.no_children_close = "/>" if empty != "normal" else ">" + self.closing_tag
        self.empty = _SafeString(self.tag_start + self.no_children_close)

    def _repr(self):
        return f"Tag(name='{self.name}', empty='{self.empty_mode}')"

    def __repr__(self):
        return self._repr()

    cdef int _raw_walk(self, object child, list run, list out, bint* slots) except -1:
        """Collect raw-text content in document order.

        ``str`` pieces and ``@baked`` slots build up one *run*; lists are
        flattened into it. Anything else (a ``SafeString`` the caller vouches
        for, a number, ``None``) ends the run and passes through untouched.
        """
        cdef object child_type = type(child)
        if child_type is str:
            PyList_Append(run, child)
        elif child_type is list:
            for item in <list>child:
                self._raw_walk(item, run, out, slots)
        elif child_type is _BakeSlot:
            PyList_Append(run, child)
            slots[0] = True
        else:
            self._raw_flush(run, out, slots)
            if child is not None:
                PyList_Append(out, child)
        return 0

    cdef int _raw_flush(self, list run, list out, bint* slots) except -1:
        """Close a run: check it once, joined, and emit it as one child.

        The wire concatenates children, so ``"</scr"`` next to ``"ipt>"``
        must fail like ``"</script>"`` does. Without slots the run becomes a
        single ``SafeString``. With slots it becomes a ``RawSlot`` whose
        ``parts`` alternate literal text and slots: adjacent literals are
        joined and checked now, and the splice checks the whole run again
        with the values in place.
        """
        cdef list parts
        cdef list literal
        cdef object text
        if not run:
            return 0
        if not slots[0]:
            text = "".join(run)
            _check_raw_text(text, self.name)
            PyList_Append(out, _SafeString(text))
        else:
            parts = []
            literal = []
            for item in run:
                if type(item) is str:
                    PyList_Append(literal, item)
                    continue
                if literal:
                    text = "".join(literal)
                    _check_raw_text(text, self.name)
                    PyList_Append(parts, text)
                    del literal[:]
                PyList_Append(parts, item)
            if literal:
                text = "".join(literal)
                _check_raw_text(text, self.name)
                PyList_Append(parts, text)
            PyList_Append(out, _RawSlot(self.name, tuple(parts)))
            slots[0] = False
        del run[:]
        return 0

    cdef list _raw_text_children(self, list children):
        cdef list run = []
        cdef list out = []
        cdef bint slots = False
        for child in children:
            self._raw_walk(child, run, out, &slots)
        self._raw_flush(run, out, &slots)
        return out

    cdef int _add_mapping(
        self, object mapping, list attribute_parts, bint* has_slots
    ) except -1:
        cdef Py_ssize_t pos = 0
        cdef PyObject* kp
        cdef PyObject* vp
        cdef object key
        cdef object fragment
        if type(mapping) is dict:
            while PyDict_Next(mapping, &pos, &kp, &vp):
                self._add_attr(<object>kp, <object>vp, attribute_parts, has_slots)
            return 0
        for raw_key, raw_value in mapping.items():
            self._add_attr(raw_key, raw_value, attribute_parts, has_slots)
        return 0

    cdef inline int _add_attr(
        self, object raw_key, object raw_value, list attribute_parts, bint* has_slots
    ) except -1:
        cdef object key
        cdef object value_type
        cdef object fragment
        if type(raw_key) is _BakeSlot:
            raise StarioError(
                "@baked: parameters cannot be used as attribute names",
                context={"tag": self._repr()},
                help_text=(
                    "Parameters may hold attribute values or children, "
                    "never attribute names."
                ),
            )
        key = _valid_key(raw_key)
        value_type = type(raw_value)
        if value_type is str:
            PyList_Append(attribute_parts, f' {key}="{c_escape_attr(raw_value)}"')
        elif value_type is _BakeSlot:
            PyList_Append(attribute_parts, _AttrSlot(key, _slot_name(raw_value)))
            has_slots[0] = True
        else:
            fragment = _attr_fragment(key, raw_value)
            if fragment is not None:
                PyList_Append(attribute_parts, fragment)
        return 0

    def __call__(self, *children):
        """Build one element from positional arguments.

        Children are processed in order:

        - Each initial mapping (`dict` or other `Mapping`) is rendered as flat
          `key=value` attributes in the order it appears. Several mappings in a
          row are allowed, but they are not merged or deduplicated; duplicate
          attributes render as duplicate attributes. `None` entries are skipped.
        - The first non-mapping starts child content. After that, mappings must
          not appear (attributes must come first).

        Return value: an `Element` (a `(open, attrs, children, tail)` tuple
        subclass), or a cached `SafeString` when there are no arguments. With
        no attributes, `open` is the tag start including `>`
        (`tag_start_no_attrs`); otherwise `open` is `"<name"` and `attrs` is a
        joined string or slot list. `children` is `None` for empty elements;
        `tail` is `no_children_close` or `closing_tag`.
        """
        cdef Py_ssize_t n = PyTuple_GET_SIZE(children)
        cdef Py_ssize_t i
        cdef object child
        cdef object child_type
        cdef list child_elements = None
        cdef list attribute_parts = None
        cdef bint has_slots = False
        cdef object open_tag
        cdef object attrs
        if n == 0:
            return self.empty

        for i in range(n):
            child = <object>PyTuple_GET_ITEM(children, i)
            child_type = type(child)

            # Child content first: it is what most calls carry.
            if (
                child_type is str
                or child_type is _Element
                or child_type is _SafeString
                or child_type is list
                or child_type is int
                or child_type is float
            ):
                if child_elements is None:
                    child_elements = [child]
                else:
                    PyList_Append(child_elements, child)
                continue

            if child is None:
                continue

            # `Attrs` fragments are pre-rendered attribute strings.
            if child_type is _Attrs:
                if child_elements is not None:
                    raise _attrs_before_children_error(self._repr())
                if attribute_parts is None:
                    attribute_parts = [child.rendered]
                else:
                    PyList_Append(attribute_parts, child.rendered)
                continue

            # {key: value} attribute mappings are merged into the opening tag.
            if child_type is dict or isinstance(child, _Mapping):
                if child_elements is not None:
                    raise _attrs_before_children_error(self._repr())
                if attribute_parts is None:
                    attribute_parts = []
                self._add_mapping(child, attribute_parts, &has_slots)
                continue

            # Anything else (a @baked slot, or a type render will reject) is
            # child content.
            if child_elements is None:
                child_elements = [child]
            else:
                PyList_Append(child_elements, child)

        # Void elements cannot have children.
        if child_elements is not None and self._void:
            raise StarioError(
                f"<{self.name}> is a void element and cannot have children",
                context={"tag": self._repr()},
                help_text=(
                    "Void elements (br, img, input, ...) always render as a single "
                    "self-closing tag. Put the content in a sibling or parent element."
                ),
                example='h.Div(h.Img({"src": "x.png", "alt": "X"}), "caption")',
            )

        if child_elements is not None and self.raw_text:
            child_elements = self._raw_text_children(child_elements)

        # Build the final leaf or branch node (always an Element except cached empty).
        if not attribute_parts and child_elements is None:
            return self.empty

        if not attribute_parts:
            # No attributes: <name> or <name>/>.
            open_tag = self.tag_start_no_attrs
            attrs = None
        elif has_slots:
            # Bake-time only: @baked slots in attributes.
            open_tag = self.tag_start
            attrs = attribute_parts
        else:
            # Normal attributes: <name attr1="value1" attr2="value2">.
            open_tag = self.tag_start
            attrs = "".join(attribute_parts)

        if child_elements is None:
            # Empty element: <name/> or <name></name>.
            return stario_new_element(
                _Element, open_tag, attrs, None, self.no_children_close
            )

        # Element with children: <name attr1="value1" attr2="value2">...</name>.
        return stario_new_element(
            _Element, open_tag, attrs, child_elements, self.closing_tag
        )


# ---------------------------------------------------------------------------
# Render

cdef object _unfilled_slot_name(object node):
    if type(node) is _BakeSlot:
        return _slot_name(node)
    for part in node.parts:
        if type(part) is _BakeSlot:
            return _slot_name(part)
    return "?"


cdef int _render_node(object node, list out, int depth) except -1:
    cdef object node_type = type(node)
    cdef object attrs
    cdef object children
    cdef object part
    cdef Py_ssize_t i

    if node_type is str:
        PyList_Append(out, c_escape_text(node))
        return 0

    if node_type is _Element:
        # Element walk must stay in sync with baked._flatten_node_into_segments.
        if depth >= _MAX_DEPTH:
            raise RecursionError("HTML tree is nested too deeply")
        PyList_Append(out, <object>PyTuple_GET_ITEM(node, 0))
        attrs = <object>PyTuple_GET_ITEM(node, 1)
        children = <object>PyTuple_GET_ITEM(node, 2)
        if attrs is not None:
            if type(attrs) is str:
                PyList_Append(out, attrs)
            else:
                for part in attrs:
                    if type(part) is str:
                        PyList_Append(out, part)
                    elif type(part) is _AttrSlot:
                        raise StarioError(
                            "Unfilled @baked attribute slot in render tree",
                            context={"attribute": part.key, "slot_name": part.name},
                            help_text="Elements with parameter-held attributes only exist inside @baked builders. Call the baked function to substitute slots, then render the returned fragment.",
                            example=(
                                "@baked\n"
                                "def link(href, label):\n"
                                "    return h.A({'href': href}, label)\n\n"
                                "render(link('/docs', 'Docs'))"
                            ),
                        )
                    else:
                        raise StarioError(
                            f"Invalid attribute part type: {type(part).__name__}",
                            context={"part_type": type(part).__name__},
                            help_text="Attribute parts must be pre-rendered strings.",
                        )
        if children is not None:
            if attrs is not None:
                PyList_Append(out, _GT)
            _render_list(<list>children, out, depth + 1)
        PyList_Append(out, <object>PyTuple_GET_ITEM(node, 3))
        return 0

    if node_type is _SafeString:
        PyList_Append(out, node.rendered)
        return 0

    if node_type is list:
        if depth >= _MAX_DEPTH:
            raise RecursionError("HTML tree is nested too deeply")
        _render_list(<list>node, out, depth + 1)
        return 0

    if node_type is int or node_type is float:
        PyList_Append(out, str(node))
        return 0

    if node is None:
        return 0

    if node_type is _BakeSlot or node_type is _RawSlot:
        raise StarioError(
            "Unfilled @baked slot in render tree",
            context={"slot_name": _unfilled_slot_name(node)},
            help_text="Call the baked function to substitute slots, then render the returned fragment.",
            example=(
                "@baked\n"
                "def layout(title, body):\n"
                "    return h.Div(h.Title(title), body)\n\n"
                "render(layout('Hi', h.P('x')))"
            ),
        )

    if node_type is Tag:
        raise StarioError(
            "Cannot render a Tag object directly",
            context={"tag": repr(node)},
            help_text="Call the tag first to create an element, e.g. use h.Div() instead of Div.",
            example='render(h.Div("Hello"))',
        )

    if node_type is bool:
        raise StarioError(
            "Boolean values are not valid HTML child content",
            context={"node_value": str(node)},
            help_text="Use a conditional to include or omit content, or convert the value to text explicitly with str(...).",
            example='h.P("enabled" if is_enabled else "disabled")',
        )

    if node_type is tuple:
        raise StarioError(
            "Cannot render a tuple as HTML content",
            context={"node_value": str(node)[:100]},
            help_text=(
                "A tuple is not markup. Unpack it into children (h.Li(*row)) "
                "or convert each item to text; only elements built by h.* "
                "render as tags."
            ),
            example="h.Ul(*[h.Li(str(cell)) for cell in row])",
        )

    raise StarioError(
        f"Cannot render element of type {type(node).__name__}",
        context={
            "node_type": type(node).__name__,
            "node_value": str(node)[:100],
        },
        help_text="Only str, int, float, SafeString, lists, and elements built by h.* can be rendered.",
        example="h.Div(str(my_object))",
    )


cdef int _render_list(list nodes, list out, int depth) except -1:
    cdef Py_ssize_t i
    for i in range(PyList_GET_SIZE(nodes)):
        _render_node(<object>PyList_GET_ITEM(nodes, i), out, depth)
    return 0


cdef object _render_any(object nodes):
    cdef list out = []
    cdef Py_ssize_t count
    if type(nodes) is list:
        _render_list(<list>nodes, out, 0)
    else:
        for node in nodes:
            _render_node(node, out, 0)
    count = PyList_GET_SIZE(out)
    if count == 0:
        return ""
    if count == 1:
        return <object>PyList_GET_ITEM(out, 0)
    return "".join(out)


def render_nodes_to_string(nodes, *, root_count=None):
    """Render an iterable of nodes with the same error contract as `render`."""
    _bind()
    try:
        return _render_any(nodes)
    except StarioError:
        raise
    except RecursionError as e:
        raise StarioError(
            "HTML tree is nested too deeply to render",
            context={"root_count": root_count if root_count is not None else "unknown"},
            help_text=(
                "The renderer walks elements recursively and hit Python's "
                "recursion limit (~1000 levels of nesting). Real markup never "
                "nests this deep; check for a cycle or a self-referencing node."
            ),
        ) from e
    except Exception as e:
        raise StarioError(
            f"Unexpected error while rendering HTML: {type(e).__name__}: {e}",
            context={
                "error_type": type(e).__name__,
                "error_message": str(e),
                "node_count": root_count if root_count is not None else "unknown",
            },
            help_text="Check that all HTML elements are valid types and properly structured.",
            example='render(h.Div({"class": "container"}, h.P("Hello")))',
        ) from e


def render(*nodes):
    """Walk HTML fragments depth-first and return one UTF-8 string.

    Accepts any number of root nodes (variadic). Plain `str` in child position
    is HTML text-escaped; attribute escaping happens inside `Tag` when the
    opening tag is built. `SafeString` is emitted unchanged (trusted).

    Fragments from `baked` are `SafeString` values. Pass them as one root, e.g.
    `render(layout(a, b))`. You can also nest `layout(...)` wherever an element
    child is accepted, same as `h.Div(...)`.
    """
    return render_nodes_to_string(nodes, root_count=len(nodes))
