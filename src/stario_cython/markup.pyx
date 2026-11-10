# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, freethreading_compatible=True
# cython: auto_pickle=False
"""Typed markup core for ``stario.markup``.

Four types live here:

- ``SafeString``: trusted markup, inserted verbatim.
- ``Attrs``: a trusted attribute fragment that starts with a space.
- ``Element``: one built element. Only ``Tag.__call__`` makes these: the type
  has no ``__new__`` for Python code, so every element in a tree holds
  validated, escaped attributes.
- ``Tag``: the element factory (``h.Div``, ``svg.Circle``, ...).

``Tag.__call__`` checks every argument once, at the call site. It writes the
attributes into one string and stores the children as a tuple: the call's own
argument tuple (skipping leading ``Attrs``), or a slice of it when a mapping
came first. No list is
built, and an element never keeps an attribute mapping alive.

``render`` and ``render_bytes`` walk a tree depth-first into one UTF-8 buffer
(on the stack first, then on the heap) and escape text while they copy.

The Python modules ``stario.markup.types``, ``.tag``, ``.render``, ``.escape``
and ``.attributes`` re-export from here. The ``@baked`` slot types
(``BakeSlot``, ``AttrSlot``, ``RawSlot``) and ``validate_attribute_key`` stay
in Python and are bound on first use, so the two packages can import each
other.
"""

import re
from html import unescape as _html_unescape

cimport cython
from cpython.list cimport PyList_Append, PyList_GET_ITEM, PyList_GET_SIZE
from cpython.mem cimport PyMem_Free, PyMem_Malloc, PyMem_Realloc
from cpython.object cimport PyObject
from cpython.ref cimport Py_DECREF, Py_INCREF, Py_XDECREF
from cpython.tuple cimport PyTuple_GET_ITEM, PyTuple_GET_SIZE
from libc.string cimport memcpy

from stario.exceptions import StarioError

cdef extern from "Python.h":
    int PyUnicode_KIND(object o)
    void* PyUnicode_DATA(object o)
    Py_ssize_t PyUnicode_GET_LENGTH(object o)
    unsigned int PyUnicode_MAX_CHAR_VALUE(object o)
    bint PyUnicode_IS_ASCII(object o)
    object PyUnicode_New(Py_ssize_t size, unsigned int maxchar)
    int PyUnicode_1BYTE_KIND
    Py_UCS4 PyUnicode_READ(int kind, const void* data, Py_ssize_t index)
    Py_UCS4 PyUnicode_READ_CHAR(object o, Py_ssize_t index)
    void PyUnicode_WRITE(int kind, void* data, Py_ssize_t index, Py_UCS4 value)
    Py_ssize_t PyUnicode_CopyCharacters(object to, Py_ssize_t to_start, object frm, Py_ssize_t from_start, Py_ssize_t how_many) except -1
    const char* PyUnicode_AsUTF8AndSize(object o, Py_ssize_t* size) except NULL
    object PyUnicode_DecodeUTF8(const char* s, Py_ssize_t size, const char* errors)
    object PyBytes_FromStringAndSize(const char* v, Py_ssize_t len)
    object PyUnicode_FromObject(object o)
    Py_ssize_t PyDict_Size(object d)

cdef extern from *:
    """
    #include <Python.h>

    /* Take a ready type's constructor away from Python code: the effect of
       Py_TPFLAGS_DISALLOW_INSTANTIATION, which only PyType_Ready reads.
       This module calls the type's tp_new function directly, so it can
       still build instances. Without a tp_new, object.__new__(T) refuses
       too ("is not safe"). */
    static int stario_disallow_instantiation(PyObject* t) {
        PyTypeObject* tp = (PyTypeObject*)t;
        PyObject* dict = PyType_GetDict(tp);
        if (dict == NULL) {
            return -1;
        }
        if (PyDict_GetItemString(dict, "__new__") != NULL
                && PyDict_DelItemString(dict, "__new__") < 0) {
            Py_DECREF(dict);
            return -1;
        }
        Py_DECREF(dict);
        tp->tp_new = NULL;
        /* Cython also gives the type a vectorcall constructor that calls
           its tp_new function directly: drop it, so a call goes through
           type_call and finds no tp_new. */
        tp->tp_vectorcall = NULL;
        PyType_Modified(tp);
        return 0;
    }

    /* d[key] as a new reference: 1 found, 0 absent, -1 error. A plain
       PyDict_GetItem returns a borrowed pointer, which another thread can
       free before the caller takes its own reference (free threading). */
    static int stario_dict_get_ref(PyObject* d, PyObject* key, PyObject** out) {
    #if PY_VERSION_HEX >= 0x030d0000
        return PyDict_GetItemRef(d, key, out);
    #else
        *out = PyDict_GetItemWithError(d, key);
        if (*out != NULL) {
            Py_INCREF(*out);
            return 1;
        }
        return PyErr_Occurred() ? -1 : 0;
    #endif
    }

    /* Bytes that text escaping rewrites: & < > */
    static const unsigned char stario_text_escape[256] = {
        ['&'] = 1, ['<'] = 1, ['>'] = 1,
    };

    /* type(o) as a borrowed pointer: Cython's type(o) takes a reference,
       a refcount write on the type for every argument or node. */
    #define stario_type(o) ((PyObject*)Py_TYPE(o))

    /* Free-threaded builds: make an object immortal, so that Py_INCREF and
       Py_DECREF on it from any thread only read its refcount instead of
       writing one cache line that every core then fights over. Every
       element holds a reference to its Tag, so without this each thread
       building `h.Td(...)` writes the shared `h.Td` twice per element.

       CPython's rules, as PyUnstable_SetImmortal (3.15+) applies them:
       only a uniquely referenced object (no other thread can be changing
       its refcount), never a str (strings become immortal by interning;
       see stario_shared_str), and a GC object is untracked first (a
       tracked one turns mortal again when its thread exits and a
       collection merges its refcount). Before 3.15, Py_SET_REFCNT past
       UINT32_MAX from the owning thread sets the immortal refcount; a
       32-bit Py_ssize_t cannot hold that value, so there it does nothing.
       Returns 1 when the object is immortal afterwards. A no-op with the
       GIL. An immortal object is never freed: only objects that live as
       long as the process anyway go through here (a Tag and its empty
       element, the caches and their entries, which are never removed). */
    static int stario_immortalize(PyObject* op) {
    #if !defined(Py_GIL_DISABLED)
        (void)op;
        return 0;
    #else
        if (PyUnicode_Check(op)) {
            return 0;
        }
    #if PY_VERSION_HEX >= 0x030F0000
        return PyUnstable_SetImmortal(op);
    #else
        if (PY_SSIZE_T_MAX <= _Py_IMMORTAL_REFCNT_LOCAL) {
            return 0;
        }
        if (_Py_IsImmortal(op)) {
            return 1;
        }
    #if PY_VERSION_HEX >= 0x030E0000
        if (!PyUnstable_Object_IsUniquelyReferenced(op)) {
            return 0;
        }
    #else
        if (!_Py_IsOwnedByCurrentThread(op) || Py_REFCNT(op) != 1) {
            return 0;
        }
    #endif
        if (PyObject_IS_GC(op) && PyObject_GC_IsTracked(op)) {
            PyObject_GC_UnTrack(op);
        }
        Py_SET_REFCNT(op, (Py_ssize_t)_Py_IMMORTAL_REFCNT_LOCAL + 1);
        return 1;
    #endif
    #endif
    }

    /* A str that every thread reads (new reference; exact str only).
       Free-threaded builds: the interned copy, and interned strings are
       immortal there. The caller's own object is never interned in place:
       another thread may hold it, so a private copy is interned instead
       (or the canonical string, when an equal one is interned already).
       The object itself when it is immortal already, and with the GIL. */
    static PyObject* stario_shared_str(PyObject* s) {
    #ifdef Py_GIL_DISABLED
        PyObject* copy;
        if (_Py_IsImmortal(s)) {
            return Py_NewRef(s);
        }
        copy = PyUnicode_FromKindAndData(
            PyUnicode_KIND(s), PyUnicode_DATA(s), PyUnicode_GET_LENGTH(s));
        if (copy != NULL) {
            PyUnicode_InternInPlace(&copy);
        }
        return copy;
    #else
        return Py_NewRef(s);
    #endif
    }

    /* A cache key tuple (new reference): on free-threaded builds an
       immortal private copy, because the dict increfs a stored key while
       it compares a lookup key against it. The caller's tuple may be
       shared (classes(*t) passes `t` itself). The tuple itself when it is
       immortal already, and with the GIL. */
    static PyObject* stario_shared_tuple(PyObject* t) {
    #ifdef Py_GIL_DISABLED
        PyObject* copy;
        Py_ssize_t i, n;
        if (_Py_IsImmortal(t)) {
            return Py_NewRef(t);
        }
        n = PyTuple_GET_SIZE(t);
        copy = PyTuple_New(n);
        if (copy == NULL) {
            return NULL;
        }
        for (i = 0; i < n; i++) {
            PyTuple_SET_ITEM(copy, i, Py_NewRef(PyTuple_GET_ITEM(t, i)));
        }
        stario_immortalize(copy);
        return copy;
    #else
        return Py_NewRef(t);
    #endif
    }

    /* A new dict for a cache that every thread reads: immortal on
       free-threaded builds (Cython increfs a module-level object each time
       it reads one). Made immortal here, while it is uniquely referenced. */
    static PyObject* stario_shared_dict(void) {
        PyObject* d = PyDict_New();
        if (d != NULL) {
            stario_immortalize(d);
        }
        return d;
    }
    """
    int stario_disallow_instantiation(object t) except -1
    int stario_dict_get_ref(object d, object key, PyObject** out) except -1
    const unsigned char stario_text_escape[256]
    PyObject* stario_type(PyObject* o)
    int stario_immortalize(object op)
    object stario_shared_str(object s)
    object stario_shared_tuple(object t)
    object stario_shared_dict()


