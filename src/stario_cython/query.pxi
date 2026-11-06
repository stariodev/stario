"""``ParsedQuery``: lazy index over the raw query bytes. Included into ``exchange.pyx``."""

cdef object _unquote_plus_component(const char* s, Py_ssize_t n):
    """Match ``urllib.parse.unquote_plus`` on a Latin-1 query component."""
    cdef Py_ssize_t i = 0
    cdef Py_ssize_t w
    cdef int h1
    cdef int h2
    cdef unsigned char c
    cdef char* buf
    cdef list parts
    cdef object chunk
    if n <= 0:
        return ""
    buf = <char*>PyMem_Malloc(<size_t>n)
    if buf == NULL:
        raise MemoryError()
    parts = []
    try:
        while i < n:
            # High bytes decode one Latin-1 char each; ASCII runs (with
            # '+' and '%XX' resolved) decode as UTF-8 with replacement.
            if <unsigned char>s[i] >= 128:
                parts.append(_decode_latin1(s + i, 1))
                i += 1
                continue
            w = 0
            while i < n and <unsigned char>s[i] < 128:
                c = <unsigned char>s[i]
                if c == 43:
                    buf[w] = 32
                    w += 1
                    i += 1
                elif c == 37 and i + 2 < n:
                    h1 = _hex_nibble(<unsigned char>s[i + 1])
                    h2 = _hex_nibble(<unsigned char>s[i + 2])
                    if h1 >= 0 and h2 >= 0:
                        buf[w] = <char>((h1 << 4) | h2)
                        w += 1
                        i += 3
                    else:
                        buf[w] = 37
                        w += 1
                        i += 1
                else:
                    buf[w] = <char>c
                    w += 1
                    i += 1
            if w:
                chunk = PyUnicode_DecodeUTF8(buf, w, <const char*>b"replace")
                parts.append(chunk)
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return "".join(parts)
    finally:
        PyMem_Free(buf)


cdef inline bint _span_needs_unquote(const char* s, Py_ssize_t n) noexcept:
    cdef Py_ssize_t i
    for i in range(n):
        if s[i] == 37 or s[i] == 43:
            return True
    return False


cdef inline bint _span_all_ascii(const char* s, Py_ssize_t n) noexcept:
    cdef Py_ssize_t i
    for i in range(n):
        if <unsigned char>s[i] >= 128:
            return False
    return True


cdef bint query_has_nul(const char* s, Py_ssize_t n) noexcept:
    """True when a raw query carries a NUL: a bare byte or ``%00``.

    The protocol answers 400 before any handler runs. Decoding would hand
    the app a string with ``\\x00`` in it, which no route or store wants.
    """
    cdef Py_ssize_t i = 0
    while i < n:
        if s[i] == 0:
            return True
        if s[i] == 37 and i + 2 < n and s[i + 1] == 48 and s[i + 2] == 48:
            return True
        i += 1
    return False


def request_query_ok(raw):
    """False when a raw query (bytes, after ``?``) would be answered 400."""
    cdef bytes data = bytes(raw)
    return not query_has_nul(PyBytes_AS_STRING(data), PyBytes_GET_SIZE(data))


cdef object _decode_query_component(const char* s, Py_ssize_t n):
    if n <= 0:
        return ""
    if _span_needs_unquote(s, n):
        return _unquote_plus_component(s, n)
    return _decode_latin1(s, n)


cdef object _decode_utf8_replace(const char* s, Py_ssize_t n):
    if n <= 0:
        return ""
    return PyUnicode_DecodeUTF8(s, n, <const char*>b"replace")


cdef Py_ssize_t _query_unquote_ascii_inplace(char* s, Py_ssize_t n) noexcept:
    """Percent-decode ``+`` / ``%XX`` in place. Output length is always ≤ n."""
    cdef Py_ssize_t r = 0
    cdef Py_ssize_t w = 0
    cdef int h1
    cdef int h2
    cdef unsigned char c
    while r < n:
        c = <unsigned char>s[r]
        if c == 43:
            s[w] = 32
            r += 1
            w += 1
        elif c == 37 and r + 2 < n:
            h1 = _hex_nibble(<unsigned char>s[r + 1])
            h2 = _hex_nibble(<unsigned char>s[r + 2])
            if h1 >= 0 and h2 >= 0:
                s[w] = <char>((h1 << 4) | h2)
                r += 3
                w += 1
            else:
                s[w] = 37
                r += 1
                w += 1
        else:
            s[w] = <char>c
            r += 1
            w += 1
    return w


