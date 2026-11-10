"""``ParsedCookies``: scan ``Cookie`` headers, decode only what is asked for. Included into ``exchange.pyx``."""

cdef inline bint _is_ows(char c) noexcept:
    return c == 32 or c == 9


cdef void _strip_span(
    const char* s,
    Py_ssize_t* start,
    Py_ssize_t* end,
) noexcept:
    while start[0] < end[0] and _is_ows(s[start[0]]):
        start[0] += 1
    while end[0] > start[0] and _is_ows(s[end[0] - 1]):
        end[0] -= 1


cdef bint _cookie_attr_name(const char* s, Py_ssize_t n) noexcept:
    cdef char buf[16]
    cdef Py_ssize_t i
    cdef unsigned char c
    if n == 0 or n > 8:
        return False
    for i in range(n):
        c = <unsigned char>s[i]
        if 65 <= c <= 90:
            c += 32
        buf[i] = <char>c
    if n == 4 and memcmp(buf, "path", 4) == 0:
        return True
    if n == 6 and (
        memcmp(buf, "domain", 6) == 0 or memcmp(buf, "secure", 6) == 0
    ):
        return True
    if n == 7 and (
        memcmp(buf, "expires", 7) == 0
        or memcmp(buf, "max-age", 7) == 0
        or memcmp(buf, "comment", 7) == 0
        or memcmp(buf, "version", 7) == 0
    ):
        return True
    if n == 8 and (
        memcmp(buf, "httponly", 8) == 0 or memcmp(buf, "samesite", 8) == 0
    ):
        return True
    return False


cdef object _cookie_unquote(const char* s, Py_ssize_t n):
    cdef Py_ssize_t i
    cdef Py_ssize_t w
    cdef unsigned char d1
    cdef unsigned char d2
    cdef char* buf
    cdef bint escaped = False
    if n >= 2 and s[0] == 34 and s[n - 1] == 34:
        s += 1
        n -= 2
        for i in range(n):
            if s[i] == 92:
                escaped = True
                break
        if not escaped:
            return _decode_latin1(s, n)
        buf = <char*>PyMem_Malloc(<size_t>n)
        if buf == NULL:
            raise MemoryError()
        try:
            i = 0
            w = 0
            while i < n:
                if s[i] == 92 and i + 1 < n:
                    d1 = <unsigned char>s[i + 1]
                    if (
                        48 <= d1 <= 51
                        and i + 3 < n
                        and 48 <= <unsigned char>s[i + 2] <= 55
                        and 48 <= <unsigned char>s[i + 3] <= 55
                    ):
                        buf[w] = <char>(
                            ((d1 - 48) << 6)
                            | ((<unsigned char>s[i + 2] - 48) << 3)
                            | (<unsigned char>s[i + 3] - 48)
                        )
                        w += 1
                        i += 4
                    else:
                        buf[w] = <char>d1
                        w += 1
                        i += 2
                else:
                    buf[w] = s[i]
                    w += 1
                    i += 1
            return _decode_latin1(buf, w)
        finally:
            PyMem_Free(buf)
    return _decode_latin1(s, n)


cdef int _next_cookie_pair(
    const char* s,
    Py_ssize_t n,
    Py_ssize_t* i,
    Py_ssize_t* name_start,
    Py_ssize_t* name_end,
    Py_ssize_t* val_start,
    Py_ssize_t* val_end,
) noexcept:
    """Advance ``i`` to the next ``name=value`` pair. 1 found, 0 done."""
    cdef Py_ssize_t j
    cdef bint quoted
    while i[0] < n:
        # Name: everything before '=' (or ';' / end — then it is junk to skip).
        while i[0] < n and _is_ows(s[i[0]]):
            i[0] += 1
        if i[0] >= n:
            return 0
        name_start[0] = i[0]
        while i[0] < n and s[i[0]] != 59 and s[i[0]] != 61:
            i[0] += 1
        if i[0] >= n or s[i[0]] == 59:
            while i[0] < n and s[i[0]] != 59:
                i[0] += 1
            if i[0] < n:
                i[0] += 1
            continue
        name_end[0] = i[0]
        _strip_span(s, name_start, name_end)
        i[0] += 1

        # Value: quoted strings keep their escapes, else run to ';'.
        while i[0] < n and _is_ows(s[i[0]]):
            i[0] += 1
        val_start[0] = i[0]
        quoted = False
        if i[0] < n and s[i[0]] == 34:
            # A quoted value ends at its closing quote, but never spans a
            # ';': browsers split pairs there whatever the quotes say, so a
            # planted `a="x` ... `z=y"` must not swallow the cookies between.
            j = i[0] + 1
            while j < n and s[j] != 59:
                if s[j] == 92 and j + 1 < n and s[j + 1] != 59:
                    j += 2
                    continue
                if s[j] == 34:
                    j += 1
                    quoted = True
                    break
                j += 1
        if quoted:
            val_end[0] = j
            while j < n and s[j] != 59:
                j += 1
            i[0] = j
        else:
            while i[0] < n and s[i[0]] != 59:
                i[0] += 1
            val_end[0] = i[0]
            _strip_span(s, val_start, val_end)
        if i[0] < n and s[i[0]] == 59:
            i[0] += 1

        if name_end[0] <= name_start[0]:
            continue
        # "$name" (RFC 2965) and Set-Cookie attribute names are not cookies.
        if s[name_start[0]] == 36:
            continue
        if _cookie_attr_name(s + name_start[0], name_end[0] - name_start[0]):
            continue
        return 1
    return 0