# ---------------------------------------------------------------------------
# Python symbols, bound on first use (see the module docstring).

cdef object _BakeSlot = None
cdef object _AttrSlot = None
cdef object _RawSlot = None
cdef object _slot_name = None
cdef object _validate_attribute_key = None
cdef object _Mapping = None
cdef bint _bound = False

# Validated attribute names: str -> str, so the hot path is one dict lookup.
# Bounded by refusing new entries past the cap, never by clearing: a clear
# in place could free a key another thread is still reading (free-threaded
# builds), and the dict object itself is never replaced for the same reason.
# Free-threaded builds store immortal entries (stario_shared_str), and the
# cache dicts are immortal too: Cython increfs a module-level object each
# time it reads one, and every thread reads these on every call.
cdef dict _VALID_KEYS = <dict>stario_shared_dict()
cdef int _VALID_KEYS_MAX = 4096

# Rendered ``class`` fragments keyed by token tuples of str / None / False
# (Tailwind lists repeat). Same bound rule. ``stario.markup.attributes`` exposes the dict.
cdef dict _classes_cache = <dict>stario_shared_dict()
classes_cache = _classes_cache
CLASSES_CACHE_MAX = 1024
cdef Py_ssize_t _CLASSES_CACHE_MAX = 1024

cdef object _TAG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
cdef frozenset _EMPTY_MODES = frozenset({"normal", "void", "self_closing_when_empty"})

cdef enum:
    _MAX_DEPTH = 1000
    _STACK_BUF = 4096
    _MAX_PAIRS = 24
    _TEXT_CHUNK = 8192

cdef void _bind():
    """Bind the Python symbols once.

    The first call happens while ``stario.markup.html`` builds its tags at
    import, under the import lock, so every later caller (any thread) sees
    the symbols already set. The imports are idempotent and ``_bound`` is
    written last, so a repeated call can only rewrite the same values.
    """
    global _BakeSlot, _AttrSlot, _RawSlot, _slot_name
    global _validate_attribute_key, _Mapping, _bound
    if _bound:
        return
    from collections.abc import Mapping
    from stario.markup.escape import validate_attribute_key
    from stario.markup.slots import AttrSlot, BakeSlot, RawSlot, slot_name
    _BakeSlot = BakeSlot
    _AttrSlot = AttrSlot
    _RawSlot = RawSlot
    _slot_name = slot_name
    _validate_attribute_key = validate_attribute_key
    _Mapping = Mapping
    _bound = True


# ---------------------------------------------------------------------------
# Escaping

cdef enum:
    ESC_TEXT = 0      # & < >
    ESC_DQUOTE = 1    # & < > "
    ESC_SQUOTE = 2    # & < > '


cdef object _escape_1byte(object s, int mode):
    """Escape a Latin-1 / ASCII ``str`` in one C pass; same object if clean."""
    cdef const unsigned char* src = <const unsigned char*>PyUnicode_DATA(s)
    cdef Py_ssize_t n = PyUnicode_GET_LENGTH(s)
    cdef Py_ssize_t i
    cdef Py_ssize_t extra = 0
    cdef Py_ssize_t w = 0
    cdef unsigned char c
    cdef unsigned char quote = 34 if mode == ESC_DQUOTE else (39 if mode == ESC_SQUOTE else 0)
    cdef unsigned char* dst
    cdef object out
    for i in range(n):
        c = src[i]
        if c == 38:
            extra += 4
        elif c == 60 or c == 62:
            extra += 3
        elif c == quote and quote != 0:
            extra += 5 if quote == 34 else 4
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
        elif c == 34 and quote == 34:
            dst[w] = 38; dst[w + 1] = 113; dst[w + 2] = 117; dst[w + 3] = 111
            dst[w + 4] = 116; dst[w + 5] = 59
            w += 6
        elif c == 39 and quote == 39:
            dst[w] = 38; dst[w + 1] = 35; dst[w + 2] = 51; dst[w + 3] = 57; dst[w + 4] = 59
            w += 5
        else:
            dst[w] = c
            w += 1
    return out


cdef object _escape_wide(object s, int mode):
    """Escape a 2-byte or 4-byte kind ``str`` in one C pass; same object if clean."""
    cdef int kind = PyUnicode_KIND(s)
    cdef const void* src = PyUnicode_DATA(s)
    cdef Py_ssize_t n = PyUnicode_GET_LENGTH(s)
    cdef Py_ssize_t i
    cdef Py_ssize_t extra = 0
    cdef Py_ssize_t w = 0
    cdef Py_UCS4 c
    cdef Py_UCS4 quote = 34 if mode == ESC_DQUOTE else (39 if mode == ESC_SQUOTE else 0)
    cdef void* dst
    cdef object out
    for i in range(n):
        c = PyUnicode_READ(kind, src, i)
        if c == 38:
            extra += 4
        elif c == 60 or c == 62:
            extra += 3
        elif c == quote and quote != 0:
            extra += 5 if quote == 34 else 4
    if extra == 0:
        return s
    out = PyUnicode_New(n + extra, PyUnicode_MAX_CHAR_VALUE(s))
    dst = PyUnicode_DATA(out)
    for i in range(n):
        c = PyUnicode_READ(kind, src, i)
        if c == 38:
            PyUnicode_WRITE(kind, dst, w, 38); PyUnicode_WRITE(kind, dst, w + 1, 97)
            PyUnicode_WRITE(kind, dst, w + 2, 109); PyUnicode_WRITE(kind, dst, w + 3, 112)
            PyUnicode_WRITE(kind, dst, w + 4, 59)
            w += 5
        elif c == 60:
            PyUnicode_WRITE(kind, dst, w, 38); PyUnicode_WRITE(kind, dst, w + 1, 108)
            PyUnicode_WRITE(kind, dst, w + 2, 116); PyUnicode_WRITE(kind, dst, w + 3, 59)
            w += 4
        elif c == 62:
            PyUnicode_WRITE(kind, dst, w, 38); PyUnicode_WRITE(kind, dst, w + 1, 103)
            PyUnicode_WRITE(kind, dst, w + 2, 116); PyUnicode_WRITE(kind, dst, w + 3, 59)
            w += 4
        elif c == 34 and quote == 34:
            PyUnicode_WRITE(kind, dst, w, 38); PyUnicode_WRITE(kind, dst, w + 1, 113)
            PyUnicode_WRITE(kind, dst, w + 2, 117); PyUnicode_WRITE(kind, dst, w + 3, 111)
            PyUnicode_WRITE(kind, dst, w + 4, 116); PyUnicode_WRITE(kind, dst, w + 5, 59)
            w += 6
        elif c == 39 and quote == 39:
            PyUnicode_WRITE(kind, dst, w, 38); PyUnicode_WRITE(kind, dst, w + 1, 35)
            PyUnicode_WRITE(kind, dst, w + 2, 51); PyUnicode_WRITE(kind, dst, w + 3, 57)
            PyUnicode_WRITE(kind, dst, w + 4, 59)
            w += 5
        else:
            PyUnicode_WRITE(kind, dst, w, c)
            w += 1
    return out


cdef inline object c_escape_text(object s):
    if PyUnicode_KIND(s) == PyUnicode_1BYTE_KIND:
        return _escape_1byte(s, ESC_TEXT)
    return _escape_wide(s, ESC_TEXT)


cdef inline object c_escape_attr(object s):
    if PyUnicode_KIND(s) == PyUnicode_1BYTE_KIND:
        return _escape_1byte(s, ESC_DQUOTE)
    return _escape_wide(s, ESC_DQUOTE)


cdef inline object c_escape_sq_attr(object s):
    if PyUnicode_KIND(s) == PyUnicode_1BYTE_KIND:
        return _escape_1byte(s, ESC_SQUOTE)
    return _escape_wide(s, ESC_SQUOTE)


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


def escape_sq_attribute_value(str s not None):
    """Escape for single-quoted attribute values (`'`, `&`, `<`, `>`).

    Use when the value is JSON or other text that already contains double
    quotes, so the attribute can be wrapped in single quotes on the wire.
    """
    return c_escape_sq_attr(s)


