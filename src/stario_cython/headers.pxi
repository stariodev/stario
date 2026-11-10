"""Response header list (``w.headers``). Included into ``exchange.pyx``."""

cdef enum:
    INTERN_MAX = 36
    INTERN_TABLE_SIZE = 64

cdef extern from *:
    """
    #include <Python.h>
    #include <stdint.h>

    static uint8_t stario_hdr_lower[256];
    static uint8_t stario_hdr_value_ok[256];
    static int stario_hdr_lower_ready;

    static void stario_hdr_lower_init(void) {
        static const char valid[] =
            "!#$%&'*+-.^_`|~0123456789"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz";
        const unsigned char* p;
        int i;
        if (stario_hdr_lower_ready) {
            return;
        }
        for (i = 0; i < 256; i++) {
            stario_hdr_lower[i] = 0;
            stario_hdr_value_ok[i] = (uint8_t)(i == 0x09 || (i >= 0x20 && i != 0x7F));
        }
        for (p = (const unsigned char*)valid; *p; p++) {
            unsigned char c = *p;
            stario_hdr_lower[c] = (c >= 'A' && c <= 'Z') ? (unsigned char)(c + 32) : c;
        }
        stario_hdr_lower_ready = 1;
    }

    static int stario_fold_header_name(
        PyObject* name,
        char* buf,
        Py_ssize_t cap,
        Py_ssize_t* out_n
    ) {
        Py_ssize_t i;
        Py_ssize_t n;
        int kind;
        void* data;
        stario_hdr_lower_init();
        if (!PyUnicode_Check(name)) {
            PyErr_SetString(PyExc_TypeError, "header name must be str");
            return -1;
        }
        n = PyUnicode_GET_LENGTH(name);
        if (n == 0) {
            PyErr_SetString(PyExc_ValueError, "Invalid header name: empty");
            return -1;
        }
        if (n >= cap) {
            PyErr_SetString(PyExc_ValueError, "Invalid header name: too long");
            return -1;
        }
        kind = PyUnicode_KIND(name);
        data = PyUnicode_DATA(name);
        for (i = 0; i < n; i++) {
            Py_UCS4 ch = PyUnicode_READ(kind, data, i);
            uint8_t mapped;
            if (ch > 255) {
                PyErr_Format(PyExc_ValueError, "Invalid header name: %R", name);
                return -1;
            }
            mapped = stario_hdr_lower[(uint8_t)ch];
            if (mapped == 0) {
                PyErr_Format(PyExc_ValueError, "Invalid header name: %R", name);
                return -1;
            }
            buf[i] = (char)mapped;
        }
        *out_n = n;
        return 0;
    }

    static int stario_header_value_ok(const unsigned char* p, Py_ssize_t n) {
        Py_ssize_t i;
        stario_hdr_lower_init();
        for (i = 0; i < n; i++) {
            if (!stario_hdr_value_ok[p[i]]) {
                return 0;
            }
        }
        return 1;
    }
    """
    int stario_fold_header_name(
        object name,
        char* buf,
        Py_ssize_t cap,
        Py_ssize_t* out_n,
    ) except -1
    int stario_header_value_ok(const unsigned char* p, Py_ssize_t n) noexcept
    void stario_hdr_lower_init() noexcept


# Open-addressed name→index table: 0 is empty, else the intern index + 1.
cdef const char* _INTERN_C[INTERN_MAX]
cdef Py_ssize_t _INTERN_N[INTERN_MAX]
cdef uint8_t _INTERN_SLOT[INTERN_TABLE_SIZE]
cdef list _INTERN_PY = []
cdef list _INTERN_WIRE = []
cdef int _INTERN_COUNT = 0


cdef inline uint32_t _hash_bytes(const char* src, size_t n) noexcept:
    cdef uint32_t value = <uint32_t>2166136261
    cdef size_t i
    for i in range(n):
        value = (
            value ^ <uint8_t>src[i]
        ) * <uint32_t>16777619
    return value


cdef object _make_wire_name(const char* src, size_t n):
    cdef object wire = PyBytes_FromStringAndSize(NULL, <Py_ssize_t>(n + 2))
    cdef char* dst = PyBytes_AS_STRING(wire)
    if n:
        memcpy(dst, src, n)
    dst[n] = ':'
    dst[n + 1] = ' '
    return wire


