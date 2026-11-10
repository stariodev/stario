# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, freethreading_compatible=True
"""Pooled request/response state, plus the ``c`` / ``w`` given to one handler.

Socket and parsing live in ``protocol``. ``Request`` is built only if the
handler reads ``c.req``. Query and cookies stay lazy on that object.

The ``.pxi`` includes are textual: ``headers.pxi`` (response ``Headers``),
``path.pxi`` (target scan, host), ``query.pxi``, ``cookies.pxi``, and
``router.pxi`` (compiled trie). Everything shares this module's imports.
"""

cimport cython

import asyncio
import http
from urllib.parse import quote as _url_quote

from libc.stddef cimport size_t
from libc.stdint cimport int32_t, uint8_t, uint32_t, uint64_t
from libc.stdlib cimport free, malloc, realloc
from libc.stdio cimport sprintf
from libc.string cimport memcmp, memcpy, memmove
from cpython.bytearray cimport (
    PyByteArray_AS_STRING,
    PyByteArray_GET_SIZE,
    PyByteArray_Resize,
)
from cpython.bytes cimport (
    PyBytes_AS_STRING,
    PyBytes_FromStringAndSize,
    PyBytes_GET_SIZE,
)
from cpython.list cimport PyList_GET_SIZE
from cpython.buffer cimport PyBUF_SIMPLE, PyBuffer_Release, PyObject_GetBuffer
from cpython.exc cimport PyErr_Clear
from cpython.mem cimport PyMem_Free, PyMem_Malloc, PyMem_Realloc
from cpython.unicode cimport (
    PyUnicode_AsUTF8AndSize,
    PyUnicode_DecodeASCII,
    PyUnicode_DecodeLatin1,
    PyUnicode_DecodeUTF8,
    PyUnicode_ReadChar,
)

from stario.exceptions import (
    ClientDisconnected,
    RequestBodyError,
    StarioError,
    StarioRuntime,
)
from stario.http.compression import (
    DEFAULT_BROTLI_LEVEL,
    DEFAULT_GZIP_LEVEL,
    DEFAULT_MIN_SIZE,
    DEFAULT_ZSTD_LEVEL,
    content_type_is_compressible,
)
from stario.http.context import EMPTY_MATCH, _Alive
from stario.http.invoke import on_handler_done
from stario.http.redirect import normalized_location
from stario.json import dumps_bytes as _json_dumps_bytes

from stario_cython.compression_buf cimport (
    StarioBrotli,
    StarioGzip,
    StarioZstd,
    stario_brotli_block_borrowed,
    stario_brotli_acquire,
    stario_brotli_compress_borrowed,
    stario_brotli_finish_borrowed,
    stario_brotli_release,
    stario_gzip_block_borrowed,
    stario_gzip_acquire,
    stario_gzip_finish_borrowed,
    stario_gzip_release,
    stario_zstd_block_borrowed,
    stario_zstd_acquire,
    stario_zstd_finish_borrowed,
    stario_zstd_release,
)
include "headers.pxi"
include "path.pxi"
include "query.pxi"
include "cookies.pxi"
include "router.pxi"

# Lazily bound on first w.html() call — stario.markup is heavier than the
# imports above, and text/json/redirect/empty do not need it.
_markup_render = None

cdef class HttpConnection:
    """HttpConnection vtable. ``CHttpProtocol`` overrides every entry."""

    def __cinit__(self):
        self.timeout_cleanup = 0

    cdef void release_exchange(self, RequestExchange exchange):
        raise NotImplementedError("release_exchange")

    cdef void response_completed(self, RequestExchange exchange):
        raise NotImplementedError("response_completed")

    cdef void set_body_paused(self, RequestExchange exchange, bint paused):
        raise NotImplementedError("set_body_paused")

    cdef void close_lingering(self):
        raise NotImplementedError("close_lingering")

    cdef object date_line(self):
        """The loop clock's ``date: ...\\r\\n`` line for this response."""
        raise NotImplementedError("date_line")

    cdef object ensure_disconnect(self):
        raise NotImplementedError("ensure_disconnect")

    cdef object ensure_stream_disconnect(self, RequestExchange ex):
        raise NotImplementedError("ensure_stream_disconnect")

    cdef object drain_waiter(self, RequestExchange exchange):
        return None

    cdef void h2_respond(
        self,
        RequestExchange ex,
        object nva,
        object body,
        bint skip_ce=False,
        bint skip_cl=True,
        bint skip_ct=True,
    ):
        raise NotImplementedError("h2_respond")

    cdef void h2_write_headers(
        self,
        RequestExchange ex,
        object nva,
        bint skip_ce=False,
        bint skip_cl=False,
        bint skip_ct=False,
        bint skip_user=False,
    ):
        raise NotImplementedError("h2_write_headers")

    cdef void h2_write_data(self, RequestExchange ex, object data, bint end):
        raise NotImplementedError("h2_write_data")

    cdef void h2_end(self, RequestExchange ex):
        raise NotImplementedError("h2_end")

    cdef void h2_abort(self, RequestExchange ex):
        raise NotImplementedError("h2_abort")

    cdef void h2_handler_finished(self, RequestExchange ex):
        pass

cdef int LOW_WATER = 128 * 1024
cdef int HIGH_WATER = 512 * 1024
cdef int BODY_HIGH_WATER = 64 * 1024
cdef int STREAM_CHUNK_LIMIT = 1024 * 1024
cdef int STREAM_CHUNK_CL = 256 * 1024
cdef int OUTPUT_BUFFER_RETAIN_MAX = 64 * 1024
cdef int DEFAULT_STREAM_CHUNK = 64 * 1024
# zlib memLevel for streamed gzip responses; a stream holds far less input
# history than a one-shot body, so the default 8 wastes ~50 KiB per stream.
cdef int STREAM_GZIP_MEM_LEVEL = 4
# Stream encoders cap brotli at quality 3: q4+ holds ~0.7 MiB of fixed
# tables per live stream for no wire-size gain on chunked writes.
cdef int STREAM_BROTLI_LEVEL_MAX = 3
cdef int ZSTD_WINDOW_LOG_MIN = 10
# Same-OS-thread spare. Keep-alive reuse is the connection idle slot.
cdef int POOL_MAX = 16
cdef int REQUEST_NAME_MAX = 256
cdef int REQUEST_ARENA_RETAIN_MAX = 8 * 1024
cdef int REQUEST_HEADERS_RETAIN_MAX = 64

cdef object BODY_TYPE_ERROR = (
    "body must be bytes-like or a list/tuple of bytes-like objects"
)

cdef int CONSUMED_NONE = 0
cdef int CONSUMED_BODY = 1
cdef int CONSUMED_STREAM = 2

cdef int ENCODING_NONE = 0
cdef int ENCODING_BR = 1
cdef int ENCODING_ZSTD = 2
cdef int ENCODING_GZIP = 3

cdef bytes CT_PREFIX = b"content-type: "
cdef bytes CE_PREFIX = b"content-encoding: "
cdef bytes VARY_PREFIX = b"vary: "
cdef bytes CL_HEADER = b"content-length: "
cdef bytes CL_PREFIX = b"\r\ncontent-length: "
cdef bytes CRLF2 = b"\r\n\r\n"
cdef bytes CRLF = b"\r\n"
cdef bytes CHUNK_END = b"0\r\n\r\n"
cdef tuple _DEC_SMALL = tuple(str(i).encode("ascii") for i in range(1024))

cdef object STARTED_ERROR = (
    "Response already started (headers sent). "
    "Set headers via w.headers.set() before the first write or one-shot respond()."
)

cdef extern from *:
    """
    #include <Python.h>
    #if defined(_MSC_VER)
    #define STARIO_TLS __declspec(thread)
    #else
    #define STARIO_TLS _Thread_local
    #endif
    /* Recycle uniqueness and the test counter are per OS thread. */
    static STARIO_TLS Py_ssize_t stario_retained_detaches = 0;

    static void stario_note_retained(void) {
        stario_retained_detaches += 1;
    }

    static Py_ssize_t stario_retained_count(void) {
        return stario_retained_detaches;
    }

    static int stario_is_unique(PyObject* obj) {
    #if PY_VERSION_HEX >= 0x030e00a0
        return PyUnstable_Object_IsUniquelyReferenced(obj);
    #else
        return Py_REFCNT(obj) == 1;
    #endif
    }

    /* Per-OS-thread exchange spare list. The pointer keeps its list alive
       until process exit, like threading.local but without a getattr on
       every acquire and release. */
    static STARIO_TLS PyObject* stario_exchange_pool = NULL;

    static PyObject* stario_pool_get(void) {
        PyObject* pool = stario_exchange_pool;
        if (pool == NULL) {
            Py_INCREF(Py_None);
            return Py_None;
        }
        Py_INCREF(pool);
        return pool;
    }

    static void stario_pool_set(PyObject* pool) {
        if (stario_exchange_pool == NULL) {
            Py_INCREF(pool);
            stario_exchange_pool = pool;
        }
    }
    """
    void stario_note_retained() noexcept
    Py_ssize_t stario_retained_count() noexcept
    bint stario_is_unique(object obj) noexcept
    object stario_pool_get()
    void stario_pool_set(object pool) noexcept


def _retained_detach_count():
    return stario_retained_count()


cdef inline list _thread_pool():
    """Same-OS-thread spare list. Never share an exchange across threads."""
    cdef object pool = stario_pool_get()
    if pool is None:
        pool = []
        stario_pool_set(pool)
    return <list>pool


cdef inline Py_ssize_t _real_len(object obj) except -1:
    """Length of the bytes a C pointer to `obj` covers.

    `len()` would call a subclass's `__len__`; a wrong answer there would
    read past the object or misstate Content-Length.
    """
    if isinstance(obj, bytes):
        return PyBytes_GET_SIZE(obj)
    if isinstance(obj, bytearray):
        return PyByteArray_GET_SIZE(obj)
    return len(obj)


cdef inline bint _is_bytes_like(object obj) except -1:
    if isinstance(obj, (bytes, bytearray)):
        return True
    if isinstance(obj, memoryview):
        # Lengths are counted with len() and written as one flat buffer.
        if (
            (<memoryview>obj).itemsize != 1
            or (<memoryview>obj).ndim != 1
            or not (<memoryview>obj).c_contiguous
        ):
            raise TypeError(
                "memoryview body parts must be flat, contiguous bytes; "
                "use bytes(view) or view.cast('B')"
            )
        return True
    return False


cdef inline void _require_bytes_like(object part):
    if not _is_bytes_like(part):
        raise TypeError(
            "body parts must be bytes-like, got " + type(part).__name__
        )


cdef inline bint _token_equals(
    const char* value,
    size_t start,
    size_t end,
    const char* token,
    size_t token_length,
) noexcept:
    """``value[start:end]`` equals ``token``, ASCII case-insensitive."""
    cdef size_t i
    cdef size_t length = end - start
    cdef unsigned char c
    if length != token_length:
        return False
    for i in range(length):
        c = <unsigned char>value[start + i]
        if 65 <= c <= 90:
            c += 32
        if c != <unsigned char>token[i]:
            return False
    return True


cdef Py_ssize_t _parse_content_length(const char* s, size_t n) noexcept:
    cdef uint64_t value = 0
    cdef size_t i
    cdef unsigned int digit
    if n == 0:
        return -1
    for i in range(n):
        digit = <unsigned int>(<unsigned char>s[i] - 48)
        if digit > 9:
            return -1
        if value > (<uint64_t>9223372036854775807 - digit) / 10:
            return -1
        value = value * 10 + digit
    return <Py_ssize_t>value


cdef bint _contains_token(
    const char* value,
    size_t length,
    const char* token,
    size_t token_length,
) noexcept:
    cdef size_t start = 0
    cdef size_t end
    while start < length:
        # Comma-separated list: skip separators and surrounding OWS.
        while start < length and (
            value[start] == <char>32
            or value[start] == <char>9
            or value[start] == <char>44
        ):
            start += 1
        end = start
        while end < length and value[end] != <char>44:
            end += 1
        while end > start and (
            value[end - 1] == <char>32 or value[end - 1] == <char>9
        ):
            end -= 1
        if _token_equals(value, start, end, token, token_length):
            return True
        start = end + 1
    return False


cdef int _parse_qvalue(
    const char* value,
    size_t start,
    size_t end,
) noexcept:
    """Accept-Encoding qvalue, milli-scaled (``q=1`` is 1000). 0 if malformed."""
    cdef int q = 0
    cdef int digits = 0
    cdef unsigned char ch
    while start < end and (
        value[start] == <char>32 or value[start] == <char>9
    ):
        start += 1
    while end > start and (
        value[end - 1] == <char>32 or value[end - 1] == <char>9
    ):
        end -= 1
    if start >= end:
        return 0

    # "1" or "1.000..0" — the only forms above 0.
    if value[start] == <char>49:
        start += 1
        if start == end:
            return 1000
        if value[start] != <char>46:
            return 0
        start += 1
        while start < end:
            if value[start] != <char>48:
                return 0
            start += 1
        return 1000

    if value[start] != <char>48:
        return 0
    start += 1
    if start == end:
        return 0
    if value[start] != <char>46:
        return 0
    start += 1
    while start < end and digits < 3:
        ch = <unsigned char>value[start]
        if ch < 48 or ch > 57:
            return 0
        q = q * 10 + ch - 48
        digits += 1
        start += 1
    if start != end:
        return 0
    while digits < 3:
        q *= 10
        digits += 1
    return q


cdef inline object _encoding_wire(int enc) noexcept:
    if enc == ENCODING_BR:
        return b"br"
    if enc == ENCODING_ZSTD:
        return b"zstd"
    if enc == ENCODING_GZIP:
        return b"gzip"
    return None


cdef inline bint _may_have_body(int status) noexcept:
    if status == 204 or status == 304:
        return False
    return not (100 <= status < 200)


cdef inline int _require_final_status(int status) except -1:
    # RFC 9110 §15: final responses are 200-599 (1xx is interim, >599 invalid).
    if status < 200 or status > 599:
        raise StarioError(
            "Invalid response status",
            context={"status": status},
            help_text="Send a final status between 200 and 599.",
        )
    return 0


cdef Py_ssize_t _parse_declared_length(object raw) except -1:
    cdef object parsed
    try:
        # Digits only, as the wire grammar: int() would also take "5_0",
        # "+50", and padded forms, and a peer may read those differently.
        if not isinstance(raw, bytes) or not (<bytes>raw).isdigit():
            raise ValueError()
        parsed = int(raw)
        return parsed
    except (TypeError, ValueError, OverflowError) as exc:
        raise StarioError(
            "Invalid Content-Length header",
            context={"content-length": raw},
            help_text="Set Content-Length to a non-negative integer before write_headers().",
        ) from exc


cdef list _build_status_lines():
    cdef list lines = [None] * 600
    cdef int status
    for status in range(100, 600):
        try:
            phrase = http.HTTPStatus(status).phrase.encode("ascii")
        except ValueError:
            phrase = b""
        lines[status] = b"HTTP/1.1 %d %s\r\n" % (status, phrase)
    return lines


cdef list _STATUS_LINES = _build_status_lines()