def check_raw_text(str text not None, str tag not None):
    """Raise if ``text`` could end a raw-text element named ``tag`` early."""
    _check_raw_text(text, tag)


cdef int _check_raw_text(object text, object tag) except -1:
    cdef object lowered = text.lower()
    cdef object name = tag.lower()  # Tag("SCRIPT", raw_text=True) ends at "</script" too
    cdef bint script = name == "script"
    if f"</{name}" in lowered or (script and "<!--" in lowered):
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
# Value types

cdef inline str _as_str(object value, str owner):
    """``value`` as an exact ``str``; a ``str`` subclass (``markupsafe.Markup``) is copied."""
    if type(value) is str:
        return value
    if isinstance(value, str):
        return PyUnicode_FromObject(value)
    raise TypeError(f"{owner} takes a str, not {type(value).__name__}")


cdef inline bint _is_html_space(Py_UCS4 c):
    """The ASCII whitespace that HTML allows between attributes."""
    return c == 32 or c == 9 or c == 10 or c == 12 or c == 13


# The first docstring lines ("Name(args)", "--" at column 0, a blank line)
# are CPython's signature block: they give inspect.signature(SafeString),
# which a cdef class with only __cinit__ has no other way to show.
@cython.final
@cython.freelist(256)
cdef class SafeString:
    """SafeString(rendered)
--

    Markup inserted verbatim into rendered HTML (no escaping).

    Immutable. Only a ``str`` is accepted (a ``str`` subclass is copied to a
    plain ``str``): a number or ``None`` is a ``TypeError`` here, at the call
    that made it.
    """

    cdef readonly str rendered
    __match_args__ = ("rendered",)

    def __cinit__(self, rendered):
        self.rendered = _as_str(rendered, "SafeString")

    def __repr__(self):
        return f"SafeString({self.rendered!r})"

    def __eq__(self, other):
        if type(other) is SafeString:
            return self.rendered == (<SafeString>other).rendered
        return NotImplemented

    def __hash__(self):
        return hash(self.rendered)

    def __reduce__(self):
        return (SafeString, (self.rendered,))


@cython.final
@cython.freelist(256)
cdef class Attrs:
    """Attrs(rendered)
--

    Trusted, pre-rendered opening-tag attributes, including leading spaces.

    Immutable. The fragment must be empty or start with whitespace, because
    it goes straight after the tag name: ``Attrs('id="x"')`` would render
    ``<divid="x">``.
    """

    cdef readonly str rendered
    __match_args__ = ("rendered",)

    def __cinit__(self, rendered):
        cdef str text = _as_str(rendered, "Attrs")
        if PyUnicode_GET_LENGTH(text) and not _is_html_space(PyUnicode_READ_CHAR(text, 0)):
            raise StarioError(
                "Attrs fragments must start with a space",
                context={"rendered": text[:100]},
                help_text=(
                    "An Attrs fragment goes straight after the tag name, so it "
                    "must be empty or begin with whitespace."
                ),
                example="Attrs(' data-x=\"1\"')",
            )
        self.rendered = text

    def __repr__(self):
        return f"Attrs({self.rendered!r})"

    def __eq__(self, other):
        if type(other) is Attrs:
            return self.rendered == (<Attrs>other).rendered
        return NotImplemented

    def __hash__(self):
        return hash(self.rendered)

    def __reduce__(self):
        return (Attrs, (self.rendered,))


cdef inline SafeString _safe(str s):
    return SafeString.__new__(SafeString, s)


def shared_str(text):
    """``text`` for code that every thread reads (``@baked`` static segments).

    On free-threaded builds an interned copy, which is immortal there, so
    loading it writes no refcount; it is never freed. With the GIL, ``text``.
    """
    return stario_shared_str(_as_str(text, "shared_str"))


def shared_safe_string(rendered):
    """A ``SafeString`` that every call returns (``@baked`` without parameters).

    On free-threaded builds it is immortal and holds an interned copy of
    ``rendered``, so threads returning it write no refcount; it is never
    freed. With the GIL it is a plain ``SafeString``.
    """
    cdef SafeString s = SafeString.__new__(
        SafeString, stario_shared_str(_as_str(rendered, "SafeString"))
    )
    stario_immortalize(s)
    return s


# ---------------------------------------------------------------------------
# Errors shared by Tag.__call__ and render

cdef object _attrs_before_children_error(object tag_repr):
    return StarioError(
        "HTML attributes must be passed before children",
        context={"tag": tag_repr},
        help_text=(
            "Pass attribute mappings and Attrs fragments first, then child nodes."
        ),
        example='h.Div(classes("card"), h.Span("ok"))',
    )


cdef object _unfilled_slot_name(object node):
    if type(node) is _BakeSlot:
        return _slot_name(node)
    for part in node.parts:
        if type(part) is _BakeSlot:
            return _slot_name(part)
    return "?"