cdef void _intern_add(const char* src):
    global _INTERN_COUNT
    cdef Py_ssize_t n = 0
    cdef int index = _INTERN_COUNT
    cdef uint32_t slot
    if index >= INTERN_MAX:
        return
    while src[n] != 0:
        n += 1
    _INTERN_C[index] = src
    _INTERN_N[index] = n
    _INTERN_PY.append(PyBytes_FromStringAndSize(src, n))
    _INTERN_WIRE.append(_make_wire_name(src, <size_t>n))
    slot = _hash_bytes(src, <size_t>n) & (INTERN_TABLE_SIZE - 1)
    while _INTERN_SLOT[slot] != 0:
        slot = (slot + 1) & (INTERN_TABLE_SIZE - 1)
    _INTERN_SLOT[slot] = <uint8_t>(index + 1)
    _INTERN_COUNT = index + 1


cdef void _init_intern() noexcept:
    if _INTERN_COUNT:
        return
    _intern_add("host")
    _intern_add("connection")
    _intern_add("cache-control")
    _intern_add("sec-ch-ua")
    _intern_add("sec-ch-ua-mobile")
    _intern_add("sec-ch-ua-platform")
    _intern_add("upgrade-insecure-requests")
    _intern_add("user-agent")
    _intern_add("accept")
    _intern_add("sec-fetch-site")
    _intern_add("sec-fetch-mode")
    _intern_add("sec-fetch-user")
    _intern_add("sec-fetch-dest")
    _intern_add("accept-encoding")
    _intern_add("accept-language")
    _intern_add("cookie")
    _intern_add("referer")
    _intern_add("origin")
    _intern_add("dnt")
    _intern_add("priority")
    _intern_add("pragma")
    _intern_add("sec-gpc")
    _intern_add("x-requested-with")
    _intern_add("content-type")
    _intern_add("content-length")
    _intern_add("content-encoding")
    _intern_add("transfer-encoding")
    _intern_add("expect")
    _intern_add("vary")
    _intern_add("location")
    _intern_add("allow")
    _intern_add("last-event-id")
    _intern_add("date")
    _intern_add("set-cookie")


_init_intern()
# Fill the name and value tables at import, under the import lock: a lazy
# first use could race between threads on a free-threaded build.
stario_hdr_lower_init()


cdef int _fold_header_name(object name, char* buf, Py_ssize_t* out_n) except -1:
    return stario_fold_header_name(name, buf, <Py_ssize_t>HEADER_NAME_STACK, out_n)


cdef inline int _intern_lookup(const char* src, size_t n) noexcept:
    cdef uint32_t slot
    cdef uint8_t entry
    cdef int index
    slot = _hash_bytes(src, n) & (INTERN_TABLE_SIZE - 1)
    while True:
        entry = _INTERN_SLOT[slot]
        if entry == 0:
            return -1
        index = <int>entry - 1
        if (
            _INTERN_N[index] == <Py_ssize_t>n
            and memcmp(_INTERN_C[index], src, n) == 0
        ):
            return index
        slot = (slot + 1) & (INTERN_TABLE_SIZE - 1)


cdef object _intern_name(const char* src, size_t n):
    cdef char buf[HEADER_NAME_STACK]
    cdef int index
    if n >= HEADER_NAME_STACK:
        raise ValueError("Invalid header name: too long")
    _lower_copy(buf, src, n)
    index = _intern_lookup(buf, n)
    if index < 0:
        return PyBytes_FromStringAndSize(buf, <Py_ssize_t>n)
    return _INTERN_PY[index]


cdef object _intern_wire_name(const char* src, size_t n):
    cdef char buf[HEADER_NAME_STACK]
    cdef int index
    if n >= HEADER_NAME_STACK - 2:
        raise ValueError("Invalid header name: too long")
    _lower_copy(buf, src, n)
    index = _intern_lookup(buf, n)
    if index < 0:
        return _make_wire_name(buf, n)
    return _INTERN_WIRE[index]