cdef int _next_query_pair(
    const char* s,
    Py_ssize_t n,
    Py_ssize_t* i,
    Py_ssize_t* name_start,
    Py_ssize_t* name_end,
    Py_ssize_t* val_start,
    Py_ssize_t* val_end,
) noexcept:
    """Advance ``i`` to the next ``name[=value]`` pair. 1 found, 0 done."""
    cdef Py_ssize_t start
    cdef Py_ssize_t eq
    cdef Py_ssize_t end
    while i[0] < n:
        # Scan to '&' (or end), remembering the first '='.
        start = i[0]
        eq = -1
        while i[0] < n and s[i[0]] != 38:
            if eq < 0 and s[i[0]] == 61:
                eq = i[0]
            i[0] += 1
        end = i[0]
        if i[0] < n and s[i[0]] == 38:
            i[0] += 1
        if start >= end:
            continue

        if eq < 0:
            name_start[0] = start
            name_end[0] = end
            val_start[0] = end
            val_end[0] = end
        else:
            name_start[0] = start
            name_end[0] = eq
            val_start[0] = eq + 1
            val_end[0] = end
        return 1
    return 0


cdef enum:
    QUERY_PARAM_INIT = 16
    # Set when the name was decoded inside ``_copy`` and is no longer the raw span.
    QUERY_NAME_INPLACE = 1


ctypedef struct RawQueryParam:
    Py_ssize_t name_off
    Py_ssize_t name_len
    Py_ssize_t value_off
    Py_ssize_t value_len
    int flags


cdef bint _query_name_matches(
    const char* buf,
    RawQueryParam* param,
    const char* key,
    Py_ssize_t klen,
    object key_obj,
) except -1:
    cdef const char* name = buf + param.name_off
    cdef Py_ssize_t nlen = param.name_len
    if param.flags & QUERY_NAME_INPLACE:
        if _span_all_ascii(name, nlen):
            return nlen == klen and memcmp(name, key, <size_t>klen) == 0
        return _decode_utf8_replace(name, nlen) == key_obj
    if not _span_needs_unquote(name, nlen):
        if _span_all_ascii(name, nlen):
            return nlen == klen and memcmp(name, key, <size_t>klen) == 0
        return _decode_latin1(name, nlen) == key_obj
    return _decode_query_component(name, nlen) == key_obj