cdef object _cookie_decode_value(const char* s, Py_ssize_t start, Py_ssize_t end):
    if end > start and s[start] == 34:
        return _cookie_unquote(s + start, end - start)
    return _decode_latin1(s + start, end - start)


cdef object _cookie_find_in_line(
    const char* s,
    Py_ssize_t n,
    const char* name,
    Py_ssize_t nlen,
):
    """Last ``name=`` in this header, or ``None``. Decodes only that value."""
    cdef Py_ssize_t i = 0
    cdef Py_ssize_t name_start
    cdef Py_ssize_t name_end
    cdef Py_ssize_t val_start
    cdef Py_ssize_t val_end
    cdef Py_ssize_t found_start = -1
    cdef Py_ssize_t found_end = 0
    while _next_cookie_pair(
        s, n, &i, &name_start, &name_end, &val_start, &val_end
    ):
        if name_end - name_start == nlen and memcmp(
            s + name_start, name, <size_t>nlen
        ) == 0:
            found_start = val_start
            found_end = val_end
    if found_start < 0:
        return None
    return _cookie_decode_value(s, found_start, found_end)


cdef void _parse_cookie_line(
    const char* s,
    Py_ssize_t n,
    dict out,
) except *:
    cdef Py_ssize_t i = 0
    cdef Py_ssize_t name_start
    cdef Py_ssize_t name_end
    cdef Py_ssize_t val_start
    cdef Py_ssize_t val_end
    while _next_cookie_pair(
        s, n, &i, &name_start, &name_end, &val_start, &val_end
    ):
        out[_decode_latin1(s + name_start, name_end - name_start)] = (
            _cookie_decode_value(s, val_start, val_end)
        )


