"""Request-target scan, percent decoding, canonical 308 path, host parsing. Included into ``exchange.pyx``."""

cdef inline int _hex_nibble(unsigned char c) noexcept:
    if 48 <= c <= 57:
        return c - 48
    if 65 <= c <= 70:
        return c - 55
    if 97 <= c <= 102:
        return c - 87
    return -1


cdef bint host_value_ok(const char* value, size_t n) noexcept:
    """Reject Host / :authority values that can break out of the host form.

    `?` and `#` turn ``//{label}.example.com`` into a different origin.
    `/`, ``\\``, `@`, and ASCII controls are not valid in a host header.
    IPv6 literals (``[::1]:port``) still pass.
    """
    cdef size_t i
    cdef unsigned char c
    for i in range(n):
        c = <unsigned char>value[i]
        if (
            c <= 32
            or c == 127
            or c == 35
            or c == 47
            or c == 63
            or c == 64
            or c == 92
        ):
            return False
    return True


cdef inline int _utf8_step(
    int* need,
    int* lo,
    int* hi,
    unsigned char b,
) noexcept:
    """One byte of strict UTF-8 (no overlongs, surrogates, or > U+10FFFF)."""
    if need[0] == 0:
        if b < 0x80:
            return 0
        lo[0] = 0x80
        hi[0] = 0xBF
        if 0xC2 <= b <= 0xDF:
            need[0] = 1
        elif b == 0xE0:
            need[0] = 2
            lo[0] = 0xA0
        elif 0xE1 <= b <= 0xEC or 0xEE <= b <= 0xEF:
            need[0] = 2
        elif b == 0xED:
            need[0] = 2
            hi[0] = 0x9F
        elif b == 0xF0:
            need[0] = 3
            lo[0] = 0x90
        elif 0xF1 <= b <= 0xF3:
            need[0] = 3
        elif b == 0xF4:
            need[0] = 3
            hi[0] = 0x8F
        else:
            return -1
        return 0
    if b < lo[0] or b > hi[0]:
        return -1
    lo[0] = 0x80
    hi[0] = 0xBF
    need[0] -= 1
    return 0


cdef enum:
    PC_PLAIN = 0
    PC_SLASH = 1
    PC_PCT = 2
    PC_DOT = 3
    PC_BAD = 4
    PC_HIGH = 5

# Wire byte classes for request paths. BAD: space, controls, DEL, '#', '?'.
cdef unsigned char _PATH_CLASS[256]


cdef void _init_path_class() noexcept:
    cdef int c
    for c in range(256):
        if c <= 32 or c == 127 or c == 35 or c == 63:
            _PATH_CLASS[c] = PC_BAD
        elif c >= 128:
            _PATH_CLASS[c] = PC_HIGH
        else:
            _PATH_CLASS[c] = PC_PLAIN
    _PATH_CLASS[47] = PC_SLASH
    _PATH_CLASS[37] = PC_PCT
    _PATH_CLASS[46] = PC_DOT


_init_path_class()


cdef int scan_request_path(const char* p, Py_ssize_t n, bint allow_raw_utf8) noexcept:
    """Validate an origin-form path (no query) and classify it.

    Returns ``PATH_*`` bits, or -1 for a path that must be rejected with 400:
    not starting with ``/``; ``#``, space, or controls on the wire; a bad
    ``%XX``; a decoded control byte (C0 or DEL); or decoded bytes that are
    not strict UTF-8. ``PATH_REDIRECT`` marks dot segments (``.``, ``..``,
    including ``%2E`` forms) or a trailing slash: the canonical path differs.
    """
    cdef int flags = 0
    cdef Py_ssize_t i
    cdef Py_ssize_t seg_len = 0
    cdef bint seg_dots = True
    cdef int need = 0
    cdef int lo = 0x80
    cdef int hi = 0xBF
    cdef int h1
    cdef int h2
    cdef unsigned char c
    cdef unsigned char d
    cdef unsigned char k
    cdef Py_ssize_t start
    if n <= 0 or p[0] != 47:
        return -1
    i = 1
    while i < n:
        c = <unsigned char>p[i]
        k = _PATH_CLASS[c]
        if k == PC_PLAIN and need == 0:
            # Fast path: a run of unremarkable bytes in one skip.
            start = i
            i += 1
            while i < n and _PATH_CLASS[<unsigned char>p[i]] == PC_PLAIN:
                i += 1
            seg_len += i - start
            seg_dots = False
            continue
        if k == PC_SLASH:
            if need != 0:
                return -1
            if seg_dots and (seg_len == 1 or seg_len == 2):
                flags |= PATH_REDIRECT
            seg_len = 0
            seg_dots = True
            i += 1
            continue
        if k == PC_BAD:
            return -1
        if k == PC_PCT:
            if i + 2 >= n:
                return -1
            h1 = _hex_nibble(<unsigned char>p[i + 1])
            h2 = _hex_nibble(<unsigned char>p[i + 2])
            if h1 < 0 or h2 < 0:
                return -1
            d = <unsigned char>(h1 * 16 + h2)
            flags |= PATH_HAS_PCT
            i += 3
            if d < 32 or d == 127:
                return -1
        else:
            if k == PC_HIGH:
                if not allow_raw_utf8:
                    return -1
                flags |= PATH_RAW_UTF8
            d = c
            i += 1

        # Decoded bytes are rechecked as strict UTF-8.
        if (d >= 0x80 or need != 0) and _utf8_step(&need, &lo, &hi, d) != 0:
            return -1
        seg_len += 1
        if d != 46:
            seg_dots = False

    if need != 0:
        return -1
    if seg_dots and (seg_len == 1 or seg_len == 2):
        flags |= PATH_REDIRECT
    if n > 1 and p[n - 1] == 47:
        flags |= PATH_REDIRECT
    return flags