cdef object _status_line(int status):
    if 100 <= status < 600:
        return _STATUS_LINES[status]
    return b"HTTP/1.1 %d \r\n" % status


cdef object _dec(size_t n):
    cdef char buf[16]
    cdef int i
    if n < 1024:
        return _DEC_SMALL[n]
    i = sprintf(buf, "%zu", n)
    return PyBytes_FromStringAndSize(buf, i)


cdef object _chunk_header(Py_ssize_t n):
    """``<hex>\\r\\n`` for one chunked frame."""
    cdef char buf[26]
    cdef int i = sprintf(buf, "%zx\r\n", <size_t>n)
    return PyBytes_FromStringAndSize(buf, i)


cdef inline object _wire_part(object part):
    """``bytes`` stays zero-copy; a mutable body part is snapshotted once."""
    if type(part) is bytes:
        return part
    _require_bytes_like(part)
    if isinstance(part, bytes):
        # A subclass: copy its storage, not what its __bytes__ returns.
        return PyBytes_FromStringAndSize(PyBytes_AS_STRING(part), PyBytes_GET_SIZE(part))
    if isinstance(part, bytearray):
        return PyBytes_FromStringAndSize(
            PyByteArray_AS_STRING(part), PyByteArray_GET_SIZE(part)
        )
    return bytes(part)


cdef void _raise_readonly_request_headers() except *:
    raise StarioRuntime(
        "Request headers are read-only",
        help_text=(
            "Read incoming headers from req.headers. "
            "Set outgoing headers on the Writer (w.headers)."
        ),
    )


cdef object _BODY_GONE = object()
cdef object BODY_GONE_ERROR = (
    "Request body is no longer available after its handler finished."
)


cdef void wake_waiters(list waiters) noexcept:
    """Resolve pending drain futures; skip ones a cancelled task left behind."""
    cdef object fut
    if not waiters:
        return
    for fut in waiters:
        try:
            if not fut.done():
                fut.set_result(None)
        except asyncio.InvalidStateError:
            pass
    waiters.clear()


cdef bint _connection_gone(object connection) except -1:
    cdef object future
    if connection is None or connection.closed:
        return True
    future = connection.disconnect
    return future is not None and future.done()


cdef inline Py_ssize_t _limit_for(object max_size, Py_ssize_t default) except -1:
    """Limit of one body read: `max_size`, else `default`; at most BODY_LIMIT_MAX."""
    if max_size is None:
        return default
    if max_size < 0:
        raise ValueError("max_size must be non-negative.")
    if max_size >= BODY_LIMIT_MAX:
        return BODY_LIMIT_MAX
    return <Py_ssize_t>max_size


@cython.final
@cython.freelist(64)
cdef class Request:
    """One HTTP request. New object per dispatch; not pooled with the exchange.

    Headers, query, and host read the exchange arena while the handler runs.
    If the Request (or its headers / query / cookies) is still referenced when
    the exchange is recycled, it takes a private copy, so it stays valid after
    the handler returns. The body is only readable while the handler runs,
    unless it was already read.
    """

    def __cinit__(self):
        self.method = None
        self.path = None
        self.headers = None
        self.protocol_version = None
        self.keep_alive = True
        self._query_bytes = None
        self._raw_path = None
        self._view = None
        self._p_off = 0
        self._p_len = 0
        self._q_off = 0
        self._q_len = 0
        self._body = None
        self._body_limit = BODY_LIMIT_MAX
        self._query = None
        self._cookies = None
        self._host = None

    def __init__(
        self,
        *,
        method="GET",
        path="/",
        query_bytes=b"",
        protocol_version="1.1",
        keep_alive=True,
        headers=None,
        body=None,
        raw_path=None,
    ):
        self.method = method
        self.path = path
        self.headers = (
            <RequestHeaders>headers
            if isinstance(headers, RequestHeaders)
            else RequestHeaders(headers)
        )
        self.protocol_version = protocol_version
        self.keep_alive = keep_alive
        self._query_bytes = query_bytes
        self._raw_path = (
            _quote_path(path) if raw_path is None else bytes(raw_path)
        )
        self._view = None
        self._q_off = 0
        self._q_len = 0
        self._body = body
        self._query = None
        self._cookies = None
        self._host = None

    cdef void detach_body(self, RequestExchange exchange) noexcept:
        """Called when the exchange is recycled while this Request is still held."""
        cdef object cached
        if self._body is not exchange:
            return
        cached = exchange._cached
        if (
            type(cached) is bytes
            and not exchange._discard_body
            and exchange._consumed_as != CONSUMED_STREAM
            and exchange._abort_reason == ABORT_NONE
        ):
            self._body = cached
            # A body the handler already read was accepted under its own
            # limit; only an unread body keeps the server default.
            self._body_limit = (
                exchange._max_size
                if exchange._consumed_as == CONSUMED_NONE
                else BODY_LIMIT_MAX
            )
        else:
            self._body = _BODY_GONE

    cdef void prefetch_host(self) noexcept:
        cdef RequestHeaders view = self.headers
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef const char* arena
        if self._host is not None:
            return
        index = view._host_index()
        arena = view._arena()
        if index < 0 or arena == NULL:
            self._host = ""
            return
        header = &view._headers()[index]
        self._host = _host_without_port_n(
            arena + header.value_offset,
            <Py_ssize_t>header.value_length,
        )

    cdef object _materialize_query(self):
        cdef const char* arena
        if self._query_bytes is not None:
            return self._query_bytes
        if self._q_len <= 0 or self._view is None:
            self._query_bytes = b""
            return self._query_bytes
        arena = self._view._arena()
        if arena == NULL or self._q_off + self._q_len > self._view._arena_len():
            self._query_bytes = b""
            return self._query_bytes
        self._query_bytes = PyBytes_FromStringAndSize(arena + self._q_off, self._q_len)
        return self._query_bytes

    cdef object _ensure_query(self):
        cdef ParsedQuery parsed
        if self._query is not None:
            return self._query
        parsed = ParsedQuery.__new__(ParsedQuery)
        if self._q_len > 0 and self._view is not None:
            parsed.bind_span(self._view, self._q_off, self._q_len)
        else:
            parsed.bind_bytes(self._materialize_query())
        self._query = parsed
        return parsed

    @property
    def raw_path(self):
        """Path bytes exactly as sent (still percent-encoded, no query)."""
        cdef const char* arena
        if self._raw_path is not None:
            return self._raw_path
        if self._view is None:
            return b"/"
        arena = self._view._arena()
        if self._p_len <= 0 or arena == NULL or self._p_off + self._p_len > self._view._arena_len():
            self._raw_path = b"/"
        else:
            self._raw_path = PyBytes_FromStringAndSize(arena + self._p_off, self._p_len)
        return self._raw_path

    @property
    def query_bytes(self):
        return self._materialize_query()

    @property
    def host(self):
        if self._host is None:
            self.prefetch_host()
        return self._host

    @property
    def query(self):
        return self._ensure_query()

    @property
    def cookies(self):
        cdef ParsedCookies parsed
        if self._cookies is None:
            parsed = ParsedCookies.__new__(ParsedCookies)
            parsed.bind_request_headers(self.headers)
            self._cookies = parsed
        return self._cookies

    async def body(self, max_size=None):
        cdef object data
        cdef Py_ssize_t limit = _limit_for(max_size, self._body_limit)
        if self._body is None:
            return b""
        if type(self._body) is bytes:
            if PyBytes_GET_SIZE(self._body) > limit:
                raise RequestBodyError(413, "Request body too large")
            return self._body
        if self._body is _BODY_GONE:
            raise StarioRuntime(BODY_GONE_ERROR)
        data = await self._body.read(max_size=max_size)
        return data

    async def stream(self, max_chunk=None, max_size=None):
        cdef Py_ssize_t limit = _limit_for(max_size, self._body_limit)
        if self._body is None:
            return
        if type(self._body) is bytes:
            if PyBytes_GET_SIZE(self._body) > limit:
                raise RequestBodyError(413, "Request body too large")
            yield self._body
            return
        if self._body is _BODY_GONE:
            raise StarioRuntime(BODY_GONE_ERROR)
        async for chunk in self._body.stream(max_chunk=max_chunk, max_size=max_size):
            yield chunk


cdef Request make_request(
    object method,
    object path,
    object protocol_version,
    bint keep_alive,
    RequestHeaders headers,
    object body,
):
    """Build a Request without running ``__init__``.

    Query, cookies, and host stay unset. Handlers that never read them
    never allocate those objects.
    """
    cdef Request req = Request.__new__(Request)
    req.method = method
    req.path = path
    req.headers = headers
    req.protocol_version = protocol_version
    req.keep_alive = keep_alive
    req._body = body
    return req