@cython.final
cdef class ParsedCookies:
    """Cookie scan. ``get`` walks C pairs and decodes only the name you ask for.

    Later ``Cookie`` headers win; within one header, later pairs win. ``get``
    starts at the last header so unused earlier cookies stay untouched.
    ``as_dict`` / ``items`` parse everything.
    """

    def __cinit__(self):
        self._headers = None
        self._lines = None
        self._parsed = None

    def __init__(self, lines=None):
        self._headers = None
        self._lines = []
        self._parsed = None
        if lines:
            self._extend_lines(lines)

    cdef void bind_request_headers(self, object headers) noexcept:
        self._headers = headers
        self._lines = None
        self._parsed = None

    cdef void _extend_lines(self, object lines) except *:
        cdef object line
        self._parsed = None
        if self._lines is None:
            self._lines = []
        for line in lines:
            if isinstance(line, str):
                self._lines.append((<str>line).encode("latin-1"))
            elif not line:
                continue
            elif type(line) is bytes:
                self._lines.append(line)
            elif isinstance(line, (bytes, bytearray, memoryview)):
                # Scanned through PyBytes_AS_STRING: store an exact bytes copy.
                self._lines.append(_wire_part(line))
            else:
                raise TypeError(
                    "cookie lines must be str or bytes, got " + type(line).__name__
                )

    cdef list _cookie_lines(self):
        cdef object headers
        cdef list values
        cdef list out
        cdef object value
        if self._lines is not None:
            return self._lines
        headers = self._headers
        out = []
        if headers is None:
            self._lines = out
            return out
        if isinstance(headers, RequestHeaders):
            values = (<RequestHeaders>headers).c_getlist_n("cookie", 6)
            out.extend(values)
        else:
            self._lines = out
            self._extend_lines(headers.getlist("cookie"))
            return self._lines
        self._lines = out
        return out

    cdef object _get_arena(self, const char* name, Py_ssize_t nlen):
        cdef RequestHeaders headers
        cdef RawHeader* raw
        cdef const char* arena
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef object found
        headers = <RequestHeaders>self._headers
        if headers._cookie_index() < 0:
            return None
        arena = headers._arena()
        raw = headers._headers()
        # Later Cookie headers win: scan the table in reverse.
        for index in range(headers._count() - 1, -1, -1):
            header = &raw[index]
            if header.name_length != 6 or memcmp(
                arena + header.name_offset,
                "cookie",
                6,
            ) != 0:
                continue
            found = _cookie_find_in_line(
                arena + header.value_offset,
                <Py_ssize_t>header.value_length,
                name,
                nlen,
            )
            if found is not None:
                return found
        return None

    cdef object _get_lines(self, const char* name, Py_ssize_t nlen):
        cdef list lines
        cdef Py_ssize_t i
        cdef bytes raw
        cdef object found
        lines = self._cookie_lines()
        i = PyList_GET_SIZE(lines)
        while i > 0:
            i -= 1
            raw = <bytes>lines[i]
            found = _cookie_find_in_line(
                PyBytes_AS_STRING(raw),
                PyBytes_GET_SIZE(raw),
                name,
                nlen,
            )
            if found is not None:
                return found
        return None

    def get(self, name, default=None):
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t n
        cdef object found
        if not isinstance(name, str):
            return default
        try:
            _latin1_stack(name, buf, HEADER_NAME_STACK, &n)
        except (ValueError, UnicodeError):
            # Too long or not Latin-1: no cookie has that name.
            return default
        if self._headers is not None and isinstance(self._headers, RequestHeaders):
            found = self._get_arena(buf, n)
        else:
            found = self._get_lines(buf, n)
        if found is None:
            return default
        return found

    def as_dict(self):
        cdef dict out
        cdef RequestHeaders headers
        cdef bytes raw
        if self._parsed is not None:
            return self._parsed
        out = {}
        if self._headers is not None and isinstance(self._headers, RequestHeaders):
            headers = <RequestHeaders>self._headers
            headers.c_parse_cookies(out)
        else:
            for raw in self._cookie_lines():
                _parse_cookie_line(
                    PyBytes_AS_STRING(raw),
                    PyBytes_GET_SIZE(raw),
                    out,
                )
        self._parsed = out
        return out

    def items(self):
        return list(self.as_dict().items())

    def keys(self):
        return list(self.as_dict().keys())

    def values(self):
        return list(self.as_dict().values())

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self):
        return len(self.as_dict())

    cdef bint _has_any(self):
        cdef list lines
        cdef bytes raw
        cdef RequestHeaders headers
        cdef const char* arena
        cdef RawHeader* table
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef Py_ssize_t i
        cdef Py_ssize_t name_start
        cdef Py_ssize_t name_end
        cdef Py_ssize_t val_start
        cdef Py_ssize_t val_end
        if self._headers is not None and isinstance(self._headers, RequestHeaders):
            headers = <RequestHeaders>self._headers
            if headers._cookie_index() < 0:
                return False
            arena = headers._arena()
            table = headers._headers()
            for index in range(headers._count()):
                header = &table[index]
                if header.name_length != 6 or memcmp(
                    arena + header.name_offset,
                    "cookie",
                    6,
                ) != 0:
                    continue
                i = 0
                if _next_cookie_pair(
                    arena + header.value_offset,
                    <Py_ssize_t>header.value_length,
                    &i,
                    &name_start,
                    &name_end,
                    &val_start,
                    &val_end,
                ):
                    return True
            return False

        lines = self._cookie_lines()
        for raw in lines:
            i = 0
            if _next_cookie_pair(
                PyBytes_AS_STRING(raw),
                PyBytes_GET_SIZE(raw),
                &i,
                &name_start,
                &name_end,
                &val_start,
                &val_end,
            ):
                return True
        return False

    def __bool__(self):
        return self._has_any()

    def __contains__(self, name):
        cdef object sentinel = object()
        if not isinstance(name, str):
            return False
        return self.get(name, sentinel) is not sentinel

    def __getitem__(self, name):
        cdef object sentinel = object()
        cdef object found = self.get(name, sentinel)
        if found is sentinel:
            raise KeyError(name)
        return found

    def __eq__(self, other):
        if isinstance(other, ParsedCookies):
            return self.as_dict() == other.as_dict()
        if isinstance(other, dict):
            return self.as_dict() == other
        return NotImplemented

    def __repr__(self):
        return f"ParsedCookies({self.as_dict()!r})"