# Identity-compared wire names for the headers respond() writes itself.
cdef object WIRE_DATE = _intern_wire_name("date", 4)
cdef object WIRE_CONTENT_TYPE = _intern_wire_name("content-type", 12)
cdef object WIRE_CONTENT_LENGTH = _intern_wire_name("content-length", 14)
cdef object WIRE_CONTENT_ENCODING = _intern_wire_name("content-encoding", 16)
cdef object WIRE_TRANSFER_ENCODING = _intern_wire_name("transfer-encoding", 17)


cdef object _value_line(object value):
    cdef bytes raw = <bytes>value
    cdef Py_ssize_t n = PyBytes_GET_SIZE(raw)
    cdef object out = PyBytes_FromStringAndSize(NULL, n + 2)
    cdef char* dst = PyBytes_AS_STRING(out)
    if n:
        memcpy(dst, PyBytes_AS_STRING(raw), <size_t>n)
    dst[n] = '\r'
    dst[n + 1] = '\n'
    return out


cdef object _bare_bytes(object stored, Py_ssize_t suffix):
    cdef bytes raw = <bytes>stored
    cdef Py_ssize_t n = PyBytes_GET_SIZE(raw)
    if n < suffix:
        return raw
    return PyBytes_FromStringAndSize(PyBytes_AS_STRING(raw), n - suffix)


cdef inline bint _wire_is(
    object wire,
    const char* name,
    Py_ssize_t n,
) noexcept:
    cdef bytes raw
    cdef Py_ssize_t wn
    cdef const char* ws
    raw = <bytes>wire
    wn = PyBytes_GET_SIZE(raw)
    if wn != n + 2:
        return False
    ws = PyBytes_AS_STRING(raw)
    return (
        memcmp(ws, name, <size_t>n) == 0
        and ws[n] == ':'
        and ws[n + 1] == ' '
    )


cdef object _encode_name(str name):
    cdef char buf[HEADER_NAME_STACK]
    cdef Py_ssize_t n
    stario_fold_header_name(name, buf, <Py_ssize_t>HEADER_NAME_STACK, &n)
    return _intern_name(buf, <size_t>n)


cdef object _encode_value(str value):
    cdef bytes raw
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(f"Invalid header value: {value}")
    if not stario_header_value_ok(
        <const unsigned char*>PyBytes_AS_STRING(raw),
        PyBytes_GET_SIZE(raw),
    ):
        raise ValueError(f"Invalid header value: {value}")
    return raw


cdef object _encode_value_bytes(object value):
    cdef bytes raw
    if isinstance(value, str):
        return _encode_value(value)
    if not isinstance(value, bytes):
        raise ValueError("Invalid header value")
    raw = <bytes>value
    if not stario_header_value_ok(
        <const unsigned char*>PyBytes_AS_STRING(raw),
        PyBytes_GET_SIZE(raw),
    ):
        raise ValueError("Invalid header value")
    return raw


# Common content-types. Identity hits skip the CR/LF scan; equal-but-not-
# identical bytes reuse the interned copy. [0] is the last caller object,
# [1] is the intern table (a list so the inline helper can mutate it).
cdef object CT_TEXT_PLAIN = b"text/plain"
cdef object CT_TEXT_PLAIN_UTF8 = b"text/plain; charset=utf-8"
cdef object CT_JSON = b"application/json"
cdef object CT_JSON_UTF8 = b"application/json; charset=utf-8"
cdef object CT_HTML_UTF8 = b"text/html; charset=utf-8"
cdef object CT_OCTET = b"application/octet-stream"
cdef list _CT_STATE = [
    CT_TEXT_PLAIN_UTF8,
    {
        CT_TEXT_PLAIN: CT_TEXT_PLAIN,
        CT_TEXT_PLAIN_UTF8: CT_TEXT_PLAIN_UTF8,
        CT_JSON: CT_JSON,
        CT_JSON_UTF8: CT_JSON_UTF8,
        CT_HTML_UTF8: CT_HTML_UTF8,
        CT_OCTET: CT_OCTET,
    },
]


cdef object _content_type_checked(object value):
    """Validate a Content-Type outside the common-type cache (``respond()``)."""
    try:
        return _encode_value_bytes(value)
    except ValueError as exc:
        raise StarioError(
            "Invalid Content-Type",
            context={"content_type": repr(value)[:120]},
            help_text="Content-Type must be a header value without CR or LF.",
        ) from exc