cdef Py_ssize_t _pct_decode_into(const char* src, Py_ssize_t n, char* dst) noexcept:
    """Decode every ``%XX`` (``%2F`` included). -1 on a malformed escape."""
    cdef Py_ssize_t i = 0
    cdef Py_ssize_t w = 0
    cdef int h1
    cdef int h2
    while i < n:
        if src[i] == 37:
            if i + 2 >= n:
                return -1
            h1 = _hex_nibble(<unsigned char>src[i + 1])
            h2 = _hex_nibble(<unsigned char>src[i + 2])
            if h1 < 0 or h2 < 0:
                return -1
            dst[w] = <char>(h1 * 16 + h2)
            i += 3
        else:
            dst[w] = src[i]
            i += 1
        w += 1
    return w


cdef object decode_path_full(const char* p, Py_ssize_t n):
    """Fully percent-decoded path as ``str``, or None if it is not valid UTF-8."""
    cdef char stack[512]
    cdef char* buf = stack
    cdef Py_ssize_t w
    cdef object out
    if n <= 0:
        return ""
    if n > 512:
        buf = <char*>malloc(<size_t>n)
        if buf == NULL:
            return None
    try:
        w = _pct_decode_into(p, n, buf)
        if w < 0:
            return None
        out = PyUnicode_DecodeUTF8(buf, w, NULL)
        return out
    except UnicodeDecodeError:
        return None
    finally:
        if buf != stack:
            free(buf)


cdef int _dot_kind(bytes seg):
    cdef bytes plain = seg
    if len(seg) > 6:
        return 0
    if b"%" in seg:
        plain = seg.replace(b"%2e", b".").replace(b"%2E", b".")
    if plain == b".":
        return 1
    if plain == b"..":
        return 2
    return 0