@cython.final
cdef class RequestExchange:
    """Pooled per-request state: parse arena, body, writer, HTTP/2 stream."""

    def __cinit__(self):
        self._req_arena = NULL
        self._req_raw_headers = NULL
        self._req_url_offset = 0
        self._req_url_length = 0
        self._req_host_index = -1
        self._req_cookie_index = -1
        self._req_authorization_index = -1
        self._brotli = NULL
        self._zstd = NULL
        self._gzip = NULL
        self._zstd_1s = NULL
        self._gzip_1s = NULL
        self._out_buf = None
        self._out_len = 0
        self._status_code = -1
        self._declared_length = -1
        self._bytes_written = 0
        self._completed = False
        self._body_buf = None
        self._body_used = 0
        self._body_cap = 0
        self._expected_size = -1
        self._http2 = False
        self._h2_stream_id = 0
        self._h2_pending = b""
        self._h2_pending_off = 0
        self._h2_body_done = False
        self._h2_method = None
        self._h2_got_method = False
        self._h2_got_path = False
        self._h2_got_authority = False
        self._h2_dispatched = False
        self._h2_headers_done = False
        self._h2_headers_sent = False
        self._h2_headers_too_large = False
        self._h2_head_bytes = 0
        self._h2_awaiting_headers = False
        self._h2_header_deadline = 0.0
        self._h2_sent_total = 0
        self._h2_send_mark = 0
        self._h2_send_deadline = 0.0
        self._h2_body_mark = -1
        self._stream_disc = None
        self._h2_date_line = None
        self._h2_date_bare = None
        self._req_content_length = -1

    def __init__(self):
        self.headers = Headers()
        self._drain_waiters = []
        self._req_view = None
        self._handle = None
        self._spare_view = None
        self._spare_handle = None
        self._clear_request_binding()
        self._cached = None
        self._data_ready = None
        self._stall_deadline = 0.0
        self._stall_touch = 0
        self._stall_seen = 0
        self._compression = None
        self._brotli_enabled = False
        self._zstd_enabled = False
        self._gzip_enabled = False
        self._s_brotli_level = DEFAULT_BROTLI_LEVEL
        self._s_brotli_window = 0
        self._s_brotli_lgblock = 0
        self._s_zstd_window = 0
        self._s_zstd_hash = 0
        self._s_zstd_chain = 0
        self._s_gzip_window = 15
        self._s_gzip_mem = 0
        self._enc_order[0] = ENCODING_ZSTD
        self._enc_order[1] = ENCODING_BR
        self._enc_order[2] = ENCODING_GZIP
        self._compress_min_size = DEFAULT_MIN_SIZE
        self.in_pool = False
        self._body_active = False
        self._discard_body = False
        self._read_max_size = -1
        self._stream_max_chunk = DEFAULT_STREAM_CHUNK

    def __dealloc__(self):
        self._free_compressors()
        if self._req_arena != NULL:
            free(self._req_arena)
            self._req_arena = NULL
        if self._req_raw_headers != NULL:
            free(self._req_raw_headers)
            self._req_raw_headers = NULL

    cdef void reset_response(self, int encoding):
        self._free_compressors()
        self._req_encoding = encoding
        self._status_code = -1
        self._declared_length = -1
        self._bytes_written = 0
        self._completed = False
        self._close_delimited = False
        self._out_len = 0
        self.headers.c_clear()

    cdef void _apply_compression(self, object compression):
        cdef object window
        if compression is None:
            self._brotli_level = DEFAULT_BROTLI_LEVEL
            self._brotli_window = 0
            self._zstd_level = DEFAULT_ZSTD_LEVEL
            self._zstd_window = 0
            self._gzip_level = DEFAULT_GZIP_LEVEL
            self._gzip_window = 15
            self._brotli_enabled = False
            self._zstd_enabled = False
            self._gzip_enabled = False
            self._enc_order[0] = ENCODING_ZSTD
            self._enc_order[1] = ENCODING_BR
            self._enc_order[2] = ENCODING_GZIP
            self._compress_min_size = DEFAULT_MIN_SIZE
            return
        # Snapshot CompressionConfig so hot paths skip Python attribute access.
        self._brotli_level = compression.brotli_level
        window = compression.brotli_window_log
        self._brotli_window = 0 if window is None else window
        self._zstd_level = compression.zstd_level
        window = compression.zstd_window_log
        self._zstd_window = 0 if window is None else window
        self._gzip_level = compression.gzip_level
        window = compression.gzip_window_bits
        self._gzip_window = 15 if window is None else window
        # Streamed responses get their own encoder profile: a smaller rolling
        # window keeps a live compressed write() from pinning codec tables.
        # None means streams reuse the one-shot parameters exactly.
        window = compression.stream_window_log
        if window is None:
            self._s_brotli_level = self._brotli_level
            self._s_brotli_window = self._brotli_window
            self._s_brotli_lgblock = 0
            self._s_zstd_window = self._zstd_window
            self._s_zstd_hash = 0
            self._s_zstd_chain = 0
            self._s_gzip_window = self._gzip_window
            self._s_gzip_mem = 0
        else:
            stream_window = window
            # Brotli quality 4+ allocates ~0.7 MiB of fixed tables per
            # encoder; quality 3 keeps the same wire size on streamed events.
            self._s_brotli_level = (
                self._brotli_level
                if self._brotli_level < STREAM_BROTLI_LEVEL_MAX
                else STREAM_BROTLI_LEVEL_MAX
            )
            self._s_brotli_window = stream_window
            self._s_brotli_lgblock = 0
            # The zstd stream buffers dwarf its window, so the window sits
            # below stream_window_log to keep a stream near 150 KiB.
            self._s_zstd_window = (
                stream_window - 3
                if stream_window - 3 > ZSTD_WINDOW_LOG_MIN
                else ZSTD_WINDOW_LOG_MIN
            )
            self._s_zstd_hash = (
                self._s_zstd_window - 3
                if self._s_zstd_window - 3 > 6
                else 6
            )
            self._s_zstd_chain = self._s_zstd_hash
            self._s_gzip_window = (
                stream_window - 2
                if 9 <= stream_window - 2 <= 15
                else (15 if stream_window - 2 > 15 else 9)
            )
            self._s_gzip_mem = STREAM_GZIP_MEM_LEVEL
        self._compress_min_size = compression.min_size
        self._brotli_enabled = self._brotli_level >= 0
        self._zstd_enabled = self._zstd_level >= 0
        self._gzip_enabled = self._gzip_level >= 0
        preference = compression.preference
        for i in range(3):
            name = preference[i]
            if name == b"br":
                self._enc_order[i] = ENCODING_BR
            elif name == b"gzip":
                self._enc_order[i] = ENCODING_GZIP
            else:
                self._enc_order[i] = ENCODING_ZSTD

    cdef inline int _ensure_brotli(self) except -1:
        if self._brotli != NULL:
            return 0
        self._brotli = stario_brotli_acquire(
            self._s_brotli_level,
            self._s_brotli_window,
            self._s_brotli_lgblock,
        )
        if self._brotli == NULL:
            raise StarioError("brotli stream init failed")
        return 0

    cdef inline int _ensure_zstd(self) except -1:
        if self._zstd != NULL:
            return 0
        self._zstd = stario_zstd_acquire(
            self._zstd_level,
            self._s_zstd_window,
            self._s_zstd_hash,
            self._s_zstd_chain,
        )
        if self._zstd == NULL:
            raise StarioError("zstd stream init failed")
        return 0

    cdef inline int _ensure_gzip(self) except -1:
        if self._gzip != NULL:
            return 0
        self._gzip = stario_gzip_acquire(
            self._gzip_level,
            self._s_gzip_window,
            self._s_gzip_mem,
        )
        if self._gzip == NULL:
            raise StarioError("gzip stream init failed")
        return 0

    cdef inline int _ensure_zstd_1s(self) except -1:
        """One-shot profile: respond() output must not change with stream_window_log."""
        if self._zstd_1s != NULL:
            return 0
        self._zstd_1s = stario_zstd_acquire(
            self._zstd_level,
            self._zstd_window,
            0,
            0,
        )
        if self._zstd_1s == NULL:
            raise StarioError("zstd stream init failed")
        return 0

    cdef inline int _ensure_gzip_1s(self) except -1:
        if self._gzip_1s != NULL:
            return 0
        self._gzip_1s = stario_gzip_acquire(
            self._gzip_level,
            self._gzip_window,
            0,
        )
        if self._gzip_1s == NULL:
            raise StarioError("gzip stream init failed")
        return 0

    cdef void _free_compressors(self):
        if self._brotli != NULL:
            stario_brotli_release(self._brotli)
            self._brotli = NULL
        if self._zstd != NULL:
            stario_zstd_release(self._zstd)
            self._zstd = NULL
        if self._gzip != NULL:
            stario_gzip_release(self._gzip)
            self._gzip = NULL
        if self._zstd_1s != NULL:
            stario_zstd_release(self._zstd_1s)
            self._zstd_1s = NULL
        if self._gzip_1s != NULL:
            stario_gzip_release(self._gzip_1s)
            self._gzip_1s = NULL

    cdef int _buf_add(self, const char* src, Py_ssize_t n) except -1:
        cdef bytearray buf
        cdef Py_ssize_t need
        cdef Py_ssize_t cap
        cdef Py_ssize_t next_cap
        if n <= 0:
            return 0
        if self._out_buf is None:
            self._out_buf = bytearray(256)
        buf = self._out_buf
        need = self._out_len + n
        cap = PyByteArray_GET_SIZE(buf)
        if need > cap:
            next_cap = cap * 2 if cap else 256
            if next_cap < need:
                next_cap = need
            if PyByteArray_Resize(buf, next_cap) < 0:
                raise MemoryError()
        memcpy(PyByteArray_AS_STRING(buf) + self._out_len, src, <size_t>n)
        self._out_len = need
        return 0

    cdef int _buf_bytes(self, object data) except -1:
        cdef Py_ssize_t n = _real_len(data)
        cdef const char* p
        cdef Py_buffer view
        if n == 0:
            return 0
        if isinstance(data, memoryview):
            PyObject_GetBuffer(data, &view, PyBUF_SIMPLE)
            try:
                return self._buf_add(<const char*>view.buf, view.len)
            finally:
                PyBuffer_Release(&view)
        p = data
        return self._buf_add(p, n)

    cdef int _buf_uint(self, size_t n, int base) except -1:
        cdef char tmp[24]
        cdef int i
        if base == 16:
            i = sprintf(tmp, "%zx", n)
        else:
            i = sprintf(tmp, "%zu", n)
        return self._buf_add(tmp, i)

    cdef void _flush(self):
        cdef object payload
        if self._out_len == 0:
            return
        payload = PyBytes_FromStringAndSize(
            PyByteArray_AS_STRING(self._out_buf),
            self._out_len,
        )
        self._out_len = 0
        if self._http2:
            self._connection.h2_write_data(self, payload, False)
            return
        self._transport.write(payload)

    cdef int _reserve_request_arena(
        self,
        Py_ssize_t bytes_needed,
    ) noexcept:
        cdef Py_ssize_t needed = self._req_arena_len + bytes_needed
        cdef Py_ssize_t cap
        cdef char* arena
        if needed <= self._req_arena_cap:
            return 0
        cap = 256 if self._req_arena_cap == 0 else self._req_arena_cap * 2
        if cap < needed:
            cap = needed
        arena = <char*>realloc(self._req_arena, <size_t>cap)
        if arena == NULL:
            return -1
        self._req_arena = arena
        self._req_arena_cap = cap
        return 0

    cdef int _reserve_request_headers(self) noexcept:
        cdef Py_ssize_t cap
        cdef RawHeader* headers
        if self._req_raw_count < self._req_raw_headers_cap:
            return 0
        cap = (
            16
            if self._req_raw_headers_cap == 0
            else self._req_raw_headers_cap * 2
        )
        headers = <RawHeader*>realloc(
            self._req_raw_headers,
            <size_t>cap * sizeof(RawHeader),
        )
        if headers == NULL:
            return -1
        self._req_raw_headers = headers
        self._req_raw_headers_cap = cap
        return 0

    cdef int append_request_url(self, const char* data, size_t length) noexcept:
        if self._req_url_length == 0:
            self._req_url_offset = self._req_arena_len
        if self._reserve_request_arena(<Py_ssize_t>length) != 0:
            return -1
        if length:
            memcpy(self._req_arena + self._req_arena_len, data, length)
        self._req_arena_len += <Py_ssize_t>length
        self._req_url_length += <Py_ssize_t>length
        return 0

    cdef int append_request_header_name(
        self,
        const char* data,
        size_t length,
    ) noexcept:
        if not self._req_pending_header:
            self._req_pending_header = True
            self._req_pending_name_offset = self._req_arena_len
            self._req_pending_name_length = 0
            self._req_pending_value_offset = -1
            self._req_pending_value_length = 0
        if self._req_pending_value_offset >= 0:
            return -1
        if self._req_pending_name_length + <Py_ssize_t>length >= REQUEST_NAME_MAX:
            return -1
        if self._reserve_request_arena(<Py_ssize_t>length) != 0:
            return -1
        _lower_copy(self._req_arena + self._req_arena_len, data, length)
        self._req_arena_len += <Py_ssize_t>length
        self._req_pending_name_length += <Py_ssize_t>length
        return 0

    cdef int append_request_header_value(
        self,
        const char* data,
        size_t length,
    ) noexcept:
        if not self._req_pending_header:
            return -1
        if self._req_pending_value_offset < 0:
            self._req_pending_value_offset = self._req_arena_len
        if self._reserve_request_arena(<Py_ssize_t>length) != 0:
            return -1
        if length:
            memcpy(self._req_arena + self._req_arena_len, data, length)
        self._req_arena_len += <Py_ssize_t>length
        self._req_pending_value_length += <Py_ssize_t>length
        return 0

    cdef int append_request_header(
        self,
        const char* name,
        size_t name_length,
        const char* value,
        size_t value_length,
        bint names_already_lower,
    ) noexcept:
        """Complete header in one pass (H2 delivers name and value together)."""
        if name_length == 0 or name_length >= <size_t>REQUEST_NAME_MAX:
            return -1
        if self._req_pending_header and self.finish_request_header() != 0:
            return -1
        if self._reserve_request_arena(<Py_ssize_t>(name_length + value_length)) != 0:
            return -1
        self._req_pending_name_offset = self._req_arena_len
        if names_already_lower:
            memcpy(self._req_arena + self._req_arena_len, name, name_length)
        else:
            _lower_copy(self._req_arena + self._req_arena_len, name, name_length)
        self._req_arena_len += <Py_ssize_t>name_length
        self._req_pending_name_length = <Py_ssize_t>name_length
        self._req_pending_value_offset = self._req_arena_len
        if value_length:
            memcpy(self._req_arena + self._req_arena_len, value, value_length)
        self._req_arena_len += <Py_ssize_t>value_length
        self._req_pending_value_length = <Py_ssize_t>value_length
        self._req_pending_header = True
        return self._commit_request_header()

    cdef bint header_value_equals(
        self,
        Py_ssize_t index,
        const char* value,
        size_t n,
    ) noexcept:
        cdef RawHeader* header
        if index < 0 or index >= self._req_raw_count:
            return False
        header = &self._req_raw_headers[index]
        if <size_t>header.value_length != n:
            return False
        if n == 0:
            return True
        return memcmp(
            self._req_arena + header.value_offset,
            value,
            n,
        ) == 0

    cdef int finish_request_header(self) noexcept:
        if not self._req_pending_header:
            return 0
        if self._req_pending_name_length == 0:
            return -1
        if self._req_pending_value_offset < 0:
            self._req_pending_value_offset = self._req_arena_len
        return self._commit_request_header()

    cdef int _commit_request_header(self) noexcept:
        cdef RawHeader* header
        cdef const char* name
        cdef const char* value
        cdef size_t name_length
        cdef size_t value_length
        cdef Py_ssize_t parsed
        name = self._req_arena + self._req_pending_name_offset
        value = self._req_arena + self._req_pending_value_offset
        name_length = <size_t>self._req_pending_name_length
        value_length = <size_t>self._req_pending_value_length
        if self._reserve_request_headers() != 0:
            return -1
        header = &self._req_raw_headers[self._req_raw_count]
        header.name_offset = <uint32_t>self._req_pending_name_offset
        header.name_length = <uint32_t>name_length
        header.value_offset = <uint32_t>self._req_pending_value_offset
        header.value_length = <uint32_t>value_length

        # Hot names carry side effects beyond the table row: indexes let
        # host/cookie/auth lookups skip a scan, and a few values steer
        # dispatch (close, expect, content-length, accept-encoding).
        if name_length == 4 and memcmp(name, "host", 4) == 0:
            if not host_value_ok(value, value_length):
                return -1
            if self._req_host_index >= 0:
                return -1
            self._req_host_index = self._req_raw_count
        elif name_length == 6:
            if memcmp(name, "cookie", 6) == 0:
                if self._req_cookie_index < 0:
                    self._req_cookie_index = self._req_raw_count
            elif memcmp(name, "expect", 6) == 0:
                if _contains_token(value, value_length, "100-continue", 12):
                    self._req_expect_continue = True
        elif name_length == 10 and memcmp(name, "connection", 10) == 0:
            if _contains_token(value, value_length, "close", 5):
                self._req_connection_close = True
        elif name_length == 13 and memcmp(name, "authorization", 13) == 0:
            if self._req_authorization_index < 0:
                self._req_authorization_index = self._req_raw_count
        elif name_length == 14 and memcmp(name, "content-length", 14) == 0:
            parsed = _parse_content_length(value, value_length)
            if parsed < 0:
                return -1
            if self._req_content_length >= 0 and self._req_content_length != parsed:
                return -1
            self._req_content_length = parsed
        elif name_length == 15 and memcmp(name, "accept-encoding", 15) == 0:
            if self._brotli_enabled or self._zstd_enabled or self._gzip_enabled:
                self._scan_request_accept_encoding(value, value_length)
        self._req_raw_count += 1
        self._req_pending_header = False
        return 0

    cdef void _scan_request_accept_encoding(
        self,
        const char* value,
        size_t length,
    ) noexcept:
        cdef size_t start = 0
        cdef size_t segment_end
        cdef size_t separator
        cdef size_t token_end
        cdef size_t param_start
        cdef size_t param_end
        cdef size_t equals
        cdef size_t key_start
        cdef size_t key_end
        cdef int q
        if not self._req_accept_present:
            # First Accept-Encoding header initializes the q table.
            self._req_accept_present = True
            self._req_br_q = -1
            self._req_zstd_q = -1
            self._req_gzip_q = -1
            self._req_wildcard_q = -1
            self._req_identity_q = -1
        while start < length:
            # One comma-separated "token;param=...;q=..." segment.
            segment_end = start
            while segment_end < length and value[segment_end] != <char>44:
                segment_end += 1
            while start < segment_end and (
                value[start] == <char>32 or value[start] == <char>9
            ):
                start += 1
            separator = start
            while separator < segment_end and value[separator] != <char>59:
                separator += 1
            token_end = separator
            while token_end > start and (
                value[token_end - 1] == <char>32
                or value[token_end - 1] == <char>9
            ):
                token_end -= 1

            q = 1000
            param_start = separator
            while param_start < segment_end:
                param_start += 1
                param_end = param_start
                while (
                    param_end < segment_end
                    and value[param_end] != <char>59
                ):
                    param_end += 1
                equals = param_start
                while equals < param_end and value[equals] != <char>61:
                    equals += 1
                key_start = param_start
                while key_start < equals and (
                    value[key_start] == <char>32
                    or value[key_start] == <char>9
                ):
                    key_start += 1
                key_end = equals
                while key_end > key_start and (
                    value[key_end - 1] == <char>32
                    or value[key_end - 1] == <char>9
                ):
                    key_end -= 1
                if (
                    equals < param_end
                    and key_end - key_start == 1
                    and (
                        value[key_start] == <char>113
                        or value[key_start] == <char>81
                    )
                ):
                    q = _parse_qvalue(value, equals + 1, param_end)
                    break
                param_start = param_end

            if _token_equals(value, start, token_end, "br", 2):
                self._req_br_q = q
            elif _token_equals(value, start, token_end, "zstd", 4):
                self._req_zstd_q = q
            elif _token_equals(value, start, token_end, "gzip", 4):
                self._req_gzip_q = q
            elif _token_equals(value, start, token_end, "*", 1):
                self._req_wildcard_q = q
            elif _token_equals(value, start, token_end, "identity", 8):
                self._req_identity_q = q
            start = segment_end + 1

    cdef void _clear_request_headers(self) noexcept:
        if (
            self._req_arena != NULL
            and self._req_arena_cap > REQUEST_ARENA_RETAIN_MAX
        ):
            free(self._req_arena)
            self._req_arena = NULL
            self._req_arena_cap = 0
        if (
            self._req_raw_headers != NULL
            and self._req_raw_headers_cap > REQUEST_HEADERS_RETAIN_MAX
        ):
            free(self._req_raw_headers)
            self._req_raw_headers = NULL
            self._req_raw_headers_cap = 0
        self._req_arena_len = 0
        self._req_raw_count = 0
        self._req_pending_header = False
        self._req_host_index = -1
        self._req_cookie_index = -1
        self._req_authorization_index = -1
        self._req_url_offset = 0
        self._req_url_length = 0
        self._clear_hot_request_headers()

    cdef void reset(
        self,
        HttpConnection connection,
        object app,
        object transport,
        object compression,
        int max_body_size,
        double body_timeout,
    ) noexcept:
        self.in_pool = False
        if self._connection is not connection:
            self._connection = connection
            self.app = app
            self._transport = transport
            self._t_is_closing = transport.is_closing if transport is not None else None
            self._t_writelines = transport.writelines if transport is not None else None
            if self._compression is not compression:
                self._compression = compression
                self._apply_compression(compression)
        self._max_size = max_body_size
        self._timeout = body_timeout
        self.span = None
        self.match = EMPTY_MATCH
        self._clear_request_binding()
        self._clear_request_headers()
        # No leftover from the last response or upload may survive.
        self._completed = False
        self._status_code = -1
        self._close_delimited = False
        self._expect_continue = False
        self._discard_body = False
        self.handler_done = False
        self.handler_started = False
        # A connection is H1 or H2 for life. Skip the H2 block on H1 reuse.
        # An H2 exchange recycled onto this thread still has _http2 set.
        if self._http2:
            self._http2 = False
            self._h2_stream_id = 0
            self._h2_pending = b""
            self._h2_pending_off = 0
            self._h2_body_done = False
            self._h2_method = None
            self._h2_got_method = False
            self._h2_got_path = False
            self._h2_got_authority = False
            self._h2_dispatched = False
            self._h2_headers_done = False
            self._h2_headers_sent = False
            self._h2_headers_too_large = False
            self._h2_head_bytes = 0
            self._h2_awaiting_headers = False
            self._h2_header_deadline = 0.0
            self._h2_outbound = False
            self._h2_end_sent = False
            self._h2_flow_paused = False
            self._h2_sent_total = 0
            self._h2_send_mark = 0
            self._h2_send_deadline = 0.0
            self._h2_body_mark = -1
            self._stream_disc = None
        self._handler_task = None
        self._head_request = False

    cdef void bind_http2(self, int32_t stream_id) noexcept:
        self._http2 = True
        self._h2_stream_id = stream_id
        self._h2_got_method = False
        self._h2_got_path = False
        self._h2_got_authority = False
        self._h2_headers_too_large = False
        self._h2_head_bytes = 0
        self._h2_awaiting_headers = True
        self._h2_header_deadline = 0.0
        self._h2_outbound = False
        self._h2_end_sent = False
        self._h2_flow_paused = False
        self._h2_sent_total = 0
        self._h2_send_mark = 0
        self._h2_send_deadline = 0.0
        self._h2_body_mark = -1
        self._stream_disc = None
        self._handler_task = None
        self._head_request = False

    cdef void _clear_hot_request_headers(self) noexcept:
        self._req_encoding = ENCODING_NONE
        self._req_expect_continue = False
        self._req_connection_close = False
        self._req_content_length = -1
        self._req_accept_present = False
        self._req_br_q = -1
        self._req_zstd_q = -1
        self._req_gzip_q = -1
        self._req_wildcard_q = -1
        self._req_identity_q = -1

    cdef void cache_hot_request_headers(self) noexcept:
        """Select response encoding from exchange-local request-header state."""
        cdef int wildcard
        cdef int brotli_q
        cdef int zstd_q
        cdef int gzip_q
        cdef int best_q = 0
        cdef int q_of[4]
        cdef bint on[4]
        cdef int enc
        cdef int i
        if (
            not self._req_accept_present
            or not (self._brotli_enabled or self._zstd_enabled or self._gzip_enabled)
        ):
            self._req_encoding = ENCODING_NONE
            return
        # An unlisted encoding takes the "*" q; unlisted and no "*" means q=0.
        wildcard = (
            self._req_wildcard_q
            if self._req_wildcard_q >= 0
            else 0
        )
        brotli_q = self._req_br_q if self._req_br_q >= 0 else wildcard
        zstd_q = self._req_zstd_q if self._req_zstd_q >= 0 else wildcard
        gzip_q = (
            self._req_gzip_q
            if self._req_gzip_q >= 0
            else wildcard
        )

        # Highest q wins; `CompressionConfig.preference` order breaks ties.
        # The default leads with zstd on live responses: near-equal wire size
        # at a fraction of brotli's encode cost. Precompressed static files
        # negotiate in Python (br first — only wire size counts).
        q_of[ENCODING_BR] = brotli_q
        q_of[ENCODING_ZSTD] = zstd_q
        q_of[ENCODING_GZIP] = gzip_q
        on[ENCODING_BR] = self._brotli_enabled
        on[ENCODING_ZSTD] = self._zstd_enabled
        on[ENCODING_GZIP] = self._gzip_enabled

        self._req_encoding = ENCODING_NONE
        for i in range(3):
            enc = self._enc_order[i]
            if on[enc] and q_of[enc] > best_q:
                best_q = q_of[enc]
                self._req_encoding = enc
        if self._req_identity_q >= best_q:
            self._req_encoding = ENCODING_NONE

    cdef void start_response(self):
        self.handler_started = True
        self.reset_response(self._req_encoding)

    cdef void _clear_request_binding(self) noexcept:
        self._req = None
        self._method = None
        self._path = None
        self._version = None
        self._target_host = None
        # Default until headers-complete. Recycle is not "this request
        # stays open" — callers that still need the value snapshot first.
        self._keep_alive = True
        self._path_flags = 0
        self._protocol_status = 0
        self._protocol_body = None
        self._path_off = 0
        self._path_n = 0
        self._query_off = 0
        self._query_len = 0
        self._handler = None
        self._route = None

    cdef const char* path_ptr(self, Py_ssize_t* n) noexcept:
        """Raw path bytes (no query). Absolute-form with an empty path is ``/``."""
        if self._path_n <= 0 or self._req_arena == NULL:
            n[0] = 1
            return "/"
        n[0] = self._path_n
        return self._req_arena + self._req_url_offset + self._path_off

    cdef object host_from_arena(self):
        cdef RawHeader* header
        if self._target_host is not None:
            return self._target_host
        if self._req_host_index < 0 or self._req_arena == NULL:
            return ""
        header = &self._req_raw_headers[self._req_host_index]
        return _host_without_port_n(
            self._req_arena + header.value_offset,
            <Py_ssize_t>header.value_length,
        )

    cdef object decode_request_path(self):
        """Fully decoded path. Falls back to Latin-1 of the raw bytes if invalid."""
        cdef const char* p
        cdef Py_ssize_t n = 0
        if self._path is not None:
            return self._path
        p = self.path_ptr(&n)
        if self._path_flags & (PATH_HAS_PCT | PATH_RAW_UTF8 | PATH_BAD):
            self._path = decode_path_full(p, n)
            if self._path is None:
                self._path = PyUnicode_DecodeLatin1(p, n, NULL)
        else:
            self._path = PyUnicode_DecodeASCII(p, n, NULL)
        return self._path

    cdef RequestHeaders _ensure_view(self):
        cdef RequestHeaders view = self._req_view
        if view is None:
            view = self._spare_view
            if view is None:
                view = RequestHeaders.__new__(RequestHeaders)
            else:
                self._spare_view = None
            view._ex = self
            self._req_view = view
        return view

    cdef Request ensure_request(self):
        cdef Request request
        cdef RequestHeaders view
        if self._req is not None:
            return self._req
        view = self._ensure_view()
        request = make_request(
            self._method if self._method is not None else "GET",
            self.decode_request_path(),
            self._version if self._version is not None else "1.1",
            self._keep_alive,
            view,
            self,
        )
        request._view = view
        if self._path_n > 0:
            request._p_off = self._req_url_offset + self._path_off
            request._p_len = self._path_n
        else:
            request._raw_path = b"/"
        if self._query_len > 0:
            request._q_off = self._query_off
            request._q_len = self._query_len
        if self._target_host is not None:
            request._host = self._target_host
        self._req = request
        return request

    cdef RequestHandle start_handle(self):
        """Fresh ``c`` / ``w`` for this dispatch. Stale once the exchange recycles."""
        cdef RequestHandle handle = self._spare_handle
        if handle is None:
            handle = RequestHandle.__new__(RequestHandle)
        else:
            self._spare_handle = None
        handle._ex = self
        handle._conn = self._connection
        handle._disc = None
        handle.app = self.app
        handle.span = self.span
        handle.match = self.match
        handle._final_status = -1
        handle._req = None
        self._handle = handle
        return handle

    cdef void _detach_views(self):
        """Cut every per-request object loose from this exchange.

        A leftover ``c`` / ``w`` / ``c.req`` / ``req.headers`` keeps working
        from a private copy. Everything else is dropped. Nothing handed to a
        handler can observe the next request.
        """
        cdef RequestHandle handle = self._handle
        cdef Request request
        cdef RequestHeaders view
        cdef Headers headers
        self._handle = None
        if handle is not None:
            if not stario_is_unique(handle):
                stario_note_retained()
                handle._req = self.ensure_request()
                handle._final_status = self._status_code
                handle._ex = None
                # Snapshot the right future (request-scoped for H2 and HEAD,
                # conn otherwise) so a retained ``c`` stops pinning the protocol.
                handle._disc = self._stream_disc
                if handle._disc is None:
                    if self._head_request:
                        # detach() resolves it right after this.
                        handle._disc = self._connection.ensure_stream_disconnect(self)
                    else:
                        handle._disc = self._connection.ensure_disconnect()
                handle._conn = None
            else:
                # Only this local refers to it. start_handle() rebinds every field.
                handle._ex = None
                handle._state = None
                self._spare_handle = handle
            handle = None
        request = self._req
        self._req = None
        if request is not None:
            if not stario_is_unique(request):
                stario_note_retained()
                request.detach_body(self)
            request = None
        view = self._req_view
        self._req_view = None
        if view is not None:
            if not stario_is_unique(view):
                stario_note_retained()
                view._take_ownership()
            else:
                view._ex = None
                self._spare_view = view
            view = None
        headers = self.headers
        # Drop the field so uniqueness is this local only. A task that kept
        # ``w.headers`` still holds a ref; give the next response its own list.
        self.headers = None
        if headers is not None and not stario_is_unique(headers):
            stario_note_retained()
            self.headers = Headers()
        else:
            self.headers = headers

    def on_handler_done(self, task):
        """Log/abort on failure, then recycle after the handler task."""
        cdef RequestHandle handle = self._handle
        if task is not self._handler_task:
            # Done-callback from a request this exchange no longer serves.
            return
        if handle is not None:
            on_handler_done(handle, handle, task)
            # Drop our reference before recycle counts who still holds ``c``.
            handle = None
        self.handler_finished()

    cdef void handler_finished(self):
        self.handler_done = True
        if self._body_active and not self._body_complete:
            self._discard_body = True
            self._cached = None
            self._clear_body_storage()
            self._cancel_stall_timer()
            self._connection.set_body_paused(self, False)
            if self._http2:
                self._connection.h2_handler_finished(self)
        self._maybe_recycle()

    cdef void cancel_before_start(self):
        if self.handler_started or self.in_pool:
            return
        self.c_abort()
        self._completed = True
        self.handler_done = True
        self._maybe_recycle()

    cdef void _maybe_recycle(self):
        # Safe only when nothing in flight can still touch this exchange:
        # response done, handler done, body fully read, no pending H2 writes.
        if (
            not self.in_pool
            and self._completed
            and self.handler_done
            and (not self._body_active or self._body_complete)
            and (not self._http2 or not self._h2_outbound)
        ):
            self._connection.release_exchange(self)

    cdef void detach(self):
        """Drop request-scoped state. May sit in the connection idle slot."""
        if self.in_pool:
            return
        self.in_pool = True
        self._generation += 1
        wake_waiters(self._drain_waiters)
        self._detach_views()
        if self._stream_disc is not None:
            # This stream's channel is over; wake anyone still watching it.
            if not self._stream_disc.done():
                self._stream_disc.set_result(None)
            self._stream_disc = None
        self._clear_request_binding()
        self._cached = None
        self._wake()
        self._data_ready = None
        self._h2_pending = b""
        self._h2_pending_off = 0
        self._cancel_stall_timer()
        self._clear_body_storage()
        self._body_active = False
        # reset_body() / mark_nobody() run before any read, but an idle
        # exchange must not carry the last upload's abort or consumer state
        # into the window before headers-complete (c_abort reads it).
        self._abort_reason = ABORT_NONE
        self._consumed_as = CONSUMED_NONE
        self._read_max_size = -1
        self._clear_hot_request_headers()
        self._stream_max_chunk = DEFAULT_STREAM_CHUNK
        if (
            self._out_buf is not None
            and PyByteArray_GET_SIZE(self._out_buf) > OUTPUT_BUFFER_RETAIN_MAX
        ):
            self._out_buf = None

    cdef void recycle(self):
        """Return to this thread's spare list, or drop if the list is full."""
        cdef list pool
        if not self.in_pool:
            self.detach()
        if self._connection is None:
            return
        self._free_compressors()
        self.headers.c_clear()
        self._clear_request_headers()
        self._clear_request_binding()
        self.span = None
        self.match = EMPTY_MATCH
        self.app = None
        self._connection = None
        self._transport = None
        self._t_is_closing = None
        self._t_writelines = None
        if self._spare_handle is not None:
            self._spare_handle._conn = None
            self._spare_handle.app = None
            self._spare_handle.span = None
            self._spare_handle.match = None
        pool = _thread_pool()
        if len(pool) < POOL_MAX:
            pool.append(self)

    cdef void _done(self):
        if (
            self._head_request
            and self._stream_disc is not None
            and not self._stream_disc.done()
        ):
            # A HEAD request ends with its response (any helper): wake
            # c.disconnect / c.alive() before the connection moves on.
            # A future made later is resolved in RequestHandle.disconnect.
            self._stream_disc.set_result(None)
        if self._body_active and self._consumed_as == CONSUMED_NONE:
            # Response is out; an unread body may buffer again (up to the cap)
            # so the connection can move on.
            self._connection.set_body_paused(self, False)
        self._connection.response_completed(self)
        self._maybe_recycle()

    @property
    def status_code(self):
        if self._status_code < 0:
            return None
        return self._status_code

    @property
    def started(self):
        return self._status_code >= 0

    @property
    def completed(self):
        return self._completed

    cdef Py_ssize_t _body_nbytes(self, object body) except -2:
        """Total byte length of ``bytes`` or a list/tuple of bytes-like parts."""
        cdef Py_ssize_t total
        cdef object part
        if body is None:
            return 0
        if _is_bytes_like(body):
            return _real_len(body)
        if isinstance(body, (list, tuple)):
            total = 0
            for part in body:
                _require_bytes_like(part)
                total += _real_len(part)
            return total
        raise TypeError(BODY_TYPE_ERROR)

    cdef object _body_as_bytes(self, object body):
        """Contiguous bytes for one-shot compression (joins list/tuple once)."""
        if body is None:
            return b""
        if isinstance(body, bytes):
            return body
        if isinstance(body, (bytearray, memoryview)):
            return bytes(body)
        if isinstance(body, (list, tuple)):
            if not body:
                return b""
            if len(body) == 1 and isinstance(body[0], bytes):
                return body[0]
            return b"".join(body)
        raise TypeError(BODY_TYPE_ERROR)

    cdef bint _may_compress(
        self,
        object data,
        object content_type,
        bint streaming,
        Py_ssize_t nbytes,
    ):
        if self._req_encoding == ENCODING_NONE:
            return False
        if not streaming:
            if data is None or nbytes < self._compress_min_size:
                return False
        if content_type is not None and not content_type_is_compressible(content_type):
            return False
        return True

    cdef int _frame(
        self,
        object data,
        object encoding,
        const unsigned char** out,
        size_t* out_len,
    ) except -1:
        cdef const char* ptr = data
        cdef size_t n = <size_t>_real_len(data)
        if encoding == b"br":
            # Whole body known: the one-shot API skips the stream encoder.
            if stario_brotli_compress_borrowed(
                self._brotli_level,
                self._brotli_window,
                <const unsigned char*>ptr,
                n,
                out,
                out_len,
            ) != 0:
                raise StarioError("brotli compression failed")
            return 0
        if encoding == b"zstd":
            self._ensure_zstd_1s()
            if stario_zstd_finish_borrowed(
                self._zstd_1s, <const unsigned char*>ptr, n, out, out_len
            ) != 0:
                raise StarioError("zstd compression failed")
            return 0
        self._ensure_gzip_1s()
        if stario_gzip_finish_borrowed(
            self._gzip_1s, <const unsigned char*>ptr, n, out, out_len
        ) != 0:
            raise StarioError("gzip compression failed")
        return 0

    cdef int _block(self, object data, const unsigned char** out, size_t* out_len) except -1:
        cdef Py_buffer view
        if isinstance(data, memoryview):
            PyObject_GetBuffer(data, &view, PyBUF_SIMPLE)
            try:
                return self._block_raw(
                    <const char*>view.buf, <size_t>view.len, out, out_len
                )
            finally:
                PyBuffer_Release(&view)
        return self._block_raw(data, <size_t>_real_len(data), out, out_len)

    cdef int _block_raw(
        self,
        const char* ptr,
        size_t n,
        const unsigned char** out,
        size_t* out_len,
    ) except -1:
        if self._brotli != NULL:
            if stario_brotli_block_borrowed(
                self._brotli, <const unsigned char*>ptr, n, out, out_len
            ) != 0:
                raise StarioError("brotli stream failed")
        elif self._zstd != NULL:
            if stario_zstd_block_borrowed(
                self._zstd, <const unsigned char*>ptr, n, out, out_len
            ) != 0:
                raise StarioError("zstd stream failed")
        elif self._gzip != NULL:
            if stario_gzip_block_borrowed(
                self._gzip, <const unsigned char*>ptr, n, out, out_len
            ) != 0:
                raise StarioError("gzip stream failed")
        else:
            return 0
        if out_len[0] == 0:
            raise StarioError("compression flush produced no output")
        return 1

    cdef int _finish(self, const unsigned char** out, size_t* out_len) except -1:
        if self._brotli != NULL:
            if stario_brotli_finish_borrowed(
                self._brotli, NULL, 0, out, out_len
            ) != 0:
                raise StarioError("brotli finish failed")
            return 1
        if self._zstd != NULL:
            if stario_zstd_finish_borrowed(
                self._zstd, NULL, 0, out, out_len
            ) != 0:
                raise StarioError("zstd finish failed")
            return 1
        if self._gzip != NULL:
            if stario_gzip_finish_borrowed(
                self._gzip, NULL, 0, out, out_len
            ) != 0:
                raise StarioError("gzip finish failed")
            return 1
        return 0

    cdef int _write_native_chunk(
        self, const unsigned char* data, size_t n
    ) except -1:
        if n == 0:
            return 0
        if self._close_delimited:
            self._buf_add(<const char*>data, <Py_ssize_t>n)
            self._flush()
            return 0
        self._buf_uint(n, 16)
        self._buf_bytes(CRLF)
        self._buf_add(<const char*>data, <Py_ssize_t>n)
        self._buf_bytes(CRLF)
        self._flush()
        return 0

    cdef object _h2_date_value(self):
        """The loop clock's ``date: ...\\r\\n`` line as an H2 header value (no name, no CRLF)."""
        cdef object line = self._connection.date_line()
        cdef bytes raw
        cdef Py_ssize_t n
        if line is self._h2_date_line and self._h2_date_bare is not None:
            return self._h2_date_bare
        raw = <bytes>line
        n = PyBytes_GET_SIZE(raw)
        if n > 8 and raw[0] == 100 and raw[n - 2] == 13:
            self._h2_date_bare = raw[6 : n - 2]
        else:
            self._h2_date_bare = raw
        self._h2_date_line = line
        return self._h2_date_bare

    cdef void _h2_respond(
        self,
        object body,
        object content_type,
        int status,
        Py_ssize_t nbytes,
    ):
        cdef Headers h = self.headers
        cdef object encoding
        cdef object flat
        cdef object scanned
        cdef object existing_ce = None
        cdef object existing_cl = None
        cdef const unsigned char* native_out = NULL
        cdef size_t native_len = 0
        cdef list nva
        cdef object payload = body
        if not h.c_empty():
            scanned = h.c_scan_respond(content_type)
            existing_ce = scanned[0]
            existing_cl = scanned[1]

        if not _may_have_body(status):
            payload = b""
            nbytes = 0
        elif existing_ce is None and self._may_compress(body, content_type, False, nbytes):
            encoding = _encoding_wire(self._req_encoding)
            try:
                nva = [
                    (b":status", _dec(<size_t>status)),
                    (b"date", self._h2_date_value()),
                    (b"content-type", content_type),
                ]
                if self._head_request:
                    # RFC 9110 9.3.2: HEAD may omit the compressed length.
                    payload = b""
                else:
                    flat = self._body_as_bytes(body)
                    self._frame(flat, encoding, &native_out, &native_len)
                    if existing_cl is not None:
                        h.c_require_respond_length(existing_cl, _dec(native_len))
                    payload = PyBytes_FromStringAndSize(
                        <char*>native_out, <Py_ssize_t>native_len
                    )
                    nva.append((b"content-length", _dec(native_len)))
                nbytes = <Py_ssize_t>native_len
                nva.append((b"content-encoding", encoding))
                if not h.c_vary_contains(b"accept-encoding"):
                    nva.append((b"vary", b"accept-encoding"))
                self._status_code = status
                self._declared_length = nbytes
                self._bytes_written = nbytes
                self._completed = True
                self._connection.h2_respond(self, nva, payload, True, True, True)
            finally:
                self._free_compressors()
            self._done()
            return

        if existing_cl is not None:
            h.c_require_respond_length(existing_cl, _dec(<size_t>nbytes))
        nva = [
            (b":status", _dec(<size_t>status)),
            (b"date", self._h2_date_value()),
        ]
        if _may_have_body(status):
            nva.append((b"content-type", content_type))
            nva.append((b"content-length", _dec(<size_t>nbytes)))
        if isinstance(payload, (list, tuple)):
            payload = b"".join(payload) if payload else b""
        self._status_code = status
        self._declared_length = nbytes
        self._bytes_written = nbytes if nbytes > 0 else 0
        self._completed = True
        self._connection.h2_respond(
            self, nva, payload if payload is not None else b"", False, True, True
        )
        self._done()

    cpdef void respond(self, body, content_type, int status=200):
        cdef Headers h = self.headers
        cdef object encoding
        cdef object flat
        cdef object scanned
        cdef object existing_ce
        cdef object existing_cl
        cdef Py_ssize_t nbytes
        cdef object wire
        cdef const unsigned char* native_out = NULL
        cdef size_t native_len = 0
        if self._t_is_closing():
            if not self._completed:
                self._completed = True
                self._done()
            return
        if self._status_code >= 0:
            raise StarioRuntime(
                STARTED_ERROR,
                help_text=(
                    "Send the response once: use respond(), or write_headers() "
                    "then write()/end()."
                ),
            )
        _require_final_status(status)
        try:
            content_type = _content_type_bytes(content_type)
        except ValueError as exc:
            raise StarioError(
                "Invalid Content-Type",
                context={"content_type": repr(content_type)[:120]},
                help_text="Content-Type must be a header value without CR or LF.",
            ) from exc

        if (
            self._expect_continue
            and not self._http2
            and self._body_active
            and not self._body_complete
        ):
            # A final response before 100 Continue: the client may or may
            # not send the body (RFC 9110 10.1.1), so the next bytes cannot
            # be framed. Close instead of reading a request as a body.
            self._keep_alive = False
            self.headers.c_set(b"connection", b"close")
        self._expect_continue = False
        if not _may_have_body(status):
            body = b""
            nbytes = 0
        else:
            nbytes = self._body_nbytes(body)
            if self._head_request:
                body = b""
        self._declared_length = nbytes
        self._bytes_written = 0

        if self._http2:
            self._h2_respond(body, content_type, status, nbytes)
            return

        # Empty headers + no compression: interned fragments via writev.
        # uvloop may hold the last write; interned status/date/CL pieces are stable.
        if h.c_empty() and (
            not _may_have_body(status)
            or not self._may_compress(body, content_type, False, nbytes)
        ):
            # Snapshot caller-mutable bodies: the transport may retain the
            # object past this call. ``bytes`` snapshots to itself.
            if body is None:
                wire = b""
            elif type(body) is bytes:
                wire = body
            elif isinstance(body, (list, tuple)):
                wire = [_wire_part(p) for p in body]
            else:
                wire = _wire_part(body)

            if not _may_have_body(status):
                # RFC 9110 §8.6: no Content-Length on 204; 304 omits it too.
                self._t_writelines((
                    _status_line(status),
                    self._connection.date_line(),
                    CRLF,
                ))
            elif nbytes and not self._head_request:
                if type(wire) is bytes and nbytes <= OUTPUT_BUFFER_RETAIN_MAX:
                    self._t_writelines((
                        _status_line(status),
                        self._connection.date_line(),
                        CT_PREFIX,
                        content_type,
                        CL_PREFIX,
                        _dec(<size_t>nbytes),
                        CRLF2,
                        wire,
                    ))
                elif type(wire) is bytes:
                    self._t_writelines((
                        _status_line(status),
                        self._connection.date_line(),
                        CT_PREFIX,
                        content_type,
                        CL_PREFIX,
                        _dec(<size_t>nbytes),
                        CRLF2,
                    ))
                    self._transport.write(wire)
                else:
                    self._t_writelines((
                        _status_line(status),
                        self._connection.date_line(),
                        CT_PREFIX,
                        content_type,
                        CL_PREFIX,
                        _dec(<size_t>nbytes),
                        CRLF2,
                    ))
                    self._t_writelines(wire)
            else:
                self._t_writelines((
                    _status_line(status),
                    self._connection.date_line(),
                    CT_PREFIX,
                    content_type,
                    CL_PREFIX,
                    _dec(<size_t>nbytes),
                    CRLF2,
                ))
        else:
            existing_ce = None
            existing_cl = None
            if not h.c_empty():
                scanned = h.c_scan_respond(content_type)
                existing_ce = scanned[0]
                existing_cl = scanned[1]

            if not _may_have_body(status):
                body = b""
            elif existing_ce is None and self._may_compress(
                body, content_type, False, nbytes
            ):
                encoding = _encoding_wire(self._req_encoding)
                if encoding is not None:
                    try:
                        if not self._head_request:
                            flat = self._body_as_bytes(body)
                            self._frame(flat, encoding, &native_out, &native_len)
                            if existing_cl is not None:
                                h.c_require_respond_length(
                                    existing_cl, _dec(native_len)
                                )
                        self._declared_length = <Py_ssize_t>native_len
                        self._bytes_written = 0
                        self._buf_bytes(_status_line(status))
                        self._buf_bytes(self._connection.date_line())
                        if self._out_buf is None:
                            self._out_buf = bytearray(256)
                        h.c_write_respond_pairs(
                            self._out_buf,
                            &self._out_len,
                            True,
                        )
                        self._buf_bytes(CE_PREFIX)
                        self._buf_bytes(encoding)
                        self._buf_bytes(CRLF)
                        if not h.c_vary_contains(b"accept-encoding"):
                            self._buf_bytes(VARY_PREFIX)
                            self._buf_bytes(b"accept-encoding")
                            self._buf_bytes(CRLF)
                        self._buf_bytes(CT_PREFIX)
                        self._buf_bytes(content_type)
                        if self._head_request:
                            # RFC 9110 9.3.2: the compressed length is only
                            # known by compressing; HEAD may omit it.
                            self._buf_bytes(CRLF2)
                        else:
                            self._buf_bytes(CRLF)
                            self._buf_bytes(CL_HEADER)
                            self._buf_uint(native_len, 10)
                            self._buf_bytes(CRLF2)
                            self._buf_add(<const char*>native_out, <Py_ssize_t>native_len)
                        self._flush()
                    finally:
                        self._free_compressors()
                    self._status_code = status
                    self._bytes_written = self._declared_length
                    self._completed = True
                    self._done()
                    return

            self._declared_length = nbytes
            self._bytes_written = 0
            if existing_cl is not None:
                h.c_require_respond_length(existing_cl, _dec(<size_t>nbytes))
            self._buf_bytes(_status_line(status))
            self._buf_bytes(self._connection.date_line())
            if self._out_buf is None:
                self._out_buf = bytearray(256)
            h.c_write_respond_pairs(self._out_buf, &self._out_len, False)
            if _may_have_body(status):
                self._buf_bytes(CT_PREFIX)
                self._buf_bytes(content_type)
                self._buf_bytes(CRLF)
                self._buf_bytes(CL_HEADER)
                self._buf_uint(<size_t>nbytes, 10)
                self._buf_bytes(CRLF2)
            else:
                self._buf_bytes(CRLF)
            self._flush()
            self._status_code = status

            if nbytes and not self._head_request:
                # Snapshot like the fast path: the transport may hold the
                # object past return, so mutation must not reach the wire.
                if isinstance(body, (list, tuple)):
                    self._t_writelines([_wire_part(p) for p in body])
                else:
                    self._transport.write(_wire_part(body))
        self._status_code = status
        self._bytes_written = self._declared_length if self._declared_length > 0 else 0
        self._completed = True
        self._done()

    cpdef void abort(self):
        if self._completed:
            return
        self._free_compressors()
        self._completed = True
        self.headers.c_set(b"connection", b"close")
        if self._http2:
            self._connection.h2_abort(self)
        else:
            self._transport.close()
        self._done()

    cdef int _stream_encoding(self, Headers headers, bint allocate) except -1:
        cdef object encoding
        if headers.c_get(b"content-encoding") is not None:
            return 0
        if not self._may_compress(None, headers.c_get(b"content-type"), True, -1):
            return 0
        encoding = _encoding_wire(self._req_encoding)
        if encoding is None:
            return 0
        if allocate:
            if encoding == b"br":
                self._ensure_brotli()
            elif encoding == b"zstd":
                self._ensure_zstd()
            else:
                self._ensure_gzip()
        headers.c_set(b"content-encoding", encoding)
        headers.c_merge_vary(b"accept-encoding")
        return 0

    cpdef object write_headers(self, int status_code):
        cdef Headers headers = self.headers
        cdef object raw_length
        cdef list nva
        if self._t_is_closing():
            return self
        if self._status_code >= 0:
            raise StarioRuntime(
                STARTED_ERROR,
                help_text=(
                    "Send the response once: use respond(), or write_headers() "
                    "then write()/end()."
                ),
            )
        _require_final_status(status_code)
        self._bytes_written = 0

        raw_length = headers.c_get(b"content-length")
        if not _may_have_body(status_code):
            headers.c_remove(b"transfer-encoding")
            headers.c_remove(b"content-length")
            self._declared_length = 0
        elif raw_length is not None:
            headers.c_remove(b"transfer-encoding")
            self._declared_length = _parse_declared_length(raw_length)
            # One canonical field: drops duplicates and leading zeros.
            headers.c_set(b"content-length", _dec(<size_t>self._declared_length))
        elif self._head_request:
            # RFC 9110 9.3.2: a HEAD response may omit Content-Length, which
            # GET only learns while streaming. Everything else matches GET.
            headers.c_remove(b"transfer-encoding")
            self._declared_length = 0
            self._stream_encoding(headers, False)
        elif self._http2:
            # DATA frames carry the compressed stream; no chunked coding.
            self._stream_encoding(headers, True)
        else:
            if self._version == "1.0":
                # RFC 9112 7: HTTP/1.0 has no chunked coding.
                headers.c_remove(b"transfer-encoding")
                headers.c_set(b"connection", b"close")
                self._close_delimited = True
            else:
                headers.c_set(b"transfer-encoding", b"chunked")
            self._stream_encoding(headers, True)
        if (
            self._expect_continue
            and not self._http2
            and self._body_active
            and not self._body_complete
        ):
            # As in respond(): the body may never come, so do not reuse.
            self._keep_alive = False
            headers.c_set(b"connection", b"close")
        self._expect_continue = False

        if self._http2:
            nva = [
                (b":status", _dec(<size_t>status_code)),
                (b"date", self._h2_date_value()),
            ]
            self._connection.h2_write_headers(self, nva, False, False, False)
            self._status_code = status_code
            if self._head_request:
                self._finish_head()
            return self

        self._buf_bytes(_status_line(status_code))
        self._buf_bytes(self._connection.date_line())
        if self._out_buf is None:
            self._out_buf = bytearray(256)
        headers.c_write_wire_ba(self._out_buf, &self._out_len)
        self._buf_bytes(CRLF)
        self._flush()
        self._status_code = status_code
        if self._head_request:
            self._finish_head()
        return self

    cdef void _finish_head(self):
        """End a HEAD response at its headers (RFC 9112 6.3, rule 1).

        The client reads no body, so the connection moves on now. Later
        write() / end() calls are no-ops, and ``_done()`` ends the request's
        own disconnect future, so ``c.alive()`` stops a streaming handler
        (SSE) instead of letting it run on a keep-alive connection.
        """
        # Completed first: END_STREAM can close the stream synchronously,
        # and an incomplete close reads as a client abort.
        self._completed = True
        if self._http2:
            self._connection.h2_end(self)
        self._done()

    cpdef object write(self, object data):
        cdef Py_ssize_t n
        cdef object part
        cdef const unsigned char* native_out = NULL
        cdef size_t native_len = 0
        if self._t_is_closing():
            self._free_compressors()
            return self
        if self._completed:
            if self._head_request:
                # The HEAD response ended at its headers; drop the body.
                return self
            raise StarioRuntime(
                "Cannot write after response is completed. "
                "This happens after calling w.end() or a response helper has "
                "already finalized the writer. "
                "Each handler should only send one response.",
                help_text=(
                    "Send one response per handler: stream with write()/end(), "
                    "or finish with a response helper — not both."
                ),
            )
        if not data:
            return self
        n = self._body_nbytes(data)

        if self._status_code < 0:
            self.write_headers(200)
        if self._status_code >= 0 and not _may_have_body(self._status_code):
            raise StarioRuntime(
                f"Cannot write a body for HTTP {self._status_code} responses.",
                help_text=(
                    "204/304 and 1xx responses must not include a message body."
                ),
            )
        if self._head_request or n == 0:
            return self
        if self._declared_length >= 0 and self._bytes_written + n > self._declared_length:
            raise StarioRuntime(
                "Response body exceeds Content-Length: writing "
                f"{n} bytes after {self._bytes_written}, Content-Length is "
                f"{self._declared_length}",
                help_text=(
                    "When Content-Length is set, write exactly that many bytes. "
                    "Nothing past it is sent."
                ),
            )
        if self._http2:
            self._bytes_written += n
            if self._brotli != NULL or self._zstd != NULL or self._gzip != NULL:
                if isinstance(data, (list, tuple)):
                    for part in data:
                        _require_bytes_like(part)
                        if part:
                            self._block(part, &native_out, &native_len)
                            self._connection.h2_write_data(
                                self,
                                PyBytes_FromStringAndSize(
                                    <char*>native_out, <Py_ssize_t>native_len
                                ),
                                False,
                            )
                else:
                    self._block(data, &native_out, &native_len)
                    self._connection.h2_write_data(
                        self,
                        PyBytes_FromStringAndSize(
                            <char*>native_out, <Py_ssize_t>native_len
                        ),
                        False,
                    )
                return self
            if isinstance(data, (list, tuple)):
                for part in data:
                    if part:
                        self._connection.h2_write_data(self, part, False)
            else:
                self._connection.h2_write_data(self, data, False)
            return self

        if self._declared_length >= 0:
            self._bytes_written += n
            if isinstance(data, (list, tuple)):
                self._transport.writelines([_wire_part(part) for part in data])
            else:
                self._transport.write(_wire_part(data))
            return self

        if self._brotli != NULL or self._zstd != NULL or self._gzip != NULL:
            if isinstance(data, (list, tuple)):
                for part in data:
                    _require_bytes_like(part)
                    if not part:
                        continue
                    self._block(part, &native_out, &native_len)
                    self._write_native_chunk(native_out, native_len)
            else:
                self._block(data, &native_out, &native_len)
                self._write_native_chunk(native_out, native_len)
            return self
        # Chunked and close-delimited bodies go straight to the transport as
        # iovecs (writev under uvloop): ``bytes`` is sent without a copy and
        # mutable parts are snapshotted once here instead of twice through
        # ``_out_buf`` + ``_flush``.
        if self._out_len:
            self._flush()
        if type(data) is bytes:
            if self._close_delimited:
                self._t_writelines((data,))
            else:
                self._t_writelines((_chunk_header(n), data, CRLF))
            return self

        if isinstance(data, (list, tuple)):
            parts = []
            if not self._close_delimited:
                parts.append(_chunk_header(n))
            for part in data:
                parts.append(_wire_part(part))
            if not self._close_delimited:
                parts.append(CRLF)
            self._t_writelines(parts)
            return self
        if self._close_delimited:
            self._t_writelines((_wire_part(data),))
            return self
        self._t_writelines((_chunk_header(n), _wire_part(data), CRLF))
        return self

    cpdef void end(self, object data=None):
        cdef object cl
        cdef const unsigned char* native_out = NULL
        cdef size_t native_len = 0
        if self._completed:
            return
        if self._t_is_closing():
            self._free_compressors()
            self._completed = True
            self._done()
            return
        if self._status_code < 0:
            if data is not None:
                cl = _dec(<size_t>self._body_nbytes(data))
                self.headers.c_set(b"content-length", cl)
                self.write_headers(200)
            else:
                self.write_headers(204)
        if self._completed:
            # HEAD: write_headers() already ended the response.
            return
        if data:
            self.write(data)

        if self._declared_length >= 0 and self._bytes_written != self._declared_length:
            raise StarioRuntime(
                "Response body length mismatch: wrote "
                f"{self._bytes_written} bytes, Content-Length is {self._declared_length}",
                help_text=(
                    "When Content-Length is set, write exactly that many bytes "
                    "before w.end()."
                ),
            )

        if self._http2:
            if self._brotli != NULL or self._zstd != NULL or self._gzip != NULL:
                self._finish(&native_out, &native_len)
                if native_len:
                    self._connection.h2_write_data(
                        self,
                        PyBytes_FromStringAndSize(
                            <char*>native_out, <Py_ssize_t>native_len
                        ),
                        False,
                    )
                self._free_compressors()
            self._completed = True
            self._connection.h2_end(self)
            self._done()
            return

        if self._declared_length < 0:
            # No Content-Length: chunked (or close-delimited) terminator.
            if self._brotli != NULL or self._zstd != NULL or self._gzip != NULL:
                self._finish(&native_out, &native_len)
                self._write_native_chunk(native_out, native_len)
                self._free_compressors()
            if not self._close_delimited:
                self._buf_bytes(CHUNK_END)
            self._flush()
        self._completed = True
        self._done()

    cdef void reset_body(self, bint expect_continue, Py_ssize_t expected_size) noexcept:
        self._body_active = True
        self._clear_body_storage()
        self._cached = None
        self._data_ready = None
        self._cancel_stall_timer()
        self._expect_continue = expect_continue
        self._total_read = 0
        self._read_max_size = -1
        self._consumed_as = CONSUMED_NONE
        self._abort_reason = ABORT_NONE
        self._body_complete = False
        self._waiting = False
        self._discard_body = False
        self._expected_size = expected_size
        self._stream_max_chunk = DEFAULT_STREAM_CHUNK

    cdef void mark_nobody(self) noexcept:
        """GET/HEAD/empty POST: no upload, body() is b'' without Event waits."""
        self._clear_body_storage()
        self._cached = b""
        self._data_ready = None
        self._cancel_stall_timer()
        self._expect_continue = False
        self._total_read = 0
        self._read_max_size = -1
        self._consumed_as = CONSUMED_NONE
        self._abort_reason = ABORT_NONE
        self._body_active = False
        self._body_complete = True
        self._waiting = False
        self._discard_body = False
        self._expected_size = 0

    cdef void _clear_body_storage(self) noexcept:
        self._body_buf = None
        self._body_used = 0
        self._body_cap = 0

    cdef inline char* _body_ptr(self) noexcept:
        if type(self._body_buf) is bytes:
            return PyBytes_AS_STRING(self._body_buf)
        return PyByteArray_AS_STRING(self._body_buf)

    cdef int _body_reserve(self, Py_ssize_t need) noexcept:
        # The allocations below raise on failure. In a noexcept function
        # that would be reported as "unraisable" and returned as 0, and the
        # caller would copy body bytes into a buffer that is not there.
        try:
            return self._body_reserve_checked(need)
        except BaseException:
            return -1

    cdef int _body_reserve_checked(self, Py_ssize_t need) except? -1:
        cdef object buf
        cdef Py_ssize_t cap
        if need <= 0:
            return 0
        if self._body_buf is not None and need <= self._body_cap:
            return 0

        if self._body_buf is None:
            # Reserve the declared length only when it is small (the body
            # completes before dispatch) or body() is reading it; a declared
            # 10 MB that nobody reads yet must not allocate 10 MB.
            if self._expected_size > 0 and (
                self._consumed_as == CONSUMED_BODY
                or (
                    self._consumed_as == CONSUMED_NONE
                    and self._expected_size <= SMALL_BODY
                )
            ):
                cap = self._expected_size
                if cap < need:
                    cap = need
                # A fixed Content-Length lands in an uninitialized bytes
                # object. It stays private to this exchange until complete,
                # then ``body()`` hands it out without a second copy.
                buf = PyBytes_FromStringAndSize(NULL, cap)
                if buf is None:
                    PyErr_Clear()
                    return -1
                self._body_buf = buf
                self._body_cap = cap
                return 0
            cap = DEFAULT_STREAM_CHUNK
            if self._consumed_as == CONSUMED_STREAM:
                cap = self._stream_max_chunk
            if cap < need:
                cap = need
            buf = bytearray()
            if PyByteArray_Resize(buf, cap) < 0:
                PyErr_Clear()
                return -1
            self._body_buf = buf
            self._body_cap = cap
            return 0

        if (
            self._expected_size > 0
            and self._consumed_as == CONSUMED_BODY
            and type(self._body_buf) is not bytes
            and need <= self._expected_size
        ):
            # body() started after chunks already buffered: move what has
            # arrived into a full-size bytes buffer, then feed the rest in
            # place so completion needs no copy.
            buf = PyBytes_FromStringAndSize(NULL, self._expected_size)
            if buf is None:
                PyErr_Clear()
                return -1
            memcpy(
                PyBytes_AS_STRING(buf),
                PyByteArray_AS_STRING(self._body_buf),
                <size_t>self._body_used,
            )
            self._body_buf = buf
            self._body_cap = self._expected_size
            return 0

        if type(self._body_buf) is bytes:
            # bytes cannot grow: a declared length that still needs room is
            # defensive only — fall back to a bytearray.
            buf = bytearray()
            if PyByteArray_Resize(buf, need) < 0:
                PyErr_Clear()
                return -1
            memcpy(
                PyByteArray_AS_STRING(buf),
                PyBytes_AS_STRING(self._body_buf),
                <size_t>self._body_used,
            )
            self._body_buf = buf
            self._body_cap = need
            return 0

        cap = self._body_cap * 2
        if cap < need:
            cap = need
        if PyByteArray_Resize(self._body_buf, cap) < 0:
            self._clear_body_storage()
            PyErr_Clear()
            return -1
        self._body_cap = cap
        return 0

    cdef int _adopt_expected_body_buffer(self) noexcept:
        """Grow the one body buffer to the declared Content-Length."""
        try:
            return self._adopt_expected_body_checked()
        except BaseException:
            return -1

    cdef int _adopt_expected_body_checked(self) except? -1:
        cdef object buf
        if self._expected_size <= 0 or self._consumed_as == CONSUMED_STREAM:
            return 0
        if self._body_complete:
            return 0
        if self._body_used > self._expected_size:
            return 0
        if type(self._body_buf) is not bytes:
            # Move what already arrived into a full-size bytes object; the
            # rest feeds in place and completion hands it out as-is.
            buf = PyBytes_FromStringAndSize(NULL, self._expected_size)
            if buf is None:
                PyErr_Clear()
                return -1
            if self._body_used > 0:
                memcpy(
                    PyBytes_AS_STRING(buf),
                    self._body_ptr(),
                    <size_t>self._body_used,
                )
            self._body_buf = buf
            self._body_cap = self._expected_size
            return 0
        return self._body_reserve(self._expected_size)

    cdef object _body_to_bytes(self):
        cdef object out
        cdef Py_ssize_t used = self._body_used
        if used <= 0 or self._body_buf is None:
            self._clear_body_storage()
            return b""
        if (
            type(self._body_buf) is bytes
            and used == PyBytes_GET_SIZE(self._body_buf)
        ):
            # The whole Content-Length body is already in a private bytes
            # object: hand it over, no copy.
            out = self._body_buf
            self._clear_body_storage()
            return out
        out = PyBytes_FromStringAndSize(self._body_ptr(), used)
        self._clear_body_storage()
        if out is None:
            raise MemoryError()
        return out

    cdef object _body_take(self, Py_ssize_t max_n):
        cdef Py_ssize_t take
        cdef object out
        cdef char* src
        if self._body_used <= 0 or self._body_buf is None:
            return b""
        take = self._body_used if max_n <= 0 or max_n > self._body_used else max_n
        src = self._body_ptr()
        out = PyBytes_FromStringAndSize(src, take)
        if take < self._body_used:
            memmove(src, src + take, <size_t>(self._body_used - take))
        self._body_used -= take
        return out

    cdef void _raise_abort(self):
        if self._abort_reason == ABORT_TOO_LARGE:
            raise RequestBodyError(413, "Request body too large")
        if self._abort_reason == ABORT_TIMEOUT:
            raise RequestBodyError(408, "Request timeout: body upload too slow.")
        if self._abort_reason == ABORT_DISCONNECTED:
            raise ClientDisconnected()

    cdef void _wake(self):
        if self._data_ready is not None:
            self._data_ready.set()

    cdef void _cancel_stall_timer(self) noexcept:
        self._stall_deadline = 0.0
        self._stall_seen = self._stall_touch

    cdef void _reset_stall_timer(self) noexcept:
        """Refresh the body-read deadline while ``body()`` or ``stream()`` is waiting.

        Sweep mode only bumps a generation counter. The connection sweeper
        stores ``now + timeout`` once per wake — body chunks do not call
        ``loop.time()`` or ``call_later``.
        """
        if not self._waiting or self._body_complete or self._timeout <= 0:
            self._cancel_stall_timer()
            return
        if (
            self._connection is None
            or self._connection.timeout_cleanup == 0
        ):
            self._cancel_stall_timer()
            return
        self._stall_touch += 1

    cdef void fire_body_stall(self):
        self._stall_deadline = 0.0
        self._stall_seen = self._stall_touch
        if self._abort_reason != ABORT_NONE or self._body_complete:
            return
        self._abort_reason = ABORT_TIMEOUT
        self._clear_body_storage()
        if self._connection is not None:
            self._connection.set_body_paused(self, False)
        self._wake()

    cdef void _maybe_continue(self):
        # One-shot: 100 Continue only makes sense before the response starts.
        if not self._expect_continue:
            return
        if self._status_code >= 0 or self._completed:
            self._expect_continue = False
            return
        self._expect_continue = False
        if self._transport is None or self._transport.is_closing():
            return
        if self._http2:
            self._connection.h2_write_headers(self, [(b":status", b"100")], True, True, True, True)
            return
        self._transport.write(_status_line(100) + CRLF)

    cdef void _maybe_pause(self):
        if self._consumed_as == CONSUMED_STREAM:
            if (
                self._body_used > HIGH_WATER
                and self._body_used >= self._stream_max_chunk
            ):
                self._connection.set_body_paused(self, True)
            return
        if (
            self._consumed_as == CONSUMED_BODY
            and self._body_used > BODY_HIGH_WATER
        ):
            self._connection.set_body_paused(self, True)
            return
        # Handler started but has not asked for the body yet (e.g. it is
        # doing auth first): stop reading instead of buffering up to the
        # body cap. Small bodies that complete before dispatch never get
        # here (handler_started is False until dispatch).
        if (
            self._consumed_as == CONSUMED_NONE
            and self.handler_started
            and not self._completed
            and self._body_used > BODY_HIGH_WATER
        ):
            self._connection.set_body_paused(self, True)
            return
        # Dispatched, but the handler task has not run yet (an HTTP/2
        # stream dispatches here once past SMALL_BODY; a queued HTTP/1
        # request is already held by the pipeline pause). No read limit
        # exists yet, so stop reading instead of buffering.
        if (
            self._consumed_as == CONSUMED_NONE
            and not self.handler_started
            and self._body_used > SMALL_BODY
        ):
            self._connection.set_body_paused(self, True)

    cdef inline Py_ssize_t _feed_limit(self) noexcept:
        # A body nobody will read (handler done) drains up to the default.
        # A read sets its own limit. Before a read, the pause above bounds
        # the buffer, so only the C int caps it: a later read may accept
        # more than the default. After the response, an unread body keeps
        # the default as its cap.
        if self._discard_body:
            return self._max_size
        if self._read_max_size >= 0:
            return self._read_max_size
        if self._completed:
            return self._max_size
        return BODY_LIMIT_MAX

    cdef int c_feed(self, const char* at, size_t length) noexcept:
        cdef Py_ssize_t new_total
        cdef Py_ssize_t used
        if not self._body_active:
            self.reset_body(False, -1)
        new_total = self._total_read + <Py_ssize_t>length
        # Discard leftover DATA on HTTP/2, or a small declared HTTP/1 body
        # the handler did not read, without closing the socket. Any other
        # HTTP/1 body is drained up to the default limit, then closes.
        if self._discard_body:
            if (
                self._http2
                or (
                    self._expected_size >= 0
                    and self._expected_size <= SMALL_BODY
                )
            ):
                self._total_read = new_total
                return 0

        if new_total > self._feed_limit():
            self._abort_reason = ABORT_TOO_LARGE
            self._cancel_stall_timer()
            self._clear_body_storage()
            self._connection.set_body_paused(self, False)
            self._wake()
            if not self._http2 and self._discard_body:
                self._connection.close_lingering()
            return 0

        if self._discard_body:
            self._total_read = new_total
            return 0

        used = self._body_used + <Py_ssize_t>length
        if (
            self._body_reserve(used) != 0
            or self._body_buf is None
            or used > self._body_cap
        ):
            return -1
        if length:
            memcpy(
                self._body_ptr() + self._body_used,
                at,
                length,
            )
        self._body_used = used
        self._total_read = new_total

        if self._consumed_as == CONSUMED_STREAM:
            self._wake()
            if self._waiting:
                self._reset_stall_timer()
            self._maybe_pause()
            return 0
        # body(): refresh stall on progress; wake on complete/abort, and also
        # when over the high-water mark so the waiter can unpause the transport.
        # Without that, _pump_data keeps ingesting parser quantums and a
        # slow consumer never yields.
        if self._waiting:
            self._reset_stall_timer()
        self._maybe_pause()
        if (
            self._consumed_as == CONSUMED_BODY
            and self._waiting
            and self._body_used > BODY_HIGH_WATER
        ):
            self._wake()
        return 0

    cdef int c_complete(self) noexcept:
        if not self._body_active:
            return 0
        self._body_complete = True
        self._cancel_stall_timer()
        if self._connection is not None:
            self._connection.set_body_paused(self, False)

        if self._discard_body:
            self._clear_body_storage()
            self._maybe_recycle()
            return 0
        if self._consumed_as == CONSUMED_STREAM:
            self._wake()
            self._maybe_recycle()
            return 0
        if self._abort_reason != ABORT_NONE:
            self._clear_body_storage()
            self._wake()
            self._maybe_recycle()
            return 0

        if self._cached is None:
            try:
                self._cached = self._body_to_bytes()
            except MemoryError:
                PyErr_Clear()
                return -1
        self._wake()
        self._maybe_recycle()
        return 0

    cdef void c_abort(self):
        wake_waiters(self._drain_waiters)
        if self._stream_disc is not None and not self._stream_disc.done():
            self._stream_disc.set_result(None)
        if self._abort_reason != ABORT_NONE:
            return
        self._free_compressors()
        self._abort_reason = ABORT_DISCONNECTED
        self._body_complete = True
        self._cancel_stall_timer()
        self._clear_body_storage()
        if self._connection is not None:
            self._connection.set_body_paused(self, False)
        self._wake()
        self._maybe_recycle()

    async def _wait_for_body_data(self):
        cdef unsigned long generation = self._generation
        cdef object ready
        if self._abort_reason != ABORT_NONE:
            self._clear_body_storage()
            self._raise_abort()
        if _connection_gone(self._connection):
            self._clear_body_storage()
            self._abort_reason = ABORT_DISCONNECTED
            self._raise_abort()
        if self._data_ready is None:
            self._data_ready = asyncio.Event()
        ready = self._data_ready
        self._waiting = True
        self._reset_stall_timer()
        try:
            await ready.wait()
        finally:
            # A generation bump means the exchange recycled mid-wait; the
            # body buffer belongs to the next request, so leave it alone.
            if generation == self._generation:
                self._waiting = False
                self._cancel_stall_timer()
        if generation != self._generation:
            raise StarioRuntime(BODY_GONE_ERROR)
        ready.clear()
        if self._abort_reason != ABORT_NONE:
            self._clear_body_storage()
            self._raise_abort()

    cdef Py_ssize_t _read_limit(self, object max_size) except -1:
        """Limit for one read: `max_size`, else the server default.

        Raises 413 before the read starts when the body is already known to
        be larger: the declared Content-Length, or what has arrived. So no
        buffer is sized from a large declared length, and a client that
        waits for ``100 Continue`` never gets it.
        """
        cdef Py_ssize_t limit = _limit_for(max_size, self._max_size)
        if self._cached is not None:
            if PyBytes_GET_SIZE(self._cached) > limit:
                raise RequestBodyError(413, "Request body too large")
        elif self._expected_size > limit or self._total_read > limit:
            raise RequestBodyError(413, "Request body too large")
        return limit

    async def stream(self, max_chunk=None, max_size=None):
        cdef object out
        cdef Py_ssize_t chunk_size
        cdef Py_ssize_t limit
        cdef unsigned long generation = self._generation
        if self._discard_body:
            raise StarioRuntime(
                "Request body is no longer available after its handler finished."
            )
        if self._abort_reason != ABORT_NONE:
            self._raise_abort()
        if self._consumed_as == CONSUMED_BODY:
            raise StarioRuntime(
                "Body already read with body(). "
                "Use the returned bytes from body(); request bodies cannot switch to streaming after buffering.",
                help_text="Choose body() or stream() once per request — not both.",
            )
        if self._consumed_as == CONSUMED_STREAM:
            raise StarioRuntime(
                "Body already streaming. Each request body can only be streamed once.",
                help_text="Call stream() only once per request.",
            )

        if max_chunk is None:
            if self._expected_size > 0:
                chunk_size = self._expected_size
                if chunk_size > STREAM_CHUNK_CL:
                    chunk_size = STREAM_CHUNK_CL
            else:
                chunk_size = DEFAULT_STREAM_CHUNK
        else:
            chunk_size = <Py_ssize_t>max_chunk
            if chunk_size <= 0:
                raise ValueError("max_chunk must be positive")
            if chunk_size >= STREAM_CHUNK_LIMIT:
                raise ValueError(
                    f"max_chunk ({chunk_size}) must be lower than "
                    f"stream chunk limit ({STREAM_CHUNK_LIMIT})"
                )
        limit = self._read_limit(max_size)
        self._stream_max_chunk = chunk_size
        self._read_max_size = limit
        self._consumed_as = CONSUMED_STREAM
        if self._cached is not None:
            yield self._cached
            return
        self._maybe_continue()

        while True:
            while self._body_used >= chunk_size or (
                self._body_complete and self._body_used > 0
            ):
                out = self._body_take(chunk_size)
                if self._body_used < LOW_WATER:
                    self._connection.set_body_paused(self, False)
                yield out
                if generation != self._generation:
                    raise StarioRuntime(BODY_GONE_ERROR)
            if self._body_complete:
                return
            # Reading must be on before we wait, or nothing arrives.
            self._connection.set_body_paused(self, False)
            await self._wait_for_body_data()

    async def read(self, max_size=None):
        cdef Py_ssize_t limit
        if self._discard_body:
            raise StarioRuntime(
                "Request body is no longer available after its handler finished."
            )
        if self._abort_reason != ABORT_NONE:
            self._raise_abort()
        if self._consumed_as == CONSUMED_STREAM:
            raise StarioRuntime(
                "Body already streamed. Each request body can only be consumed once.",
                help_text="Choose body() or stream() once per request.",
            )
        if self._consumed_as == CONSUMED_BODY and self._cached is not None:
            # Already read and accepted: only an explicit max_size applies.
            if max_size is not None:
                self._read_limit(max_size)
            return self._cached
        limit = self._read_limit(max_size)
        if self._cached is not None:
            self._consumed_as = CONSUMED_BODY
            return self._cached

        self._consumed_as = CONSUMED_BODY
        self._read_max_size = limit
        if self._adopt_expected_body_buffer() != 0:
            raise MemoryError()
        self._maybe_continue()

        while not self._body_complete:
            if self._abort_reason != ABORT_NONE:
                self._raise_abort()
            if (
                self._read_max_size >= 0
                and self._total_read > self._read_max_size
            ):
                raise RequestBodyError(413, "Request body too large")
            if self._connection is not None:
                self._connection.set_body_paused(self, False)
            if self._body_complete:
                break
            await self._wait_for_body_data()

        if self._connection is not None:
            self._connection.set_body_paused(self, False)
        if self._abort_reason != ABORT_NONE:
            self._raise_abort()
        if self._cached is None:
            self._cached = self._body_to_bytes()
        if PyBytes_GET_SIZE(self._cached) > limit:
            raise RequestBodyError(413, "Request body too large")
        return self._cached