cdef inline object _content_type_bytes(object value):
    """``respond()``'s Content-Type as wire bytes; StarioError if invalid."""
    cdef object cached
    cdef dict table
    if value is _CT_STATE[0]:
        return value
    if (
        value is CT_TEXT_PLAIN_UTF8
        or value is CT_TEXT_PLAIN
        or value is CT_JSON_UTF8
        or value is CT_JSON
        or value is CT_HTML_UTF8
        or value is CT_OCTET
    ):
        _CT_STATE[0] = value
        return value
    if type(value) is bytes:
        table = <dict>_CT_STATE[1]
        cached = table.get(value)
        if cached is not None:
            _CT_STATE[0] = value
            return cached
    value = _content_type_checked(value)
    if type(value) is bytes:
        table = <dict>_CT_STATE[1]
        if len(table) < 64:
            table[value] = value
    _CT_STATE[0] = value
    return value


def encode_header_value(str value):
    """Validate and return wire bytes for a header value."""
    return _encode_value(value)


cdef inline bytes _trusted_bytes(object value):
    if type(value) is bytes:
        return <bytes>value
    if type(value) is str:
        return (<str>value).encode("latin-1")
    raise TypeError(
        f"Headers() keys and values must be str or bytes, not {type(value).__name__}"
    )


cdef object RESPOND_DATE_ERROR = (
    "Date is emitted by respond(); do not set it on w.headers."
)
cdef object RESPOND_TE_ERROR = (
    "respond() always sends Content-Length; do not set Transfer-Encoding."
)