cdef bytes canonical_path(const char* p, Py_ssize_t n):
    """Canonical form of a scanned path for a 308 ``Location``.

    RFC 3986 dot-segment removal (``%2E`` counts as ``.``, as in the WHATWG
    URL parser), then no trailing slash. Other segments stay exactly as sent,
    percent-encoding included. Leading empty segments are dropped so the
    target can never be protocol-relative (``//host``), and ``\\`` is sent as
    ``%5C`` because browsers treat a raw backslash as ``/``.
    """
    cdef list out = []
    cdef bytes seg
    cdef int kind
    cdef bytes raw = PyBytes_FromStringAndSize(p, n)
    for seg in raw.split(b"/")[1:]:
        kind = _dot_kind(seg)
        if kind == 1:
            continue
        if kind == 2:
            if out:
                out.pop()
            continue
        out.append(seg)

    while out and not out[len(out) - 1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return (b"/" + b"/".join(out)).replace(b"\\", b"%5C")


cdef object _decode_latin1(const char* s, Py_ssize_t n):
    if n <= 0:
        return ""
    return PyUnicode_DecodeLatin1(s, n, NULL)


cdef int _latin1_stack(
    object name,
    char* buf,
    Py_ssize_t cap,
    Py_ssize_t* out_n,
) except -1:
    """Write a Latin-1 ``str`` into ``buf``. No Python ``bytes`` object."""
    cdef Py_ssize_t i
    cdef Py_ssize_t n = len(name)
    cdef unsigned int ch
    if n > cap:
        raise ValueError("name is too long")
    for i in range(n):
        ch = PyUnicode_ReadChar(name, i)
        if ch > 255:
            raise UnicodeEncodeError(
                "latin-1", name, i, i + 1, "ordinal not in range(256)"
            )
        buf[i] = <char>ch
    out_n[0] = n
    return 0


cdef inline bint _is_host_ws(char c) noexcept:
    return (
        c == 32
        or c == 9
        or c == 10
        or c == 13
        or c == 11
        or c == 12
    )


cdef inline bint _span_digits(const char* s, Py_ssize_t n) noexcept:
    cdef Py_ssize_t i
    if n <= 0:
        return False
    for i in range(n):
        if <unsigned char>s[i] < 48 or <unsigned char>s[i] > 57:
            return False
    return True


cdef object _ascii_lower_latin1(const char* s, Py_ssize_t n):
    """Lowercased ASCII copy of ``s`` as a Latin-1 Unicode string."""
    cdef char stack[512]
    cdef char* buf
    cdef bint heap = False
    cdef Py_ssize_t i
    cdef unsigned char c
    cdef object out
    if n <= 0:
        return ""
    if n <= 512:
        buf = stack
    else:
        buf = <char*>malloc(<size_t>n)
        if buf == NULL:
            return ""
        heap = True
    for i in range(n):
        c = <unsigned char>s[i]
        if 65 <= c <= 90:
            c += 32
        buf[i] = <char>c
    out = PyUnicode_DecodeLatin1(buf, n, NULL)
    if heap:
        free(buf)
    return out


cdef object _host_without_port_raw(const char* s, Py_ssize_t n):
    cdef Py_ssize_t start = 0
    cdef Py_ssize_t end = n
    cdef Py_ssize_t i
    cdef Py_ssize_t bracket_end
    cdef Py_ssize_t colon
    cdef Py_ssize_t rest
    while start < end and _is_host_ws(s[start]):
        start += 1
    while end > start and _is_host_ws(s[end - 1]):
        end -= 1
    if start >= end:
        return ""

    # IPv6 literal: the port only follows a closing "]".
    if s[start] == 91:
        bracket_end = -1
        for i in range(start + 1, end):
            if s[i] == 93:
                bracket_end = i
                break
        if bracket_end < 0:
            return _ascii_lower_latin1(s + start, end - start)
        rest = bracket_end + 1
        if rest < end and (
            s[rest] != 58 or not _span_digits(s + rest + 1, end - rest - 1)
        ):
            return _ascii_lower_latin1(s + start, end - start)
        return _ascii_lower_latin1(s + start, bracket_end - start + 1)

    # host:port — strip the port only when everything after ':' is digits.
    colon = -1
    for i in range(end - 1, start - 1, -1):
        if s[i] == 58:
            colon = i
            break
    if (
        colon > start
        and _span_digits(s + colon + 1, end - colon - 1)
    ):
        return _ascii_lower_latin1(s + start, colon - start)
    return _ascii_lower_latin1(s + start, end - start)


cdef object _host_without_port_n(const char* s, Py_ssize_t n):
    """Lowercased Host without a numeric port; IPv6 literals keep brackets.

    One trailing dot is dropped: ``example.com.`` names ``example.com``.
    """
    cdef object host = _host_without_port_raw(s, n)
    cdef Py_ssize_t m = len(host)
    # ``example.com.`` (fully qualified) is the same host as ``example.com``.
    if m > 1 and host[m - 1] == "." and host[m - 2] != ".":
        return host[: m - 1]
    return host


cdef bytes _quote_path(object path):
    """Wire form of a decoded path (for ``Request(path=...)`` built by hand)."""
    return _url_quote(path or "/", safe="/!$&'()*+,;=:@~").encode("ascii")


# Python-callable wrappers (tests).
def canonical_request_path(raw):
    """``(status, target)`` for a raw origin-form path (bytes, no query).

    ``(200, None)`` routes as-is, ``(308, canonical)`` redirects, and
    ``(400, None)`` rejects. Same rules as the HTTP protocol.
    """
    cdef bytes data = bytes(raw)
    cdef int flags = scan_request_path(
        PyBytes_AS_STRING(data), PyBytes_GET_SIZE(data), False
    )
    if flags < 0:
        return 400, None
    if flags & PATH_REDIRECT:
        return 308, canonical_path(PyBytes_AS_STRING(data), PyBytes_GET_SIZE(data))
    return 200, None


def host_without_port(str host):
    """Lowercased Host value without a numeric port; IPv6 literals keep brackets.

    One trailing dot is dropped: ``example.com.`` names ``example.com``.
    """
    cdef Py_ssize_t n = 0
    cdef const char* p = PyUnicode_AsUTF8AndSize(host, &n)
    return _host_without_port_n(p, n)


def decode_request_path(raw):
    """Fully decoded ``str`` for a raw request path; ``ValueError`` if invalid."""
    cdef bytes data = bytes(raw)
    cdef object out
    if scan_request_path(PyBytes_AS_STRING(data), PyBytes_GET_SIZE(data), True) < 0:
        raise ValueError(f"invalid request path: {data[:120]!r}")
    out = decode_path_full(PyBytes_AS_STRING(data), PyBytes_GET_SIZE(data))
    if out is None:
        raise ValueError(f"invalid request path: {data[:120]!r}")
    return out