@cython.final
@cython.freelist(64)
cdef class RequestHeaders:
    """Read-only request headers (``c.req.headers``).

    While the handler runs this reads the exchange arena in place. When the
    exchange is recycled and this view is still referenced, it copies the
    arena and header table so it keeps answering for this request.
    """

    def __cinit__(self):
        self._ex = None
        self._own_arena = NULL
        self._own_arena_len = 0
        self._own_headers = NULL
        self._own_count = 0
        self._own_host = -1
        self._own_cookie = -1
        self._own_auth = -1

    def __init__(self, headers=None):
        """Standalone headers for requests built outside the server (tests).

        ``headers`` is a ``Headers`` or a mapping of names to a value or a
        list of values (``str`` or ``bytes``). Names are lowercased.
        """
        cdef list pairs
        cdef object name
        cdef object value
        cdef object item
        if headers is None:
            return
        if isinstance(headers, Headers):
            pairs = (<Headers>headers).unsafe_items()
        else:
            pairs = []
            for name, value in headers.items():
                name = _trusted_bytes(name).lower()
                if type(value) is list:
                    for item in value:
                        pairs.append((name, _trusted_bytes(item)))
                else:
                    pairs.append((name, _trusted_bytes(value)))
        self._own_pairs(pairs)

    cdef int _own_pairs(self, list pairs) except -1:
        cdef Py_ssize_t count = len(pairs)
        cdef Py_ssize_t size = 0
        cdef Py_ssize_t offset = 0
        cdef Py_ssize_t index
        cdef bytes name
        cdef bytes value
        cdef RawHeader* header
        if count == 0:
            return 0
        for name, value in pairs:
            size += PyBytes_GET_SIZE(name) + PyBytes_GET_SIZE(value)
        if size > <Py_ssize_t>0xFFFFFFFF:
            raise ValueError("RequestHeaders are limited to 4 GiB")

        self._own_arena = <char*>malloc(<size_t>(size if size > 0 else 1))
        self._own_headers = <RawHeader*>malloc(<size_t>count * sizeof(RawHeader))
        if self._own_arena == NULL or self._own_headers == NULL:
            raise MemoryError()

        for index in range(count):
            name, value = pairs[index]
            header = &self._own_headers[index]
            header.name_offset = <uint32_t>offset
            header.name_length = <uint32_t>PyBytes_GET_SIZE(name)
            memcpy(self._own_arena + offset, PyBytes_AS_STRING(name), header.name_length)
            offset += header.name_length
            header.value_offset = <uint32_t>offset
            header.value_length = <uint32_t>PyBytes_GET_SIZE(value)
            memcpy(self._own_arena + offset, PyBytes_AS_STRING(value), header.value_length)
            offset += header.value_length
            if self._own_host < 0 and name == b"host":
                self._own_host = index
            elif self._own_cookie < 0 and name == b"cookie":
                self._own_cookie = index
            elif self._own_auth < 0 and name == b"authorization":
                self._own_auth = index
        self._own_arena_len = size
        self._own_count = count
        return 0

    def __dealloc__(self):
        if self._own_arena != NULL:
            free(self._own_arena)
            self._own_arena = NULL
        if self._own_headers != NULL:
            free(self._own_headers)
            self._own_headers = NULL

    cdef inline const char* _arena(self) noexcept:
        if self._ex is not None:
            return self._ex._req_arena
        return self._own_arena

    cdef inline Py_ssize_t _arena_len(self) noexcept:
        if self._ex is not None:
            return self._ex._req_arena_len
        return self._own_arena_len

    cdef inline RawHeader* _headers(self) noexcept:
        if self._ex is not None:
            return self._ex._req_raw_headers
        return self._own_headers

    cdef inline Py_ssize_t _count(self) noexcept:
        if self._ex is not None:
            return self._ex._req_raw_count
        return self._own_count

    cdef inline Py_ssize_t _host_index(self) noexcept:
        if self._ex is not None:
            return self._ex._req_host_index
        return self._own_host

    cdef inline Py_ssize_t _cookie_index(self) noexcept:
        if self._ex is not None:
            return self._ex._req_cookie_index
        return self._own_cookie

    cdef inline Py_ssize_t _auth_index(self) noexcept:
        if self._ex is not None:
            return self._ex._req_authorization_index
        return self._own_auth

    cdef void _take_ownership(self) noexcept:
        cdef RequestExchange ex = self._ex
        cdef Py_ssize_t n
        cdef Py_ssize_t count
        if ex is None:
            return
        n = ex._req_arena_len
        count = ex._req_raw_count
        self._own_host = ex._req_host_index
        self._own_cookie = ex._req_cookie_index
        self._own_auth = ex._req_authorization_index
        if n > 0 and ex._req_arena != NULL:
            self._own_arena = <char*>malloc(<size_t>n)
            if self._own_arena != NULL:
                memcpy(self._own_arena, ex._req_arena, <size_t>n)
                self._own_arena_len = n
        if count > 0 and ex._req_raw_headers != NULL and self._own_arena != NULL:
            self._own_headers = <RawHeader*>malloc(<size_t>count * sizeof(RawHeader))
            if self._own_headers != NULL:
                memcpy(self._own_headers, ex._req_raw_headers, <size_t>count * sizeof(RawHeader))
                self._own_count = count
        if self._own_count == 0:
            self._own_host = -1
            self._own_cookie = -1
            self._own_auth = -1
        self._ex = None

    cdef object c_get(self, object name):
        cdef bytes key = name
        return self.c_get_n(<const char*>key, <Py_ssize_t>len(key))

    cdef object c_request_indexed(self, Py_ssize_t index):
        cdef RawHeader* header
        if index < 0 or index >= self._count():
            return None
        header = &self._headers()[index]
        return PyBytes_FromStringAndSize(
            self._arena() + header.value_offset,
            header.value_length,
        )

    cdef object c_value_str(self, Py_ssize_t index):
        cdef RawHeader* header
        if index < 0 or index >= self._count():
            return None
        header = &self._headers()[index]
        return _decode_latin1(
            self._arena() + header.value_offset,
            <Py_ssize_t>header.value_length,
        )

    cdef Py_ssize_t c_find_n(self, const char* query, Py_ssize_t query_length) noexcept:
        cdef RawHeader* table
        cdef RawHeader* header
        cdef const char* arena
        cdef Py_ssize_t index
        if query_length == 4 and memcmp(query, "host", 4) == 0:
            return self._host_index()
        if query_length == 6 and memcmp(query, "cookie", 6) == 0:
            return self._cookie_index()
        if query_length == 13 and memcmp(query, "authorization", 13) == 0:
            return self._auth_index()
        table = self._headers()
        arena = self._arena()
        for index in range(self._count()):
            header = &table[index]
            if (
                header.name_length == <uint32_t>query_length
                and memcmp(
                    arena + header.name_offset,
                    query,
                    <size_t>query_length,
                ) == 0
            ):
                return index
        return -1

    cdef object c_get_n(self, const char* query, Py_ssize_t query_length):
        return self.c_request_indexed(self.c_find_n(query, query_length))

    cdef object c_getlist_n(self, const char* query, Py_ssize_t query_length):
        cdef RawHeader* table = self._headers()
        cdef const char* arena = self._arena()
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef Py_ssize_t start = 0
        cdef list result
        if query_length == 4 and memcmp(query, "host", 4) == 0:
            start = self._host_index()
        elif query_length == 6 and memcmp(query, "cookie", 6) == 0:
            start = self._cookie_index()
        elif query_length == 13 and memcmp(query, "authorization", 13) == 0:
            start = self._auth_index()
        if start < 0:
            return []
        result = []
        for index in range(start, self._count()):
            header = &table[index]
            if (
                header.name_length == <uint32_t>query_length
                and memcmp(
                    arena + header.name_offset,
                    query,
                    <size_t>query_length,
                ) == 0
            ):
                result.append(
                    PyBytes_FromStringAndSize(
                        arena + header.value_offset,
                        header.value_length,
                    )
                )
        return result

    cdef void c_parse_cookies(self, dict out) except *:
        cdef RawHeader* table = self._headers()
        cdef const char* arena = self._arena()
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef Py_ssize_t start = self._cookie_index()
        if start < 0:
            return
        for index in range(start, self._count()):
            header = &table[index]
            if header.name_length == 6 and memcmp(
                arena + header.name_offset,
                "cookie",
                6,
            ) == 0:
                _parse_cookie_line(
                    arena + header.value_offset,
                    <Py_ssize_t>header.value_length,
                    out,
                )

    def get(self, str name, default=None):
        """Value of ``name``, else ``default``. A name no request can carry is absent."""
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t n
        cdef object value
        try:
            _fold_header_name(name, buf, &n)
        except ValueError:
            return default
        value = self.c_value_str(self.c_find_n(buf, n))
        if value is None:
            return default
        return value

    def unsafe_get(self, name, default=None):
        cdef object value = self.c_get(name)
        if value is None:
            return default
        return value

    def set(self, str name, str value):
        _raise_readonly_request_headers()

    def add(self, str name, str value):
        _raise_readonly_request_headers()

    def remove(self, str name):
        _raise_readonly_request_headers()

    def getlist(self, str name):
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t n
        try:
            _fold_header_name(name, buf, &n)
        except ValueError:
            return []
        return [value.decode("latin-1") for value in self.c_getlist_n(buf, n)]

    def unsafe_getlist(self, name):
        cdef bytes key = name
        return self.c_getlist_n(<const char*>key, <Py_ssize_t>len(key))

    def items(self):
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in self.unsafe_items()
        ]

    def unsafe_items(self):
        cdef RawHeader* table = self._headers()
        cdef const char* arena = self._arena()
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef list result = []
        for index in range(self._count()):
            header = &table[index]
            result.append((
                PyBytes_FromStringAndSize(
                    arena + header.name_offset,
                    header.name_length,
                ),
                PyBytes_FromStringAndSize(
                    arena + header.value_offset,
                    header.value_length,
                ),
            ))
        return result

    def __contains__(self, name):
        cdef char buf[HEADER_NAME_STACK]
        cdef Py_ssize_t n
        try:
            _fold_header_name(name, buf, &n)
        except ValueError:
            return False
        return self.c_find_n(buf, n) >= 0

    def __len__(self):
        cdef RawHeader* table = self._headers()
        cdef const char* arena = self._arena()
        cdef RawHeader* header
        cdef Py_ssize_t index
        cdef set seen = set()
        for index in range(self._count()):
            header = &table[index]
            seen.add(
                _intern_name(
                    arena + header.name_offset,
                    header.name_length,
                )
            )
        return len(seen)

    def __bool__(self):
        return self._count() != 0

    def __repr__(self):
        return f"RequestHeaders({self.items()!r})"