@cython.final
cdef class Headers:
    def __init__(self, raw_header_data=None):
        cdef object key
        cdef object value
        cdef object item
        cdef bytes name
        self._names = []
        self._values = []
        self._n = 0
        if raw_header_data:
            for key, value in raw_header_data.items():
                name = _trusted_bytes(key)
                if type(value) is list:
                    for item in value:
                        self.c_add(name, _trusted_bytes(item))
                else:
                    self.c_set(name, _trusted_bytes(value))

    cdef Py_ssize_t _find_n(self, const char* name, Py_ssize_t n) noexcept:
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t i
        if n >= HEADER_NAME_STACK:
            return -1
        _lower_copy(buf, name, <size_t>n)
        for i in range(self._n):
            if _wire_is(self._names[i], buf, n):
                return i
        return -1

    cdef Py_ssize_t _compact_except(self, const char* name, Py_ssize_t n) noexcept:
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t i
        cdef Py_ssize_t w = 0
        if n >= HEADER_NAME_STACK:
            return self._n
        _lower_copy(buf, name, <size_t>n)
        for i in range(self._n):
            if _wire_is(self._names[i], buf, n):
                continue
            if w != i:
                self._names[w] = self._names[i]
                self._values[w] = self._values[i]
            w += 1
        return w

    cdef void _store_at(self, Py_ssize_t index, object wire, object line):
        cdef Py_ssize_t size = <Py_ssize_t>len(self._names)
        if index < size:
            self._names[index] = wire
            self._values[index] = line
            return
        self._names.append(wire)
        self._values.append(line)

    cdef object c_get(self, object name):
        cdef bytes key = <bytes>name
        cdef Py_ssize_t i = self._find_n(
            PyBytes_AS_STRING(key),
            PyBytes_GET_SIZE(key),
        )
        if i < 0:
            return None
        return _bare_bytes(self._values[i], 2)

    cdef void c_set(self, object name, object value):
        cdef bytes key = <bytes>name
        cdef const char* src = PyBytes_AS_STRING(key)
        cdef Py_ssize_t n = PyBytes_GET_SIZE(key)
        cdef object wire = _intern_wire_name(src, <size_t>n)
        cdef object line = _value_line(value)
        cdef Py_ssize_t w = self._compact_except(src, n)
        self._store_at(w, wire, line)
        self._n = w + 1

    cdef void c_add(self, object name, object value):
        cdef bytes key = <bytes>name
        self._store_at(
            self._n,
            _intern_wire_name(PyBytes_AS_STRING(key), <size_t>PyBytes_GET_SIZE(key)),
            _value_line(value),
        )
        self._n += 1

    cdef void c_remove(self, object name):
        cdef bytes key = <bytes>name
        self._n = self._compact_except(
            PyBytes_AS_STRING(key),
            PyBytes_GET_SIZE(key),
        )

    cdef void c_clear(self):
        self._n = 0

    cdef bint c_empty(self):
        return self._n == 0

    cdef bint c_vary_contains(self, object token):
        cdef object token_lower = token.lower()
        cdef Py_ssize_t i
        cdef object value
        cdef object part
        for i in range(self._n):
            if not _wire_is(self._names[i], "vary", 4):
                continue
            value = _bare_bytes(self._values[i], 2)
            for raw_part in value.split(b","):
                part = raw_part.strip()
                if part == b"*" or part.lower() == token_lower:
                    return True
        return False

    cdef void c_merge_vary(self, object token):
        cdef object existing = self.c_get(b"vary")
        cdef object stripped
        cdef object part
        cdef object token_lower
        cdef bint has_value
        if existing is None:
            self.c_set(b"vary", token)
            return
        stripped = existing.strip()
        if not stripped:
            self.c_set(b"vary", token)
            return
        if stripped == b"*":
            return
        token_lower = token.lower()
        has_value = False
        for raw_part in existing.split(b","):
            part = raw_part.strip()
            if not part:
                continue
            has_value = True
            if part == b"*" or part.lower() == token_lower:
                return
        if has_value:
            self.c_set(b"vary", existing.rstrip() + b", " + token)
        else:
            self.c_set(b"vary", token)

    cdef int _add_ba(
        self,
        object buf,
        Py_ssize_t* length,
        const char* src,
        Py_ssize_t n,
    ) except -1:
        cdef bytearray ba = buf
        cdef Py_ssize_t need = length[0] + n
        cdef Py_ssize_t cap = PyByteArray_GET_SIZE(ba)
        cdef Py_ssize_t next_cap
        if n <= 0:
            return 0
        if need > cap:
            next_cap = 256 if cap == 0 else cap * 2
            if next_cap < need:
                next_cap = need
            if PyByteArray_Resize(ba, next_cap) < 0:
                raise MemoryError()
        memcpy(PyByteArray_AS_STRING(ba) + length[0], src, <size_t>n)
        length[0] = need
        return 0

    cdef int _write_pair_at(
        self,
        object buf,
        Py_ssize_t* length,
        Py_ssize_t index,
    ) except -1:
        cdef bytes name = <bytes>self._names[index]
        cdef bytes value = <bytes>self._values[index]
        self._add_ba(
            buf,
            length,
            PyBytes_AS_STRING(name),
            PyBytes_GET_SIZE(name),
        )
        self._add_ba(
            buf,
            length,
            PyBytes_AS_STRING(value),
            PyBytes_GET_SIZE(value),
        )
        return 0

    cdef int c_write_wire_ba(
        self,
        object buf,
        Py_ssize_t* length,
    ) except -1:
        cdef Py_ssize_t i
        for i in range(self._n):
            self._write_pair_at(buf, length, i)
        return 0

    cdef object c_scan_respond(self, object content_type):
        """Validate ``w.headers`` against a one-shot respond; collect CE/CL.

        Identity hits on the interned wire names: anything the writer emits
        itself (Date, TE, a mismatched Content-Type) is a usage error.
        """
        cdef Py_ssize_t i
        cdef object name
        cdef object value
        cdef object existing_ce = None
        cdef object existing_cl = None
        for i in range(self._n):
            name = self._names[i]
            if name is WIRE_DATE:
                raise StarioRuntime(
                    RESPOND_DATE_ERROR,
                    help_text="The writer supplies Date on every response.",
                )
            if name is WIRE_TRANSFER_ENCODING:
                raise StarioRuntime(
                    RESPOND_TE_ERROR,
                    help_text="Use write_headers() when you need chunked encoding.",
                )
            if name is WIRE_CONTENT_TYPE:
                value = _bare_bytes(self._values[i], 2)
                if value != content_type:
                    raise StarioRuntime(
                        "Content-Type on w.headers does not match respond()",
                        context={"headers": value, "respond": content_type},
                        help_text=(
                            "Omit Content-Type on w.headers and pass it as "
                            "respond()'s content_type argument. If you set it, "
                            "it must match."
                        ),
                    )
                continue
            if name is WIRE_CONTENT_LENGTH:
                existing_cl = _bare_bytes(self._values[i], 2)
                continue
            if name is WIRE_CONTENT_ENCODING:
                existing_ce = _bare_bytes(self._values[i], 2)
        return existing_ce, existing_cl

    cdef void c_require_respond_length(
        self,
        object existing_cl,
        object expected,
    ) except *:
        if existing_cl is None:
            return
        if existing_cl != expected:
            raise StarioRuntime(
                "Content-Length on w.headers does not match respond()",
                context={"headers": existing_cl, "respond": expected},
                help_text=(
                    "Omit Content-Length on w.headers; respond() derives it from "
                    "the on-wire body (after compression). If you set it, it must match."
                ),
            )

    cdef int c_write_respond_pairs(
        self,
        object buf,
        Py_ssize_t* length,
        bint skip_ce,
    ) except -1:
        cdef Py_ssize_t i
        cdef object name
        for i in range(self._n):
            name = self._names[i]
            if (
                name is WIRE_CONTENT_TYPE
                or name is WIRE_CONTENT_LENGTH
                or (skip_ce and name is WIRE_CONTENT_ENCODING)
            ):
                continue
            self._write_pair_at(buf, length, i)
        return 0

    def add(self, str name, str value):
        self.c_add(_encode_name(name), _encode_value(value))

    def unsafe_add(self, bytes name not None, bytes value not None):
        self.c_add(name, value)

    def set(self, str name, str value):
        self.c_set(_encode_name(name), _encode_value(value))

    def unsafe_set(self, bytes name not None, bytes value not None):
        self.c_set(name, value)

    def unsafe_merge_vary(self, bytes token not None):
        """Append `token` to `Vary` unless it, or `*`, is already listed."""
        self.c_merge_vary(token)

    def setdefault(self, str name, str value):
        cdef object key = _encode_name(name)
        cdef object existing = self.c_get(key)
        cdef object encoded
        if existing is not None:
            return existing.decode("latin-1")
        encoded = _encode_value(value)
        self.c_set(key, encoded)
        return encoded.decode("latin-1")

    def get(self, str name, default=None):
        cdef object wire = self.c_get(_encode_name(name))
        if wire is None:
            return default
        return wire.decode("latin-1")

    def unsafe_get(self, bytes name not None, default=None):
        cdef object value = self.c_get(name)
        if value is None:
            return default
        return value

    def getlist(self, str name):
        return [
            value.decode("latin-1")
            for value in self.unsafe_getlist(_encode_name(name))
        ]

    def unsafe_getlist(self, bytes name not None):
        cdef bytes key = name
        cdef char buf[HEADER_NAME_STACK]
        cdef const char* src = PyBytes_AS_STRING(key)
        cdef Py_ssize_t n = PyBytes_GET_SIZE(key)
        cdef list result = []
        cdef Py_ssize_t i
        if n >= HEADER_NAME_STACK:
            return result
        _lower_copy(buf, src, <size_t>n)
        for i in range(self._n):
            if _wire_is(self._names[i], buf, n):
                result.append(_bare_bytes(self._values[i], 2))
        return result

    def remove(self, str name):
        self.c_remove(_encode_name(name))

    def unsafe_remove(self, bytes name not None):
        self.c_remove(name)

    def items(self):
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in self.unsafe_items()
        ]

    def unsafe_items(self):
        cdef list result = []
        cdef Py_ssize_t i
        for i in range(self._n):
            result.append((
                _bare_bytes(self._names[i], 2),
                _bare_bytes(self._values[i], 2),
            ))
        return result

    def __contains__(self, name):
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t n
        try:
            _fold_header_name(name, buf, &n)
        except (TypeError, ValueError):
            return False
        return self._find_n(buf, n) >= 0

    def __bool__(self):
        return self._n != 0

    def __len__(self):
        cdef set seen = set()
        cdef Py_ssize_t i
        for i in range(self._n):
            seen.add(self._names[i])
        return len(seen)

    def __repr__(self):
        return f"Headers({self.items()!r})"