cdef object _bad_node_error(object node):
    """The error for a value that is not markup (same text at build and render)."""
    cdef object node_type = type(node)
    if node_type is _BakeSlot or node_type is _RawSlot:
        return StarioError(
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
    if isinstance(node, Tag):
        return StarioError(
            "Cannot render a Tag object directly",
            context={"tag": repr(node)},
            help_text="Call the tag first to create an element, e.g. use h.Div() instead of Div.",
            example='render(h.Div("Hello"))',
        )
    if node_type is bool:
        return StarioError(
            "Boolean values are not valid HTML child content",
            context={"node_value": str(node)},
            help_text="Use a conditional to include or omit content, or convert the value to text explicitly with str(...).",
            example='h.P("enabled" if is_enabled else "disabled")',
        )
    if node_type is tuple:
        return StarioError(
            "Cannot render a tuple as HTML content",
            context={"node_value": str(node)[:100]},
            help_text=(
                "A tuple is not markup. Unpack it into children (h.Li(*row)) "
                "or convert each item to text; only elements built by h.* "
                "render as tags."
            ),
            example="h.Ul(*[h.Li(str(cell)) for cell in row])",
        )
    return StarioError(
        f"Cannot render element of type {node_type.__name__}",
        context={
            "node_type": node_type.__name__,
            "node_value": str(node)[:100],
        },
        help_text="Only str, int, float, SafeString, lists, and elements built by h.* can be rendered.",
        example="h.Div(str(my_object))",
    )


cdef object _bad_child_error(object node, object parent):
    """``_bad_node_error`` raised by a tag call.

    The context names the tag that got the child (``parent``) and the
    child's type. ``error.stario_child_kind`` lets ``@baked`` explain the
    error in its own terms; it is an attribute, not context, so users never
    see it.
    """
    cdef object error = _bad_node_error(node)
    cdef object node_type = type(node)
    if node_type is bool:
        kind = "bool"
    elif isinstance(node, Tag):
        kind = "tag"
    elif node_type is tuple:
        kind = "tuple"
    else:
        kind = "other"
    error.context["parent"] = parent
    error.context["node_type"] = node_type.__name__
    error.stario_child_kind = kind
    return error


cdef object _unfilled_attr_slot_error(object part):
    return StarioError(
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


# ---------------------------------------------------------------------------
# Attribute names and values

def _valid_key_cache_size():
    """Entries in the attribute-name cache (tests check it stops at the cap)."""
    return len(_VALID_KEYS)


cdef inline object _valid_key(object raw_key):
    """The validated attribute name: one dict hit, else the Python validator."""
    cdef PyObject* hit
    cdef object key
    if type(raw_key) is str and stario_dict_get_ref(_VALID_KEYS, raw_key, &hit):
        key = <object>hit
        Py_DECREF(key)  # ``key`` holds its own reference now
        return key
    key = _validate_attribute_key(raw_key)
    if len(_VALID_KEYS) < _VALID_KEYS_MAX:
        # setdefault: the first insert wins and is never replaced, so no
        # reader can see an entry freed under it.
        key = stario_shared_str(key)
        key = _VALID_KEYS.setdefault(key, key)
    return key


cdef object _bad_attr_value_error(object key, object value):
    return StarioError(
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


cdef object _attr_fragment(object key, object value):
    """`` key="value"`` for one scalar value, or None to omit (matches ``wire``)."""
    cdef object value_type = type(value)
    if value_type is str:
        return f' {key}="{c_escape_attr(value)}"'
    if value_type is SafeString:
        return f' {key}="{(<SafeString>value).rendered}"'
    if value is True:
        return f" {key}"
    if value is None or value is False:
        return None
    if value_type is int or value_type is float:
        return f' {key}="{value}"'
    raise _bad_attr_value_error(key, value)


def splice_attribute(key, value):
    """One ``@baked`` whole-value attribute slot (``""`` to omit)."""
    fragment = _attr_fragment(key, value)
    return "" if fragment is None else fragment


cdef struct Pair:
    PyObject* key      # NULL: ``value`` is a pre-rendered Attrs fragment
    PyObject* value    # NULL with a key: bare boolean attribute


cdef inline void _copy_into(object out, Py_ssize_t* w, object s, bint one_byte):
    cdef Py_ssize_t n = PyUnicode_GET_LENGTH(s)
    if one_byte:
        memcpy(<char*>PyUnicode_DATA(out) + w[0], PyUnicode_DATA(s), n)
    else:
        PyUnicode_CopyCharacters(out, w[0], s, 0, n)
    w[0] += n


cdef inline void _put_char(object out, Py_ssize_t* w, Py_UCS4 c, bint one_byte):
    if one_byte:
        (<char*>PyUnicode_DATA(out))[w[0]] = <char>c
    else:
        PyUnicode_WRITE(PyUnicode_KIND(out), PyUnicode_DATA(out), w[0], c)
    w[0] += 1


cdef enum:
    _PAIR_OK = 0
    _PAIR_FULL = 1     # more than _MAX_PAIRS: use the list path
    _PAIR_SLOT = 2     # a @baked parameter: use the list path


cdef int _push(Pair* pairs, Py_ssize_t* count, Py_ssize_t* total, unsigned int* maxchar,
               object key, object text):
    """Own ``key`` and ``text`` in the next pair; returns ``_PAIR_FULL`` when out of room."""
    cdef unsigned int m
    if count[0] >= _MAX_PAIRS:
        return _PAIR_FULL
    if key is not None:
        Py_INCREF(key)
        pairs[count[0]].key = <PyObject*>key
        total[0] += 1 + PyUnicode_GET_LENGTH(key)
        m = PyUnicode_MAX_CHAR_VALUE(key)
        if m > maxchar[0]:
            maxchar[0] = m
    else:
        pairs[count[0]].key = NULL
    if text is not None:
        Py_INCREF(text)
        pairs[count[0]].value = <PyObject*>text
        total[0] += PyUnicode_GET_LENGTH(text) + (3 if key is not None else 0)
        m = PyUnicode_MAX_CHAR_VALUE(text)
        if m > maxchar[0]:
            maxchar[0] = m
    else:
        pairs[count[0]].value = NULL
    count[0] += 1
    return _PAIR_OK


cdef int _push_value(Pair* pairs, Py_ssize_t* count, Py_ssize_t* total, unsigned int* maxchar,
                     object raw_key, object value) except -1:
    cdef object key
    cdef object value_type = type(value)
    if type(raw_key) is _BakeSlot or value_type is _BakeSlot:
        return _PAIR_SLOT
    key = _valid_key(raw_key)
    if value_type is str:
        return _push(pairs, count, total, maxchar, key, c_escape_attr(value))
    if value_type is SafeString:
        return _push(pairs, count, total, maxchar, key, (<SafeString>value).rendered)
    if value is True:
        return _push(pairs, count, total, maxchar, key, None)
    if value is None or value is False:
        return _PAIR_OK
    if value_type is int or value_type is float:
        return _push(pairs, count, total, maxchar, key, str(value))
    raise _bad_attr_value_error(key, value)


cdef object _pair_string(object key, object text):
    """`` key="text"`` in one allocation."""
    cdef Py_ssize_t nk = PyUnicode_GET_LENGTH(key)
    cdef Py_ssize_t nt = PyUnicode_GET_LENGTH(text)
    cdef unsigned int maxchar = PyUnicode_MAX_CHAR_VALUE(key)
    cdef unsigned int mt = PyUnicode_MAX_CHAR_VALUE(text)
    cdef object out
    cdef char* d
    if mt > maxchar:
        maxchar = mt
    if maxchar < 128:
        maxchar = 127
    out = PyUnicode_New(nk + nt + 4, maxchar)
    if PyUnicode_KIND(out) == PyUnicode_1BYTE_KIND:
        d = <char*>PyUnicode_DATA(out)
        d[0] = 32
        memcpy(d + 1, PyUnicode_DATA(key), nk)
        d[nk + 1] = 61
        d[nk + 2] = 34
        memcpy(d + nk + 3, PyUnicode_DATA(text), nt)
        d[nk + nt + 3] = 34
        return out
    return f' {key}="{text}"'


cdef object _one_pair(object raw_key, object value, bint* fallback):
    """The attribute string for a mapping with exactly one entry."""
    cdef object key
    cdef object value_type = type(value)
    if type(raw_key) is _BakeSlot or value_type is _BakeSlot:
        fallback[0] = True
        return None
    key = _valid_key(raw_key)
    if value_type is str:
        return _pair_string(key, c_escape_attr(value))
    if value_type is SafeString:
        return _pair_string(key, (<SafeString>value).rendered)
    if value is True:
        return " " + key
    if value is None or value is False:
        return None
    if value_type is int or value_type is float:
        return _pair_string(key, str(value))
    raise _bad_attr_value_error(key, value)


cdef object _attrs_one_string(tuple args, Py_ssize_t end, bint* fallback):
    """`` k="v" k2`` for the attribute arguments in ``args[:end]``, one allocation.

    Sets ``fallback`` (and returns None) when the list path must run instead:
    more than ``_MAX_PAIRS`` pairs, or a ``@baked`` parameter in a value.
    """
    cdef Pair pairs[_MAX_PAIRS]
    cdef Py_ssize_t count = 0
    cdef Py_ssize_t total = 0
    cdef unsigned int maxchar = 127
    cdef Py_ssize_t i, w
    cdef object arg, arg_type, out
    cdef int status = _PAIR_OK
    cdef bint one_byte
    # Mappings are walked with ``.items()``: Cython then holds a strong
    # reference to each key and value (the validator may run Python code,
    # and another thread may change the dict meanwhile) and takes the dict's
    # lock on each step under free threading. A raw ``PyDict_Next`` loop
    # hands out borrowed pointers that can be freed under us.
    if end == 1:
        # One mapping with one entry ({"href": url}) is the common case.
        arg = <object>PyTuple_GET_ITEM(args, 0)
        if type(arg) is dict and PyDict_Size(arg) == 1:
            for raw_key, raw_value in (<dict>arg).items():
                return _one_pair(raw_key, raw_value, fallback)
    try:
        for i in range(end):
            arg = <object>PyTuple_GET_ITEM(args, i)
            arg_type = type(arg)
            if arg_type is dict:
                for raw_key, raw_value in (<dict>arg).items():
                    status = _push_value(pairs, &count, &total, &maxchar, raw_key, raw_value)
                    if status != _PAIR_OK:
                        break
            elif arg_type is Attrs:
                if PyUnicode_GET_LENGTH((<Attrs>arg).rendered):
                    status = _push(pairs, &count, &total, &maxchar, None, (<Attrs>arg).rendered)
            elif arg is not None:
                for raw_key, raw_value in arg.items():
                    status = _push_value(pairs, &count, &total, &maxchar, raw_key, raw_value)
                    if status != _PAIR_OK:
                        break
            if status != _PAIR_OK:
                fallback[0] = True
                return None
        if count == 0:
            return None
        if count == 1 and pairs[0].key == NULL:
            return <object>pairs[0].value
        out = PyUnicode_New(total, maxchar)
        one_byte = PyUnicode_KIND(out) == PyUnicode_1BYTE_KIND
        w = 0
        for i in range(count):
            if pairs[i].key == NULL:
                _copy_into(out, &w, <object>pairs[i].value, one_byte)
                continue
            _put_char(out, &w, 32, one_byte)
            _copy_into(out, &w, <object>pairs[i].key, one_byte)
            if pairs[i].value != NULL:
                _put_char(out, &w, 61, one_byte)
                _put_char(out, &w, 34, one_byte)
                _copy_into(out, &w, <object>pairs[i].value, one_byte)
                _put_char(out, &w, 34, one_byte)
        return out
    finally:
        for i in range(count):
            Py_XDECREF(pairs[i].key)
            Py_XDECREF(pairs[i].value)


cdef int _add_attr_part(Tag tag, object raw_key, object raw_value, list parts, bint* has_slots) except -1:
    cdef object key
    cdef object fragment
    if type(raw_key) is _BakeSlot:
        raise StarioError(
            "@baked: parameters cannot be used as attribute names",
            context={"tag": tag._repr()},
            help_text=(
                "Parameters may hold attribute values or children, "
                "never attribute names."
            ),
        )
    key = _valid_key(raw_key)
    if type(raw_value) is _BakeSlot:
        PyList_Append(parts, _AttrSlot(key, _slot_name(raw_value)))
        has_slots[0] = True
        return 0
    fragment = _attr_fragment(key, raw_value)
    if fragment is not None:
        PyList_Append(parts, fragment)
    return 0


cdef object _attrs_parts(Tag tag, tuple args, Py_ssize_t end):
    """The list path: a joined str, or a list with ``AttrSlot`` entries at bake time."""
    cdef list parts = []
    cdef bint has_slots = False
    cdef Py_ssize_t i
    cdef object arg
    for i in range(end):
        arg = <object>PyTuple_GET_ITEM(args, i)
        if type(arg) is dict:
            for raw_key, raw_value in (<dict>arg).items():
                _add_attr_part(tag, raw_key, raw_value, parts, &has_slots)
        elif type(arg) is Attrs:
            if PyUnicode_GET_LENGTH((<Attrs>arg).rendered):
                PyList_Append(parts, (<Attrs>arg).rendered)
        elif arg is not None:
            for raw_key, raw_value in arg.items():
                _add_attr_part(tag, raw_key, raw_value, parts, &has_slots)
    if has_slots:
        return parts
    if not parts:
        return None
    return "".join(parts)


cdef inline object _build_attrs(Tag tag, tuple args, Py_ssize_t end):
    cdef bint fallback = False
    cdef object attrs = _attrs_one_string(args, end, &fallback)
    if fallback:
        return _attrs_parts(tag, args, end)
    return attrs


# ---------------------------------------------------------------------------
# Element

cdef class Tag


@cython.final
@cython.freelist(1024)
cdef class Element:
    """One built element. Immutable; only a ``Tag`` call makes one.

    - ``tag``: the ``Tag`` that built it (``el.tag is h.Div``)
    - ``name``: the element name (``"div"``)
    - ``attrs``: the rendered attributes (`` id="x" class="a"``) or None
    - ``children``: a tuple of child nodes (empty when there are none)

    ``el["id"]`` reads one attribute back (``True`` for a bare boolean);
    ``el.with_attrs({...})`` returns a copy with attributes set.
    An element is not a sequence: it does not iterate or index by number.
    """

    cdef readonly Tag tag
    cdef object _attrs        # str, None, or a list of str | AttrSlot at bake time
    cdef tuple _children      # None when empty; the children are _children[_first:]
    cdef Py_ssize_t _first    # > 0 only when _children is the call's own args

    @property
    def name(self):
        return self.tag.name

    @property
    def attrs(self):
        if self._attrs is None or type(self._attrs) is str:
            return self._attrs
        return tuple(self._attrs)

    @property
    def children(self):
        if self._children is None:
            return ()
        if self._first:
            return self._children[self._first:]
        return self._children

    def __getitem__(self, key):
        if type(key) is not str:
            raise TypeError(
                f"Element attributes are read by name, not {type(key).__name__}; "
                "use .children for child nodes"
            )
        cdef object value = _attr_value(self._rendered_attrs(), key)
        if value is None:
            raise KeyError(key)
        return value

    def get(self, str key not None, default=None):
        """One attribute's value (``True`` when bare), else ``default``.

        The value is decoded as a browser reads it (``&amp;`` gives ``&``).
        A name that appears twice gives its first value, as in a browser.
        """
        cdef object value = _attr_value(self._rendered_attrs(), key)
        return default if value is None else value

    def __contains__(self, key):
        return type(key) is str and _attr_value(self._rendered_attrs(), key) is not None

    def __iter__(self):
        raise TypeError("Element is not iterable; use .children for child nodes")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def with_attrs(self, *attributes):
        """A copy with these attributes set. A name already present is replaced.

        Accepts the same mappings and ``Attrs`` fragments as a tag call; a
        ``None`` or ``False`` value removes the attribute. A replaced name
        keeps its place, a new name goes last, and every other attribute is
        copied byte for byte. Costs a parse (about 1 us): when the value is
        known at build time, pass it to the tag call instead.
        """
        cdef list names = []
        cdef list fragments = []
        cdef Element e
        cdef object attrs
        for name, start, stop, _ in _parse_attrs(self._rendered_attrs()):
            PyList_Append(names, name)
            PyList_Append(fragments, " " + self._attrs[start:stop])
        for item in attributes:
            if item is None:
                continue
            if type(item) is Attrs:
                for name, start, stop, _ in _parse_attrs((<Attrs>item).rendered):
                    _set_attr(names, fragments, name, " " + (<Attrs>item).rendered[start:stop])
            elif type(item) is dict or (type(item) is not bool and isinstance(item, _Mapping)):
                for raw_key, value in item.items():
                    key = _valid_key(raw_key)
                    _set_attr(names, fragments, key, _attr_fragment(key, value))
            else:
                raise StarioError(
                    f"with_attrs takes mappings and Attrs, not {type(item).__name__}",
                    context={"tag": self.tag._repr(), "value_type": type(item).__name__},
                    help_text="Pass a dict of attributes or an Attrs fragment.",
                    example='link.with_attrs({"class": "active"})',
                )
        attrs = "".join([f for f in fragments if f is not None]) or None
        if attrs is None and self._children is None:
            return self.tag.empty
        e = Element.__new__(Element)
        e.tag = self.tag
        e._attrs = attrs
        e._children = self._children
        e._first = self._first
        return e

    cdef object _rendered_attrs(self):
        if self._attrs is not None and type(self._attrs) is not str:
            raise TypeError("a @baked capture element has no rendered attributes")
        return self._attrs

    def __repr__(self):
        cdef Py_ssize_t count = 0 if self._children is None else PyTuple_GET_SIZE(self._children) - self._first
        return f"<Element {self.tag.name} attrs={self.attrs!r} children={count}>"


def element_parts(Element el not None):
    """``(open, attrs, children, tail)`` for the ``@baked`` flattener.

    Concatenated in order (with ``">"`` after ``attrs`` when there are
    children) they give the element's markup, as the old tuple form did.
    """
    cdef Tag tag = el.tag
    if el._children is None:
        return (tag.tag_start, el._attrs, None, tag.no_children_close)
    if el._attrs is None:
        return (tag.tag_start_no_attrs, None, el.children, tag.closing_tag)
    return (tag.tag_start, el._attrs, el.children, tag.closing_tag)


cdef object _attr_unreadable_error(str attrs, str reason):
    return StarioError(
        f"Cannot read the attributes back: {reason}",
        context={"attrs": attrs[:100]},
        help_text=(
            "An Attrs fragment or SafeString value in this element is not "
            'valid HTML attribute syntax (name, name="v", name=\'v\' or name=v).'
        ),
    )


cdef list _parse_attrs(object attrs):
    """Split a rendered attribute string into ``[(name, start, stop, value)]``.

    ``attrs[start:stop]`` is the attribute's own text, from its name to the
    end of its value. ``value`` is the decoded value, or ``True`` for a bare
    attribute. The syntax is HTML's: whitespace between attributes, optional
    whitespace around ``=``, and a value that is double-quoted,
    single-quoted (``data-signals='{...}'``), or unquoted. Values are
    decoded as a browser decodes them (``&nbsp;`` gives U+00A0).
    """
    cdef list out = []
    cdef str s
    cdef Py_ssize_t i = 0, n, start, name_stop, value_start, value_stop
    cdef Py_UCS4 c, quote
    cdef object value
    if attrs is None:
        return out
    s = attrs
    n = PyUnicode_GET_LENGTH(s)
    while True:
        while i < n and _is_html_space(s[i]):
            i += 1
        if i >= n:
            return out
        start = i
        while i < n and not _is_html_space(s[i]) and s[i] != "=":
            i += 1
        name_stop = i
        if name_stop == start:
            raise _attr_unreadable_error(s, "an attribute has no name")
        while i < n and _is_html_space(s[i]):
            i += 1
        if i >= n or s[i] != "=":
            # A bare attribute; ``i`` already sits on the next name.
            PyList_Append(out, (s[start:name_stop], start, name_stop, True))
            continue
        i += 1
        while i < n and _is_html_space(s[i]):
            i += 1
        if i < n and (s[i] == '"' or s[i] == "'"):
            quote = s[i]
            value_start = i + 1
            value_stop = s.find(quote, value_start)
            if value_stop < 0:
                raise _attr_unreadable_error(s, f"a value has no closing {quote}")
            i = value_stop + 1
        else:
            value_start = i
            while i < n and not _is_html_space(s[i]):
                i += 1
            value_stop = i
        value = s[value_start:value_stop]
        if "&" in value:
            value = _html_unescape(value)
        PyList_Append(out, (s[start:name_stop], start, i, value))


cdef object _attr_value(object attrs, str key):
    """The decoded value of the first attribute named ``key``, else None."""
    for name, _, _, value in _parse_attrs(attrs):
        if name == key:
            return value
    return None


cdef int _set_attr(list names, list fragments, object name, object fragment) except -1:
    """Put ``fragment`` (None: remove) at the first ``name``; drop later copies."""
    cdef Py_ssize_t i
    cdef bint found = False
    for i in range(len(names)):
        if fragments[i] is not None and names[i] == name:
            fragments[i] = None if found else fragment
            found = True
    if not found and fragment is not None:
        PyList_Append(names, name)
        PyList_Append(fragments, fragment)
    return 0


# ---------------------------------------------------------------------------
# Tag

# Tag, VoidTag and their __init__ / __call__ open their docstrings with a
# signature block too: Cython slot methods have no signature otherwise,
# and doc tools (stario.dev's apidoc) read it from there.
cdef class Tag:
    """Tag(name, *, empty='normal', prefix='', raw_text=False)
--

    Callable factory for a single HTML or SVG element name.

    Instantiate with the constructor (e.g. `P = Tag("p")`). Prefer the
    built-in catalogs in `stario.markup.html` and `stario.markup.svg` where they
    exist.

    `empty="normal"` — no children renders `<tag></tag>`; children allowed.
    `empty="void"` — no children renders `<tag/>`; children are an error
    (HTML `br`, `img`, `input`, …). `VoidTag` is the same, typed so that a
    type checker rejects children.
    `empty="self_closing_when_empty"` — no children renders `<tag/>`; children
    allowed when present (SVG leaves like `circle`, `path`, filter primitives).

    `raw_text=True` marks a raw-text element (`script`, `style`): the browser
    does not decode entities inside it, so `str` children go on the wire
    verbatim instead of text-escaped. Text that could end the element early
    (`</script`, `<!--`) is an error.

    Calling the instance (`h.Div(...)`, `h.P("hi")`, etc.) builds an
    `Element`; see `Tag.__call__`.

    Create tags once, at import. On a free-threaded build a tag is
    immortal, so threads building elements from it never write its
    refcount, and it is never freed.
    """

    cdef readonly str name
    cdef readonly str empty_mode
    cdef readonly bint raw_text
    cdef readonly str tag_start            # "<div" (with any prefix)
    cdef readonly str tag_start_no_attrs   # "<div>"
    cdef readonly str closing_tag          # "</div>"
    cdef readonly str no_children_close    # "/>" or "></div>"
    cdef readonly str empty_html           # "<div></div>" or "<br/>"
    cdef readonly Element empty            # the element a bare call returns
    cdef bint _void

    def __init__(
        self,
        str name not None,
        *,
        empty="normal",
        str prefix not None = "",
        bint raw_text=False,
    ):
        """__init__($self, /, name, *, empty='normal', prefix='', raw_text=False)
--

        Create a tag factory.

        `name` is the element name on the wire (e.g. `"div"`, `"circle"`).
        Use `empty="void"` for HTML void elements (children are an error), or
        `empty="self_closing_when_empty"` for SVG-style leaves that self-close
        only when called with no children. Optional `prefix` is prepended to the
        opening tag (e.g. `<!doctype html>` on `HtmlDocument`). `raw_text=True`
        is for `script` and `style`, whose text is not entity-decoded.
        """
        self._setup(name, empty, prefix, raw_text)

    cdef int _setup(self, str name, object empty, str prefix, bint raw_text) except -1:
        """The body of ``__init__``, also called by ``VoidTag.__init__``.

        A C call, so ``self`` keeps the single reference that the
        constructor holds: only a uniquely referenced object is made
        immortal (stario_immortalize). A Python subclass that calls
        ``super().__init__()`` holds more and keeps a mortal tag.
        """
        cdef Element e
        cdef str tag_start, closing_tag, no_children_close
        _bind()
        if self.name is not None:
            raise TypeError("Tag is already initialised; create a new Tag instead")
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
        self.empty_mode = empty
        self.raw_text = raw_text
        self._void = empty == "void"
        # Every element built from this tag renders these: shared strings
        # (interned, so immortal, on free-threaded builds).
        tag_start = prefix + "<" + name
        closing_tag = f"</{name}>"
        no_children_close = "/>" if empty != "normal" else ">" + closing_tag
        self.tag_start = stario_shared_str(tag_start)
        self.tag_start_no_attrs = stario_shared_str(tag_start + ">")
        self.closing_tag = stario_shared_str(closing_tag)
        self.no_children_close = stario_shared_str(no_children_close)
        self.empty_html = stario_shared_str(tag_start + no_children_close)
        # Immortal while nothing else refers to it yet (validation is done,
        # so a failed call never leaves an immortal object behind).
        stario_immortalize(self)
        e = Element.__new__(Element)
        e.tag = self
        stario_immortalize(e)
        self.empty = e
        self.name = name  # last: marks the tag as ready
        return 0

    def _repr(self):
        return f"Tag(name='{self.name}', empty='{self.empty_mode}')"

    def __repr__(self):
        return self._repr()

    cdef int _raw_walk(self, object child, list run, list out, bint* slots, int depth) except -1:
        """Collect raw-text content in document order.

        ``str`` pieces, numbers and ``@baked`` slots build up one *run*;
        lists are flattened into it. ``None`` and an empty ``SafeString``
        put nothing on the wire, so they never split a run: the text on both
        sides is checked as one. A non-empty ``SafeString`` is the caller's
        own markup: it ends the run and goes out unchecked. An element is an
        error, because its tags would be text inside the raw element.
        """
        cdef object child_type = type(child)
        if child_type is str:
            PyList_Append(run, child)
        elif child_type is list:
            if depth >= _MAX_DEPTH:
                raise StarioError(
                    f"Children of <{self.name}> are nested too deeply",
                    context={"tag": self._repr(), "max_depth": _MAX_DEPTH},
                    help_text="Raw-text children nest lists 1000 levels deep at most; check for a list that contains itself.",
                )
            for item in <list>child:
                self._raw_walk(item, run, out, slots, depth + 1)
        elif child_type is _BakeSlot:
            PyList_Append(run, child)
            slots[0] = True
        elif child is None:
            return 0
        elif child_type is int or child_type is float:
            PyList_Append(run, str(child))
        elif child_type is SafeString:
            if PyUnicode_GET_LENGTH((<SafeString>child).rendered):
                self._raw_flush(run, out, slots)
                PyList_Append(out, child)
        else:
            raise StarioError(
                f"<{self.name}> holds raw text, not elements",
                context={"tag": self._repr(), "child": repr(child)[:100]},
                help_text=(
                    f"The browser reads everything inside <{self.name}> as text, "
                    "so an element there would not be an element. Pass str, or a "
                    "SafeString for markup you control."
                ),
                example='h.Script("console.log(1)")',
            )
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
            PyList_Append(out, _safe(text))
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

    cdef tuple _raw_text_children(self, tuple args, Py_ssize_t first):
        cdef list run = []
        cdef list out = []
        cdef bint slots = False
        cdef Py_ssize_t i
        for i in range(first, PyTuple_GET_SIZE(args)):
            self._raw_walk(<object>PyTuple_GET_ITEM(args, i), run, out, &slots, 0)
        self._raw_flush(run, out, &slots)
        return tuple(out)

    def __call__(self, *args):
        """__call__($self, /, *children)
--

        Build one element from positional arguments.

        Arguments are processed in order:

        - Each leading mapping (`dict` or other `Mapping`) or `Attrs`
          fragment renders into the opening tag, in order. Mappings are not
          merged or deduplicated: duplicate names render twice. `None`
          values are skipped.
        - The first other argument starts the children. A mapping after
          that is an error (attributes must come first).
        - A child must be `str`, `int`, `float`, `SafeString`, an `Element`,
          a `list` of those, or `None` (skipped). Anything else (a `bool`,
          a `tuple`, an uncalled `Tag`, another object) is an error here,
          at the call that would have put it in the tree.

        Returns an `Element`. A call with no attributes and no children
        returns the tag's cached `empty` element.
        """
        cdef Py_ssize_t n = PyTuple_GET_SIZE(args)
        cdef Py_ssize_t i
        cdef Py_ssize_t first = n
        cdef bint has_children = False
        cdef bint has_attrs = False
        cdef bint has_mapping = False
        cdef PyObject* arg
        cdef PyObject* arg_type
        cdef object attrs = None
        cdef tuple children = None
        cdef Element e
        if self.name is None:
            raise TypeError("Tag is not initialised; create it with Tag(name)")
        if n == 0:
            return self.empty

        for i in range(n):
            # Borrowed: ``args`` holds every item for the whole call, and
            # the scan only compares pointers (no refcount writes).
            arg = PyTuple_GET_ITEM(args, i)
            arg_type = stario_type(arg)
            # Child content first: it is what most calls carry. A @baked
            # parameter is child content too while the builder runs.
            if (
                arg_type == <PyObject*>str
                or arg_type == <PyObject*>Element
                or arg_type == <PyObject*>SafeString
                or arg_type == <PyObject*>int
                or arg_type == <PyObject*>float
                or arg_type == <PyObject*>list
                or arg_type == <PyObject*>_BakeSlot
            ):
                pass
            elif arg == <PyObject*>None:
                continue
            elif arg_type == <PyObject*>Attrs:
                if has_children:
                    raise _attrs_before_children_error(self._repr())
                has_attrs = True
                continue
            elif (
                arg_type == <PyObject*>dict
                or (
                    arg_type != <PyObject*>bool
                    and arg_type != <PyObject*>tuple
                    and isinstance(<object>arg, _Mapping)
                )
            ):
                if has_children:
                    raise _attrs_before_children_error(self._repr())
                has_attrs = True
                has_mapping = True
                continue
            else:
                raise _bad_child_error(<object>arg, self._repr())
            if not has_children:
                has_children = True
                first = i

        if has_children and self._void:
            raise StarioError(
                f"<{self.name}> is a void element and cannot have children",
                context={"tag": self._repr()},
                help_text=(
                    "Void elements (br, img, input, ...) always render as a single "
                    "self-closing tag. Put the content in a sibling or parent element."
                ),
                example='h.Div(h.Img({"src": "x.png", "alt": "X"}), "caption")',
            )

        if has_attrs:
            attrs = _build_attrs(self, args, first)

        if has_children:
            if self.raw_text:
                children = self._raw_text_children(args, first)
                if not children:
                    children = None
            elif first == 0 or not has_mapping:
                # The args tuple holds only children, None, and immutable
                # Attrs: keep it, and skip the leading attributes.
                children = args
            else:
                # Drop the mappings: they are mutable, and the element must
                # not keep them alive.
                children = args[first:]
                first = 0

        if attrs is None and children is None:
            return self.empty

        e = Element.__new__(Element)
        e.tag = self
        e._attrs = attrs
        e._children = children
        if children is not None and not self.raw_text:
            e._first = first
        return e


cdef class VoidTag(Tag):
    """VoidTag(name, *, prefix='')
--

    A void element factory (`br`, `img`, `input`, ...): attributes only.

    Same as `Tag(name, empty="void")`; the separate type lets a type checker
    reject children (`h.Br("x")`) before the code runs.
    """

    def __init__(self, str name not None, *, str prefix not None = ""):
        """__init__($self, /, name, *, prefix='')
--

        Create a void element factory: ``Tag(name, empty="void", prefix=prefix)``.
        """
        self._setup(name, "void", prefix, False)


stario_disallow_instantiation(Element)


# ---------------------------------------------------------------------------
# classes()

cdef object _class_token_error(object token, str example):
    return StarioError(
        f"Invalid class token type: {type(token).__name__}",
        context={"token_type": type(token).__name__},
        help_text="Class tokens must be strings. Use None or False to omit a token.",
        example=example,
    )


def classes(*tokens):
    """Return a pre-rendered `class` attribute fragment.

    Tokens are `str`, `None` / `False` (skipped), or a mapping of class name
    to a condition: any truthy value includes the name. A call without a
    mapping is cached.
    """
    cdef Py_ssize_t n = PyTuple_GET_SIZE(tokens)
    cdef Py_ssize_t i
    cdef bint cacheable = True
    cdef PyObject* hit
    cdef object token, result, text
    cdef list rendered
    _bind()
    # str, None and False tokens are hashable and fully decide the result,
    # so the token tuple itself is the cache key: `classes("card", None)`
    # hits the cache like `classes("card")` does. A mapping is not cached.
    for i in range(n):
        token = <object>PyTuple_GET_ITEM(tokens, i)
        if type(token) is not str and token is not None and token is not False:
            cacheable = False
            break
    if cacheable and stario_dict_get_ref(_classes_cache, tokens, &hit):
        result = <object>hit
        Py_DECREF(result)
        return result

    rendered = []
    for i in range(n):
        token = <object>PyTuple_GET_ITEM(tokens, i)
        if type(token) is str:
            PyList_Append(rendered, c_escape_attr(token))
        elif token is None or token is False:
            continue
        elif type(token) is dict or (type(token) is not bool and isinstance(token, _Mapping)):
            for name, on in token.items():
                if type(on) is _BakeSlot:
                    raise StarioError(
                        f"@baked: parameters are not supported inside classes() conditional values (class {name!r})",
                        context={"class": name, "parameter": _slot_name(on)},
                        help_text=(
                            "Pass the finished class mapping from outside the baked "
                            "builder, or use a whole classes() fragment as one parameter."
                        ),
                    )
                if on:
                    if type(name) is not str:
                        raise _class_token_error(name, 'h.Div(classes({"btn": is_active}))')
                    PyList_Append(rendered, c_escape_attr(name))
        else:
            raise _class_token_error(token, 'h.Div(classes("btn", "primary"))')

    text = ' class="' + " ".join(rendered) + '"'
    if not cacheable or len(_classes_cache) >= _CLASSES_CACHE_MAX:
        return Attrs.__new__(Attrs, text)
    # Every thread gets the cached fragment, and each element built with it
    # holds its string: on free-threaded builds both are immortal (an
    # interned string; the Attrs is new, so uniquely referenced, until
    # setdefault publishes it). One that loses the insert race is never
    # freed; at most one per racing thread and key.
    result = Attrs.__new__(Attrs, stario_shared_str(text))
    stario_immortalize(result)
    return _classes_cache.setdefault(stario_shared_tuple(tokens), result)


# ``prefix`` -> {key: "prefix-key"}: validated wire names for data() / aria().
# Same rules as the other caches: refuse new entries past a cap and never
# clear; insert with setdefault (the first value wins and is never replaced)
# and read with stario_dict_get_ref.
cdef dict _PREFIXED_KEYS = <dict>stario_shared_dict()


cdef object _prefixed_key(object prefix, object key):
    cdef PyObject* hit
    cdef object names = None
    cdef object wire_key
    cdef bint cached = True
    if stario_dict_get_ref(_PREFIXED_KEYS, prefix, &hit):
        names = <object>hit
        Py_DECREF(names)
    elif len(_PREFIXED_KEYS) < 64:
        names = _PREFIXED_KEYS.setdefault(stario_shared_str(prefix), stario_shared_dict())
    else:
        cached = False
    if cached and type(key) is str and stario_dict_get_ref(names, key, &hit):
        wire_key = <object>hit
        Py_DECREF(wire_key)
        return wire_key
    wire_key = prefix + "-" + _valid_key(key)
    if cached and len(names) < _VALID_KEYS_MAX:
        wire_key = (<dict>names).setdefault(
            stario_shared_str(key), stario_shared_str(wire_key)
        )
    return wire_key


cdef object _prefixed_fragment(object prefix, object key, object value):
    cdef object wire_key = _prefixed_key(prefix, key)
    cdef object value_type = type(value)
    if value_type is str:
        return _pair_string(wire_key, c_escape_attr(value))
    if value_type is _BakeSlot:
        raise StarioError(
            f"@baked: parameters are not supported inside {prefix}() helper values ('{wire_key}')",
            context={"attribute": wire_key, "parameter": _slot_name(value)},
            help_text=(
                "Pass the flat attribute directly to the baked template instead, "
                f"e.g. h.Div({{{wire_key!r}: value}})."
            ),
        )
    if value_type is int or value_type is float:
        return _pair_string(wire_key, str(value))
    return _attr_fragment(wire_key, value)


cdef Attrs _prefixed(object prefix, object attrs):
    cdef list parts = []
    cdef object fragment
    _bind()
    prefix = _valid_key(prefix)
    if type(attrs) is dict:
        # ``.items()``, not ``PyDict_Next``: see _attrs_one_string.
        for key, value in (<dict>attrs).items():
            fragment = _prefixed_fragment(prefix, key, value)
            if fragment is not None:
                PyList_Append(parts, fragment)
    else:
        for key, value in attrs.items():
            fragment = _prefixed_fragment(prefix, key, value)
            if fragment is not None:
                PyList_Append(parts, fragment)
    if PyList_GET_SIZE(parts) == 1:
        return Attrs.__new__(Attrs, <object>PyList_GET_ITEM(parts, 0))
    return Attrs.__new__(Attrs, "".join(parts))


def prefixed(prefix, attrs):
    """Return pre-rendered `prefix-name` attribute fragments from a flat mapping.

    Internal primitive for `data()` and `aria()`. For other prefixes import
    from this submodule, e.g. `from stario.markup.attributes import prefixed`.
    """
    return _prefixed(prefix, attrs)


def data(attrs):
    """Return pre-rendered `data-*` attribute fragments from a flat mapping."""
    return _prefixed("data", attrs)


def aria(attrs):
    """Return pre-rendered `aria-*` attribute fragments from a flat mapping."""
    return _prefixed("aria", attrs)


# ---------------------------------------------------------------------------
# Render: depth-first into one UTF-8 buffer

cdef struct Buf:
    char* data
    Py_ssize_t len
    Py_ssize_t cap
    bint heap


cdef int _grow(Buf* b, Py_ssize_t need) except -1:
    cdef Py_ssize_t cap = b.cap * 2
    cdef char* p
    while cap < b.len + need:
        cap *= 2
    if b.heap:
        p = <char*>PyMem_Realloc(b.data, cap)
    else:
        p = <char*>PyMem_Malloc(cap)
        if p != NULL:
            memcpy(p, b.data, b.len)
    if p == NULL:
        raise MemoryError()
    b.data = p
    b.cap = cap
    b.heap = True
    return 0


cdef inline int _write(Buf* b, const char* s, Py_ssize_t n) except -1:
    if b.len + n > b.cap:
        _grow(b, n)
    memcpy(b.data + b.len, s, n)
    b.len += n
    return 0


cdef inline int _write_str(Buf* b, object s) except -1:
    cdef Py_ssize_t n
    cdef const char* p
    if PyUnicode_IS_ASCII(s):
        return _write(b, <const char*>PyUnicode_DATA(s), PyUnicode_GET_LENGTH(s))
    p = PyUnicode_AsUTF8AndSize(s, &n)
    return _write(b, p, n)


cdef int _write_text(Buf* b, object s) except -1:
    """Copy ``s`` as UTF-8 and escape ``& < >`` on the way (bytes < 0x80 only).

    Works in chunks of ``_TEXT_CHUNK`` bytes. Each chunk reserves its worst
    case (every byte becomes ``&amp;``) once, then copies byte by byte: text
    nodes are short, and a copy loop beats a ``memcpy`` call per run. The
    chunks keep a long text from reserving five times its size.
    """
    cdef const unsigned char* p
    cdef Py_ssize_t n, k, start = 0, stop, w
    cdef unsigned char c
    cdef char* d
    if PyUnicode_IS_ASCII(s):
        p = <const unsigned char*>PyUnicode_DATA(s)
        n = PyUnicode_GET_LENGTH(s)
    else:
        p = <const unsigned char*>PyUnicode_AsUTF8AndSize(s, &n)
    # A while loop for the chunks: range() with a step is not a C loop here.
    while start < n:
        stop = n if n - start <= _TEXT_CHUNK else start + _TEXT_CHUNK
        if b.len + (stop - start) * 5 > b.cap:
            _grow(b, (stop - start) * 5)
        d = b.data + b.len
        w = 0
        for k in range(start, stop):
            c = p[k]
            if not stario_text_escape[c]:
                d[w] = <char>c
                w += 1
            elif c == 38:
                d[w] = 38; d[w + 1] = 97; d[w + 2] = 109; d[w + 3] = 112; d[w + 4] = 59
                w += 5
            elif c == 60:
                d[w] = 38; d[w + 1] = 108; d[w + 2] = 116; d[w + 3] = 59
                w += 4
            else:
                d[w] = 38; d[w + 1] = 103; d[w + 2] = 116; d[w + 3] = 59
                w += 4
        b.len += w
        start = stop
    return 0


cdef inline int _write_str_ptr(Buf* b, PyObject* s) except -1:
    return _write_str(b, <object>s)


cdef int _render_element(Element e, Buf* b, int depth) except -1:
    # Borrowed reads, no references taken: an element never changes after
    # it is built, and the caller holds it (so its tag, attributes and
    # children stay alive) for the whole walk. A reference would write the
    # refcount of every object in a subtree that all threads render, such
    # as a module-level element, from every thread.
    cdef PyObject* tag = <PyObject*>e.tag
    cdef PyObject* attrs = <PyObject*>e._attrs
    cdef PyObject* children = <PyObject*>e._children
    cdef PyObject* child
    cdef PyObject* child_type
    cdef Py_ssize_t i
    if attrs == <PyObject*>None:
        if children == <PyObject*>None:
            return _write_str_ptr(b, <PyObject*>(<Tag>tag).empty_html)
        _write_str_ptr(b, <PyObject*>(<Tag>tag).tag_start_no_attrs)
    else:
        if type(<object>attrs) is not str:
            for part in <object>attrs:
                if type(part) is _AttrSlot:
                    raise _unfilled_attr_slot_error(part)
            raise StarioError("Invalid attribute parts in render tree")
        _write_str_ptr(b, <PyObject*>(<Tag>tag).tag_start)
        _write_str_ptr(b, attrs)
        if children == <PyObject*>None:
            return _write_str_ptr(b, <PyObject*>(<Tag>tag).no_children_close)
        _write(b, ">", 1)
    if depth >= _MAX_DEPTH:
        raise RecursionError("HTML tree is nested too deeply")
    for i in range(e._first, PyTuple_GET_SIZE(<object>children)):
        # Text and elements, the usual children, without the _render_node
        # call (the tuple holds each child; type compared, not referenced).
        child = PyTuple_GET_ITEM(<object>children, i)
        child_type = stario_type(child)
        if child_type == <PyObject*>str:
            _write_text(b, <object>child)
        elif child_type == <PyObject*>Element:
            _render_element(<Element>child, b, depth + 1)
        else:
            _render_node(<object>child, b, depth + 1)
    return _write_str_ptr(b, <PyObject*>(<Tag>tag).closing_tag)


cdef int _render_node(object node, Buf* b, int depth) except -1:
    cdef PyObject* node_type = stario_type(<PyObject*>node)
    if node_type == <PyObject*>str:
        return _write_text(b, node)
    if node_type == <PyObject*>Element:
        return _render_element(<Element>node, b, depth)
    if node_type == <PyObject*>SafeString:
        # Immutable, and held by the caller: a borrowed read (see above).
        return _write_str_ptr(b, <PyObject*>(<SafeString>node).rendered)
    if node_type == <PyObject*>list:
        if depth >= _MAX_DEPTH:
            raise RecursionError("HTML tree is nested too deeply")
        # Cython's list loop reads the length on each step and takes a
        # strong reference to each item: another thread may change the list.
        for item in <list>node:
            _render_node(item, b, depth + 1)
        return 0
    if node_type == <PyObject*>int or node_type == <PyObject*>float:
        return _write_str(b, str(node))
    if node is None:
        return 0
    raise _bad_node_error(node)


cdef int _render_into(object nodes, Buf* b) except -1:
    if type(nodes) is tuple:
        for i in range(PyTuple_GET_SIZE(nodes)):
            _render_node(<object>PyTuple_GET_ITEM(nodes, i), b, 0)
    else:
        for node in nodes:
            _render_node(node, b, 0)
    return 0


cdef object _render_error(BaseException e, object root_count):
    if isinstance(e, UnicodeEncodeError):
        return StarioError(
            "Text contains a lone surrogate, which UTF-8 cannot encode",
            context={
                "character": f"U+{ord((<object>e).object[(<object>e).start]):04X}",
                "text": (<object>e).object[:100],
            },
            help_text=(
                "A lone surrogate (U+D800 to U+DFFF) usually comes from broken "
                "input, such as a JSON string with a lone \\ud800 escape. Clean "
                "the text before rendering it."
            ),
            example="text.encode('utf-8', 'replace').decode('utf-8')",
        )
    if isinstance(e, RecursionError):
        return StarioError(
            "HTML tree is nested too deeply to render",
            context={"root_count": root_count if root_count is not None else "unknown"},
            help_text=(
                "The renderer walks elements recursively and stops at "
                "1000 levels of nesting. Real markup never nests this deep; "
                "check for a cycle or a self-referencing node."
            ),
        )
    return StarioError(
        f"Unexpected error while rendering HTML: {type(e).__name__}: {e}",
        context={
            "error_type": type(e).__name__,
            "error_message": str(e),
            "node_count": root_count if root_count is not None else "unknown",
        },
        help_text="Check that all HTML elements are valid types and properly structured.",
        example='render(h.Div({"class": "container"}, h.P("Hello")))',
    )


cdef object _render(object nodes, object root_count, bint as_bytes):
    cdef char stack[_STACK_BUF]
    cdef Buf b
    b.data = stack
    b.len = 0
    b.cap = _STACK_BUF
    b.heap = False
    _bind()
    try:
        _render_into(nodes, &b)
        if as_bytes:
            return PyBytes_FromStringAndSize(b.data, b.len)
        return PyUnicode_DecodeUTF8(b.data, b.len, NULL)
    except StarioError:
        raise
    except Exception as e:
        raise _render_error(e, root_count) from e
    finally:
        if b.heap:
            PyMem_Free(b.data)


def render_nodes_to_string(nodes, *, root_count=None):
    """Render an iterable of nodes with the same error contract as `render`."""
    return _render(nodes, root_count, False)


def render(*nodes):
    """Walk HTML fragments depth-first and return one string.

    Accepts any number of root nodes (variadic). Plain `str` in child position
    is HTML text-escaped; attribute escaping happens inside `Tag` when the
    opening tag is built. `SafeString` is emitted unchanged (trusted).

    Fragments from `baked` are `SafeString` values. Pass them as one root, e.g.
    `render(layout(a, b))`. You can also nest `layout(...)` wherever an element
    child is accepted, same as `h.Div(...)`.
    """
    return _render(nodes, PyTuple_GET_SIZE(nodes), False)


def render_bytes(*nodes):
    """`render`, encoded: the UTF-8 bytes of the markup, with no str in between.

    `w.html()` and the Datastar element patch use this.
    """
    return _render(nodes, PyTuple_GET_SIZE(nodes), True)


def splice_child(value):
    """One ``@baked`` child slot: the same value ladder as `render`."""
    cdef object value_type = type(value)
    if value_type is str:
        return c_escape_text(value)
    if value is None:
        return ""
    if value_type is SafeString:
        return (<SafeString>value).rendered
    if value_type is int or value_type is float:
        return str(value)
    return _render((value,), 1, False)