cdef object RESPONSE_FINISHED_ERROR = (
    "This request has finished: its handler returned and the response is "
    "complete, so the writer can no longer send."
)


cdef inline void _raise_finished() except *:
    raise StarioRuntime(
        RESPONSE_FINISHED_ERROR,
        help_text=(
            "Finish the response before the handler returns. Background work "
            "must not write to a request whose handler already returned."
        ),
    )


@cython.final
@cython.freelist(64)
cdef class RequestHandle:
    """``c`` and ``w`` for one handler call (one object implements both).

    After the handler returns and the exchange is recycled, writes raise,
    ``end()`` / ``abort()`` are no-ops, and reads (``req``, ``match``,
    ``state``, ``span``, ``status_code``) keep describing this request.
    """

    def __cinit__(self):
        self._ex = None
        self._conn = None
        self._disc = None
        self._state = None
        self._req = None
        self._final_status = -1

    @property
    def req(self):
        cdef RequestExchange ex = self._ex
        if ex is None:
            return self._req
        return ex.ensure_request()

    @property
    def state(self):
        if self._state is None:
            self._state = {}
        return self._state

    @state.setter
    def state(self, value):
        self._state = value

    @property
    def disconnect(self):
        cdef RequestExchange ex = self._ex
        cdef object fut
        if ex is not None and (ex._http2 or ex._head_request):
            # Request-scoped: a reset stream goes away without the
            # connection, and a HEAD request ends with its response.
            fut = self._conn.ensure_stream_disconnect(ex)
            if ex._head_request and ex._completed and not fut.done():
                fut.set_result(None)
            return fut
        if self._conn is not None:
            return self._conn.ensure_disconnect()
        # Detached handle: _detach_views snapshotted the right future.
        return self._disc

    @property
    def disconnected(self):
        cdef RequestExchange ex = self._ex
        cdef object fut
        if ex is not None and (ex._http2 or ex._head_request):
            if ex._head_request and ex._completed:
                return True
            fut = ex._stream_disc
            if fut is not None and fut.done():
                return True
            if ex._abort_reason == ABORT_DISCONNECTED:
                return True
            return _connection_gone(self._conn)
        if self._conn is not None:
            return _connection_gone(self._conn)
        fut = self._disc
        return fut is None or fut.done()

    @property
    def shutting_down(self):
        return self.app.shutting_down

    @property
    def closing(self):
        cdef RequestExchange ex = self._ex
        cdef object transport
        if ex is None:
            return True
        # A reset stream sets this before the connection itself closes.
        if ex._abort_reason == ABORT_DISCONNECTED:
            return True
        # close() flips is_closing before connection_lost runs.
        transport = ex._transport
        if transport is not None and transport.is_closing():
            return True
        return self.disconnected or self.shutting_down

    def alive(self, source=None):
        return _Alive(self, source)

    @property
    def headers(self):
        cdef RequestExchange ex = self._ex
        if ex is None:
            _raise_finished()
        return ex.headers

    @property
    def status_code(self):
        cdef RequestExchange ex = self._ex
        cdef int status = self._final_status if ex is None else ex._status_code
        if status < 0:
            return None
        return status

    @property
    def started(self):
        cdef RequestExchange ex = self._ex
        if ex is None:
            return True
        return ex._status_code >= 0

    @property
    def completed(self):
        cdef RequestExchange ex = self._ex
        if ex is None:
            return True
        return ex._completed

    cpdef void respond(self, body, content_type, int status=200):
        cdef RequestExchange ex = self._ex
        if ex is None:
            _raise_finished()
        ex.respond(body, content_type, status)

    def text(self, body, status=200):
        """Send `body` as UTF-8 `text/plain` and finish the response."""
        self.respond(body.encode("utf-8"), b"text/plain; charset=utf-8", status)

    def json(self, value, status=200):
        """Serialize `value` to compact UTF-8 JSON and finish the response."""
        try:
            payload = _json_dumps_bytes(value)
        except (TypeError, ValueError) as exc:
            raise StarioError(
                "JSON response value is not serializable",
                context={"value": value},
                help_text="Pass dict, list, str, int, float, bool, or None values only.",
            ) from exc
        self.respond(payload, b"application/json; charset=utf-8", status)

    def html(self, content, status=200):
        """Send `bytes`, `str`, or markup nodes as `text/html`."""
        global _markup_render
        if isinstance(content, bytes):
            body = content
        elif isinstance(content, str):
            body = content.encode("utf-8")
        else:
            if _markup_render is None:
                from stario.markup.render import render_bytes

                _markup_render = render_bytes
            body = _markup_render(content)
        self.respond(body, b"text/html; charset=utf-8", status)

    def redirect(self, url, status=307):
        """Send a redirect with an empty body and a `Location` header."""
        if not 300 <= status < 400:
            raise StarioError(
                "Redirect status must be a 3xx status code",
                context={"status": status},
                help_text=(
                    "Use w.text/w.json/w.html for response bodies, "
                    "or pass a 3xx redirect status."
                ),
            )
        self.headers.set("location", normalized_location(url))
        self.respond(b"", b"text/plain; charset=utf-8", status)

    def empty(self, status=204):
        """Finish with no message body (default `204 No Content`)."""
        self.respond(b"", b"text/plain; charset=utf-8", status)

    def write_headers(self, int status_code):
        cdef RequestExchange ex = self._ex
        if ex is None:
            _raise_finished()
        ex.write_headers(status_code)
        return self

    def write(self, data):
        cdef RequestExchange ex = self._ex
        if ex is None:
            _raise_finished()
        ex.write(data)
        return self

    async def drain(self):
        cdef RequestExchange ex = self._ex
        cdef object waiter
        if ex is None:
            return
        waiter = ex._connection.drain_waiter(ex)
        while waiter is not None:
            await waiter
            ex = self._ex
            if ex is None:
                return
            waiter = ex._connection.drain_waiter(ex)

    def end(self, data=None):
        cdef RequestExchange ex = self._ex
        if ex is None:
            return
        ex.end(data)

    def abort(self):
        cdef RequestExchange ex = self._ex
        if ex is None:
            return
        ex.abort()

    @property
    def _exchange_id(self):
        """``id()`` of the pooled exchange while the handler runs (tests only)."""
        return None if self._ex is None else id(self._ex)

    def __repr__(self):
        state = "finished" if self._ex is None else "active"
        return f"<RequestHandle {state} match={self.match!r}>"


cdef RequestExchange acquire_exchange(
    HttpConnection connection,
    object app,
    object transport,
    object compression,
    int max_body_size,
    double body_timeout,
):
    cdef RequestExchange exchange
    cdef list pool = _thread_pool()
    if pool:
        exchange = pool.pop()
    else:
        exchange = RequestExchange()
    exchange.reset(
        connection,
        app,
        transport,
        compression,
        max_body_size,
        body_timeout,
    )
    return exchange