@cython.final
cdef class ParsedQuery:
    """Query index. First read fills C name/value spans.

    Names that need no unquote stay on the original bytes. Names with ``+``
    / ``%XX`` get a private copy so they can decode in place. ``get``
    memcmps typical ASCII names, then decodes only that value. First
    ``name=`` wins. ``items`` / ``as_dict`` / ``as_lists`` decode everything.
    """

    cdef bytes _raw
    cdef object _owner
    cdef Py_ssize_t _off
    cdef Py_ssize_t _len
    cdef char* _copy
    cdef Py_ssize_t _copy_len
    cdef Py_ssize_t _copy_cap
    cdef RawQueryParam* _params
    cdef Py_ssize_t _pcount
    cdef Py_ssize_t _pcap
    cdef bint _indexed

    def __cinit__(self):
        self._raw = None
        self._owner = None
        self._off = 0
        self._len = 0
        self._copy = NULL
        self._copy_len = 0
        self._copy_cap = 0
        self._params = NULL
        self._pcount = 0
        self._pcap = 0
        self._indexed = False

    def __dealloc__(self):
        if self._copy != NULL:
            PyMem_Free(self._copy)
            self._copy = NULL
        if self._params != NULL:
            PyMem_Free(self._params)
            self._params = NULL

    def __init__(self, raw=b""):
        self.bind_bytes(raw)

    cdef void _reset_view(self) noexcept:
        self._indexed = False
        self._pcount = 0
        self._copy_len = 0

    cdef void bind_bytes(self, object raw) noexcept:
        self._raw = raw if raw else b""
        self._owner = None
        self._off = 0
        self._len = PyBytes_GET_SIZE(self._raw) if self._raw else 0
        self._reset_view()

    cdef void bind_span(self, object owner, Py_ssize_t off, Py_ssize_t n) noexcept:
        """Point at request-arena query bytes. Copied only if a name needs unquote."""
        self._raw = None
        self._owner = owner
        self._off = off
        self._len = n
        self._reset_view()

    cdef bint _resolve_span(self, const char** out, Py_ssize_t* n) noexcept:
        """Where the raw query bytes live now: owned ``bytes`` or the arena span."""
        cdef RequestHeaders view
        cdef const char* arena
        n[0] = self._len
        if n[0] <= 0:
            out[0] = NULL
            return False
        if self._raw is not None:
            out[0] = PyBytes_AS_STRING(self._raw)
            return True
        if self._owner is not None:
            view = <RequestHeaders>self._owner
            arena = view._arena()
            if arena != NULL and self._off + self._len <= view._arena_len():
                out[0] = arena + self._off
                return True
        out[0] = NULL
        n[0] = 0
        return False

    cdef int _grow_params(self, Py_ssize_t need) except -1:
        cdef Py_ssize_t cap = self._pcap
        cdef void* p
        if cap >= need:
            return 0
        if cap == 0:
            cap = QUERY_PARAM_INIT
        while cap < need:
            cap *= 2
        if self._params == NULL:
            p = PyMem_Malloc(<size_t>cap * sizeof(RawQueryParam))
        else:
            p = PyMem_Realloc(self._params, <size_t>cap * sizeof(RawQueryParam))
        if p == NULL:
            raise MemoryError()
        self._params = <RawQueryParam*>p
        self._pcap = cap
        return 0

    cdef int _ensure_copy(self, const char* src, Py_ssize_t n) except -1:
        cdef void* p
        if n <= 0:
            self._copy_len = 0
            return 0
        if self._copy_cap < n:
            if self._copy == NULL:
                p = PyMem_Malloc(<size_t>n)
            else:
                p = PyMem_Realloc(self._copy, <size_t>n)
            if p == NULL:
                raise MemoryError()
            self._copy = <char*>p
            self._copy_cap = n
        memcpy(self._copy, src, <size_t>n)
        self._copy_len = n
        return 0

    cdef const char* _query_buf(self) noexcept:
        """The buffer param offsets index into: ``_copy`` if taken, else the span."""
        cdef const char* src = NULL
        cdef Py_ssize_t n = 0
        if self._copy_len > 0:
            return self._copy
        if self._resolve_span(&src, &n):
            return src
        return NULL

    cdef void _drop_query_owner(self) noexcept:
        self._raw = None
        self._owner = None
        self._off = 0

    cdef void _ensure_index(self) except *:
        cdef const char* src = NULL
        cdef Py_ssize_t n = 0
        cdef Py_ssize_t i = 0
        cdef Py_ssize_t name_start
        cdef Py_ssize_t name_end
        cdef Py_ssize_t val_start
        cdef Py_ssize_t val_end
        cdef Py_ssize_t name_len
        cdef Py_ssize_t p
        cdef bint need_copy = False
        cdef char* buf
        cdef RawQueryParam* param
        if self._indexed:
            return
        self._pcount = 0
        if self._resolve_span(&src, &n) and src != NULL and n > 0:
            while _next_query_pair(
                src, n, &i, &name_start, &name_end, &val_start, &val_end
            ):
                self._grow_params(self._pcount + 1)
                param = &self._params[self._pcount]
                name_len = name_end - name_start
                param.name_off = name_start
                param.name_len = name_len
                param.value_off = val_start
                param.value_len = val_end - val_start
                param.flags = 0
                if (
                    name_len > 0
                    and _span_needs_unquote(src + name_start, name_len)
                    and _span_all_ascii(src + name_start, name_len)
                ):
                    need_copy = True
                self._pcount += 1

            # Names needing unquote decode once into a private copy; the
            # borrowed span is only read for values and clean names.
            if need_copy:
                self._ensure_copy(src, n)
                buf = self._copy
                for p in range(self._pcount):
                    param = &self._params[p]
                    name_len = param.name_len
                    if (
                        name_len > 0
                        and _span_needs_unquote(buf + param.name_off, name_len)
                        and _span_all_ascii(buf + param.name_off, name_len)
                    ):
                        param.name_len = _query_unquote_ascii_inplace(
                            buf + param.name_off, name_len
                        )
                        param.flags = QUERY_NAME_INPLACE
                self._drop_query_owner()
        else:
            self._drop_query_owner()
        self._indexed = True

    cdef object _decode_name_at(self, Py_ssize_t i):
        cdef RawQueryParam* param = &self._params[i]
        cdef const char* buf = self._query_buf()
        cdef const char* name
        cdef Py_ssize_t nlen = param.name_len
        if buf == NULL:
            return ""
        name = buf + param.name_off
        if param.flags & QUERY_NAME_INPLACE:
            if _span_all_ascii(name, nlen):
                return _decode_latin1(name, nlen)
            return _decode_utf8_replace(name, nlen)
        return _decode_query_component(name, nlen)

    cdef object _decode_value_at(self, Py_ssize_t i):
        cdef RawQueryParam* param = &self._params[i]
        cdef const char* buf = self._query_buf()
        if buf == NULL:
            return ""
        return _decode_query_component(
            buf + param.value_off, param.value_len
        )

    cdef bint _name_eq(self, Py_ssize_t i, const char* kbuf, Py_ssize_t klen, object key) except -1:
        cdef const char* buf = self._query_buf()
        if buf == NULL:
            return False
        return _query_name_matches(buf, &self._params[i], kbuf, klen, key)

    cdef object _index_get(self, object key, object default):
        cdef const char* kbuf
        cdef Py_ssize_t klen
        cdef Py_ssize_t i
        self._ensure_index()
        if self._pcount == 0:
            return default
        kbuf = PyUnicode_AsUTF8AndSize(key, &klen)
        for i in range(self._pcount):
            if self._name_eq(i, kbuf, klen, key):
                return self._decode_value_at(i)
        return default

    cdef list _index_getlist(self, object key):
        cdef const char* kbuf
        cdef Py_ssize_t klen
        cdef Py_ssize_t i
        cdef list out = []
        self._ensure_index()
        if self._pcount == 0:
            return out
        kbuf = PyUnicode_AsUTF8AndSize(key, &klen)
        for i in range(self._pcount):
            if self._name_eq(i, kbuf, klen, key):
                out.append(self._decode_value_at(i))
        return out

    cdef bint _index_contains(self, object key) except -1:
        cdef const char* kbuf
        cdef Py_ssize_t klen
        cdef Py_ssize_t i
        self._ensure_index()
        if self._pcount == 0:
            return False
        kbuf = PyUnicode_AsUTF8AndSize(key, &klen)
        for i in range(self._pcount):
            if self._name_eq(i, kbuf, klen, key):
                return True
        return False

    cdef bint _has_any(self) except -1:
        self._ensure_index()
        return self._pcount != 0

    def get(self, key, default=None):
        if not isinstance(key, str):
            return default
        return self._index_get(key, default)

    def getlist(self, key):
        if not isinstance(key, str):
            return []
        return self._index_getlist(key)

    def items(self):
        cdef Py_ssize_t i
        cdef list out = []
        self._ensure_index()
        for i in range(self._pcount):
            out.append((self._decode_name_at(i), self._decode_value_at(i)))
        return out

    def as_dict(self, *, last=False):
        cdef dict out = {}
        cdef Py_ssize_t i
        cdef object key
        self._ensure_index()
        for i in range(self._pcount):
            key = self._decode_name_at(i)
            if last or key not in out:
                out[key] = self._decode_value_at(i)
        return out

    def as_lists(self):
        cdef dict out = {}
        cdef Py_ssize_t i
        cdef object key
        cdef list existing
        self._ensure_index()
        for i in range(self._pcount):
            key = self._decode_name_at(i)
            existing = out.get(key)
            if existing is None:
                out[key] = [self._decode_value_at(i)]
            else:
                existing.append(self._decode_value_at(i))
        return out

    def __contains__(self, key):
        if not isinstance(key, str):
            return False
        return self._index_contains(key)

    def __bool__(self):
        return self._has_any()

    def __len__(self):
        cdef set seen = set()
        cdef Py_ssize_t i
        self._ensure_index()
        for i in range(self._pcount):
            seen.add(self._decode_name_at(i))
        return len(seen)

    def __eq__(self, other):
        if isinstance(other, ParsedQuery):
            return self.items() == other.items()
        return NotImplemented

    def __repr__(self):
        return f"ParsedQuery({self.as_lists()!r})"
