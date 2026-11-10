# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, freethreading_compatible=True
# cython: auto_pickle=False
"""asyncio protocol: HTTP/1 (llhttp) and HTTP/2 (nghttp2).

``HttpProtocol`` owns the socket, parser session, pause/resume, pipeline
queue, and timeouts. Per-request flags live on ``RequestExchange``. The
handler sees ``RequestHandle`` (``c`` / ``w``), not the pooled exchange.

H1 vs H2 is chosen once (TLS ALPN or the cleartext H2 preface). Timeouts:
``Connections.sweep`` compares stored deadlines once a second.
"""

import asyncio
import logging
from collections import deque
from threading import Lock

from libc.stddef cimport size_t
from libc.stdint cimport int32_t, uint8_t, uint16_t, uint32_t, uint64_t
from libc.stdlib cimport free, malloc
from libc.string cimport memchr, memcmp, memcpy, memmove, strlen
from cpython.bytearray cimport (
    PyByteArray_AS_STRING,
    PyByteArray_Check,
    PyByteArray_FromStringAndSize,
    PyByteArray_GET_SIZE,
    PyByteArray_Resize,
)
from cpython.bytes cimport (
    PyBytes_AS_STRING,
    PyBytes_Check,
    PyBytes_FromStringAndSize,
    PyBytes_GET_SIZE,
)
from cpython.exc cimport PyErr_Clear
from cpython.object cimport PyObject_Call
from cpython.unicode cimport PyUnicode_DecodeLatin1

from stario._env import env_bool
from stario.http.config import (
    DEFAULT_HEADER_TIMEOUT,
    DEFAULT_KEEP_ALIVE_TIMEOUT,
    DEFAULT_MAX_PIPELINED_REQUESTS,
    DEFAULT_WRITE_TIMEOUT,
)
from stario.http.invoke import finish_request_span
from stario.http.request import (
    DEFAULT_BODY_TIMEOUT,
    DEFAULT_MAX_BODY_SIZE,
    DEFAULT_MAX_HEADER_BYTES,
)
from stario.telemetry.noop import NoOpTracer

from stario_cython.exchange cimport (
    ABORT_TOO_LARGE,
    BODY_LIMIT_MAX,
    SMALL_BODY,
    PATH_BAD,
    PATH_HAS_PCT,
    PATH_OPTIONS_STAR,
    PATH_REDIRECT,
    AppState,
    CRouter,
    HttpConnection,
    Headers,
    RequestExchange,
    RequestHandle,
    _host_without_port_n,
    wake_waiters,
    acquire_exchange,
    canonical_path,
    decode_path_full,
    host_value_ok,
    query_has_nul,
    scan_request_path,
    _status_line,
)

cdef extern from *:
    """
    #include <stdio.h>
    #include <time.h>

    /* Wall-clock second for the Date header. The coarse clock is a few ns
       (no syscall); macOS has no coarse variant, and the plain one is ~30 ns.
       Windows has no clock_gettime; time() reads the system time without a
       kernel transition. ``long long``: Windows ``long`` is 32 bits. */
    static long long stario_wall_second(void) {
    #if defined(_WIN32)
        return (long long)time(NULL);
    #else
        struct timespec ts;
    #if defined(CLOCK_REALTIME_COARSE)
        clock_gettime(CLOCK_REALTIME_COARSE, &ts);
    #else
        clock_gettime(CLOCK_REALTIME, &ts);
    #endif
        return (long long)ts.tv_sec;
    #endif
    }

    /* ``date: Tue, 18 Aug 2026 00:00:00 GMT\\r\\n`` (RFC 9110 IMF-fixdate). */
    static int stario_date_line(long long second, char* out, size_t cap) {
        static const char days[] = "SunMonTueWedThuFriSat";
        static const char months[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
        struct tm parts;
        time_t t = (time_t)second;
    #if defined(_WIN32)
        if (gmtime_s(&parts, &t) != 0) {
            return -1;
        }
    #else
        if (gmtime_r(&t, &parts) == NULL) {
            return -1;
        }
    #endif
        return snprintf(
            out, cap, "date: %.3s, %02d %.3s %04d %02d:%02d:%02d GMT\\r\\n",
            days + parts.tm_wday * 3, parts.tm_mday, months + parts.tm_mon * 3,
            parts.tm_year + 1900, parts.tm_hour, parts.tm_min, parts.tm_sec
        );
    }

    /* glibc only. Keeps 128 KiB - 64 MiB buffers (bodies, compressor
       scratch, compressed output) on the heap instead of mmap/munmap per
       allocation, and stops the heap top being trimmed back after each
       large response. Process-wide, and per malloc arena (one per thread
       that allocates): STARIO_MALLOC_TUNE=0 turns it off. */
    #if defined(__linux__)
    #include <malloc.h>
    static void stario_malloc_tune(void) {
        static int once;
        if (once) {
            return;
        }
        once = 1;
        mallopt(M_MMAP_THRESHOLD, 64 * 1024 * 1024);
        mallopt(M_TRIM_THRESHOLD, 64 * 1024 * 1024);
    }
    #else
    static void stario_malloc_tune(void) {}
    #endif
    """
    void stario_malloc_tune() noexcept
    long long stario_wall_second() noexcept nogil
    int stario_date_line(long long second, char* out, size_t cap) noexcept nogil
from stario_cython.llhttp cimport *
from stario_cython.nghttp2 cimport (
    ssize_t,
    NGHTTP2_DATA,
    NGHTTP2_DATA_FLAG_EOF,
    NGHTTP2_DATA_FLAG_NO_COPY,
    NGHTTP2_ERR_CALLBACK_FAILURE,
    NGHTTP2_ERR_DEFERRED,
    NGHTTP2_ERR_TEMPORAL_CALLBACK_FAILURE,
    NGHTTP2_FLAG_ACK,
    NGHTTP2_FLAG_END_STREAM,
    NGHTTP2_FLAG_NONE,
    NGHTTP2_HEADERS,
    NGHTTP2_PING,
    NGHTTP2_INTERNAL_ERROR,
    NGHTTP2_PROTOCOL_ERROR,
    NGHTTP2_CANCEL,
    NGHTTP2_ENHANCE_YOUR_CALM,
    NGHTTP2_NO_ERROR,
    NGHTTP2_NV_FLAG_NONE,
    NGHTTP2_SETTINGS_ENABLE_PUSH,
    NGHTTP2_SETTINGS_HEADER_TABLE_SIZE,
    NGHTTP2_SETTINGS_INITIAL_WINDOW_SIZE,
    NGHTTP2_SETTINGS_MAX_CONCURRENT_STREAMS,
    NGHTTP2_SETTINGS_MAX_HEADER_LIST_SIZE,
    NGHTTP2_SETTINGS_NO_RFC7540_PRIORITIES,
    nghttp2_data_provider,
    nghttp2_data_source,
    nghttp2_frame,
    nghttp2_nv,
    nghttp2_option,
    nghttp2_option_del,
    nghttp2_option_new,
    nghttp2_option_set_no_auto_window_update,
    nghttp2_option_set_no_closed_streams,
    nghttp2_option_set_max_continuations,
    nghttp2_option_set_stream_reset_rate_limit,
    nghttp2_session,
    nghttp2_session_callbacks,
    nghttp2_session_callbacks_del,
    nghttp2_session_callbacks_new,
    nghttp2_session_callbacks_set_on_begin_headers_callback,
    nghttp2_session_callbacks_set_on_data_chunk_recv_callback,
    nghttp2_session_callbacks_set_on_frame_recv_callback,
    nghttp2_session_callbacks_set_on_frame_send_callback,
    nghttp2_session_callbacks_set_on_header_callback,
    nghttp2_session_callbacks_set_on_stream_close_callback,
    nghttp2_session_callbacks_set_send_data_callback,
    nghttp2_session_del,
    nghttp2_session_get_effective_recv_data_length,
    nghttp2_session_get_last_proc_stream_id,
    nghttp2_session_get_local_window_size,
    nghttp2_session_get_stream_effective_recv_data_length,
    nghttp2_session_get_stream_local_window_size,
    nghttp2_session_get_stream_user_data,
    nghttp2_session_mem_recv,
    nghttp2_session_mem_send,
    nghttp2_session_resume_data,
    nghttp2_session_server_new2,
    nghttp2_session_set_local_window_size,
    nghttp2_session_set_stream_user_data,
    nghttp2_session_want_read,
    nghttp2_session_get_stream_remote_close,
    nghttp2_session_want_write,
    nghttp2_settings_entry,
    nghttp2_strerror,
    nghttp2_session_terminate_session,
    nghttp2_submit_goaway,
    nghttp2_submit_headers,
    nghttp2_submit_ping,
    nghttp2_submit_response,
    nghttp2_submit_rst_stream,
    nghttp2_submit_settings,
    nghttp2_submit_shutdown_notice,
    nghttp2_submit_window_update,
)
cdef enum:
    PARSE_NONE = 0
    PARSE_H1 = 1
    PARSE_H2 = 2
    H2_PREFACE_LEN = 24
    # Receive windows: default 64KiB stalls a multiplexed POST and
    # auto-WINDOW_UPDATE emits a frame per DATA. 1MiB/4MiB plus batched
    # consume keeps credit up without a frame per request.
    H2_STREAM_WINDOW = 1024 * 1024
    H2_CONN_WINDOW = 4 * 1024 * 1024
    H2_STREAM_UPDATE_THRESH = 256 * 1024
    H2_CONN_UPDATE_THRESH = 512 * 1024
    H2_MAX_CONCURRENT = 256
    # Rapid-reset: nghttp2 defaults 1000 burst / 33 per second.
    # Keep the refill, shrink the bucket.
    H2_RST_BURST = 100
    H2_RST_RATE = 33
    # RFC 7541 / nghttp2 default. Advertising 0 is rejected by some
    # intermediaries; 4KiB is the spec default.
    H2_HEADER_TABLE_SIZE = 4096
    # w.drain() on HTTP/2 waits while a stream has more unsent DATA than
    # this, and wakes once nghttp2 has sent it down to the low mark.
    H2_DRAIN_HIGH = 256 * 1024
    H2_DRAIN_LOW = 64 * 1024
    # DATA frames up to this size are copied into the outbound scratch with
    # their header; bigger ones go out zero-copy as a memoryview.
    H2_INLINE_DATA_MAX = 4096

cdef bytes H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
cdef object _settings_lock = Lock()
cdef object _asyncio_Task = asyncio.Task
cdef object _task_done = asyncio.Task.done
cdef object _log = logging.getLogger("stario.http")


cdef object _span_path(const char* url, Py_ssize_t n):
    """Best-effort decoded path of a raw request-target, for telemetry only."""
    cdef const char* q = <const char*>memchr(url, 63, <size_t>n)
    cdef Py_ssize_t end = n if q == NULL else <Py_ssize_t>(q - url)
    cdef object out = decode_path_full(url, end)
    if out is None:
        out = PyUnicode_DecodeLatin1(url, end, NULL)
    return out


cdef inline bint _task_clean(object task):
    """A done task that neither raised nor was cancelled.

    One ``result()`` call instead of ``cancelled()`` plus ``exception()``;
    either way the outcome counts as retrieved.
    """
    try:
        task.result()
    except BaseException:
        return False
    return True


def _cancel_unless_cancelling(task):
    """Cancel ``task`` unless an ``alive()`` cancel is already in flight."""
    if not task.done() and task.cancelling() == 0:
        task.cancel()


cdef Py_ssize_t _h2_unsent(RequestExchange ex) noexcept:
    """DATA bytes a stream has queued but nghttp2 has not sent yet."""
    cdef object pending = ex._h2_pending
    if PyBytes_Check(pending):
        return PyBytes_GET_SIZE(pending) - ex._h2_pending_off
    if PyByteArray_Check(pending):
        return PyByteArray_GET_SIZE(pending) - ex._h2_pending_off
    return 0


cdef inline bint _as_buf(object data, const char** ptr, Py_ssize_t* n) noexcept:
    if PyBytes_Check(data):
        ptr[0] = PyBytes_AS_STRING(data)
        n[0] = PyBytes_GET_SIZE(data)
        return True
    if PyByteArray_Check(data):
        ptr[0] = PyByteArray_AS_STRING(data)
        n[0] = PyByteArray_GET_SIZE(data)
        return True
    return False


cdef int _preface_append(object hold, const char* p, Py_ssize_t n) noexcept:
    cdef Py_ssize_t old
    if n <= 0:
        return 0
    old = PyByteArray_GET_SIZE(hold)
    if PyByteArray_Resize(hold, old + n) < 0:
        PyErr_Clear()
        return -1
    memcpy(PyByteArray_AS_STRING(hold) + old, p, <size_t>n)
    return 0


cdef bint _absolute_form_authority(
    const char* url,
    Py_ssize_t n,
    Py_ssize_t* auth_start,
    Py_ssize_t* auth_end,
) noexcept:
    """``http://`` / ``https://`` (any case): authority span up to the path."""
    cdef Py_ssize_t i
    cdef Py_ssize_t scheme_n
    if n >= 7 and _ascii_ieq(url, "http://", 7):
        scheme_n = 7
    elif n >= 8 and _ascii_ieq(url, "https://", 8):
        scheme_n = 8
    else:
        return False
    i = scheme_n
    while i < n and url[i] != 47:
        i += 1
    auth_start[0] = scheme_n
    auth_end[0] = i
    return True


cdef inline bint _ascii_ieq(const char* s, const char* lower, Py_ssize_t n) noexcept:
    cdef Py_ssize_t i
    cdef unsigned char c
    for i in range(n):
        c = <unsigned char>s[i]
        if 65 <= c <= 90:
            c += 32
        if c != <unsigned char>lower[i]:
            return False
    return True


cdef int F_CHUNKED = 0x8
cdef int F_CONTENT_LENGTH = 0x20
cdef int F_TRANSFER_ENCODING = 0x200
cdef int PAUSE_WRITE = 1
cdef int PAUSE_PIPELINE = 2
cdef int PAUSE_BODY = 4
# Set once a connection-closing error waits behind earlier responses.
cdef int PAUSE_REJECTED = 8
# Per-field overhead in the header budget (RFC 7541 §4.1 counts 32 octets),
# so a byte limit also bounds how many fields a request can carry.
cdef int HEADER_FIELD_OVERHEAD = 32
# Wait for the shutdown-notice PING ACK before the final GOAWAY (one RTT).
cdef double H2_GOAWAY_GRACE = 1.0
# HTTP/1 lingering close: read and drop input this long after the FIN.
cdef double LINGER_TIME = 2.0
cdef int PARSER_QUANTUM = 512 * 1024
cdef int TIMEOUT_NONE = 0
cdef int TIMEOUT_HEADER = 1
cdef int TIMEOUT_IDLE = 2
# Deferred small request bodies: body_timeout with progress resets.
cdef int TIMEOUT_BODY = 3

cdef bytes H1_OVERLOADED = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"connection: close\r\n"
    b"content-length: 0\r\n"
    b"retry-after: 1\r\n"
    b"\r\n"
)
cdef int CLEANUP_OFF = 0
cdef int CLEANUP_SWEEP = 1

cdef object METH_DELETE = "DELETE"
cdef object METH_GET = "GET"
cdef object METH_HEAD = "HEAD"
cdef object METH_POST = "POST"
cdef object METH_PUT = "PUT"
cdef object METH_CONNECT = "CONNECT"
cdef object METH_OPTIONS = "OPTIONS"
cdef object METH_TRACE = "TRACE"
cdef object METH_PATCH = "PATCH"
cdef object METH_QUERY = "QUERY"
cdef object VER_10 = "1.0"
cdef object VER_11 = "1.1"
cdef object VER_20 = "2"

cdef llhttp_settings_t* _SETTINGS = NULL


cdef object _version_str(int major, int minor):
    if major == 2:
        return VER_20
    if major == 1 and minor == 1:
        return VER_11
    if major == 1 and minor == 0:
        return VER_10
    return "%d.%d" % (major, minor)


cdef inline bint _h2_header_name_ok(const char* p, size_t n) noexcept nogil:
    cdef size_t i
    cdef unsigned char c
    if n == 0:
        return False
    for i in range(n):
        c = <unsigned char>p[i]
        if c <= 32 or c >= 127 or (65 <= c <= 90):
            return False
    return True


cdef object _method_from_bytes(const char* p, size_t n):
    if n == 3:
        if memcmp(p, "GET", 3) == 0:
            return METH_GET
        if memcmp(p, "PUT", 3) == 0:
            return METH_PUT
    elif n == 4:
        if memcmp(p, "POST", 4) == 0:
            return METH_POST
        if memcmp(p, "HEAD", 4) == 0:
            return METH_HEAD
    elif n == 5:
        if memcmp(p, "PATCH", 5) == 0:
            return METH_PATCH
        if memcmp(p, "TRACE", 5) == 0:
            return METH_TRACE
        if memcmp(p, "QUERY", 5) == 0:
            return METH_QUERY
    elif n == 6:
        if memcmp(p, "DELETE", 6) == 0:
            return METH_DELETE
    elif n == 7:
        if memcmp(p, "OPTIONS", 7) == 0:
            return METH_OPTIONS
        if memcmp(p, "CONNECT", 7) == 0:
            return METH_CONNECT
    return PyUnicode_DecodeLatin1(p, <Py_ssize_t>n, NULL)


cdef object _method_str(int method):
    cdef const char* name = llhttp_method_name(method)
    if name == NULL:
        return METH_GET
    return _method_from_bytes(name, strlen(name))


cdef void _bind_settings():
    global _SETTINGS
    cdef llhttp_settings_t* settings
    if _SETTINGS != NULL:
        return
    _settings_lock.acquire()
    try:
        if _SETTINGS != NULL:
            return
        settings = stario_settings_new()
        if settings == NULL:
            raise MemoryError()
        settings.on_message_begin = _cb_message_begin
        settings.on_url = _cb_url
        settings.on_header_field = _cb_header_field
        settings.on_header_value = _cb_header_value
        settings.on_header_value_complete = _cb_header_value_complete
        settings.on_headers_complete = _cb_headers_complete
        settings.on_body = _cb_body
        settings.on_message_complete = _cb_message_complete
        _SETTINGS = settings
        if env_bool("STARIO_MALLOC_TUNE", True):
            stario_malloc_tune()
    finally:
        _settings_lock.release()


cdef int _cb_message_begin(llhttp_t* parser) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>stario_parser_get_data(parser)
    proto._on_message_begin()
    return -1 if proto.rejected else 0


cdef int _cb_url(llhttp_t* parser, const char* at, size_t length) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>stario_parser_get_data(parser)
    proto._on_url(at, length)
    return -1 if proto.rejected else 0


cdef int _cb_header_field(llhttp_t* parser, const char* at, size_t length) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>stario_parser_get_data(parser)
    proto._on_header_field(at, length)
    return -1 if proto.rejected else 0


cdef int _cb_header_value(llhttp_t* parser, const char* at, size_t length) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>stario_parser_get_data(parser)
    proto._on_header_value(at, length)
    return -1 if proto.rejected else 0


cdef int _cb_header_value_complete(llhttp_t* parser) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>stario_parser_get_data(parser)
    proto._on_header_value_complete()
    return -1 if proto.rejected else 0


cdef int _cb_headers_complete(llhttp_t* parser) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>stario_parser_get_data(parser)
    proto._on_headers_complete()
    return -1 if proto.rejected else 0


cdef int _cb_body(llhttp_t* parser, const char* at, size_t length) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>stario_parser_get_data(parser)
    proto._on_body(at, length)
    return -1 if proto.rejected else 0


cdef int _cb_message_complete(llhttp_t* parser) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>stario_parser_get_data(parser)
    proto._on_message_complete()
    return -1 if proto.rejected else 0


cdef int _h2_on_begin_headers(
    nghttp2_session* session,
    const nghttp2_frame* frame,
    void* user_data,
) noexcept:
    cdef CHttpProtocol proto
    try:
        if frame.hd.type != NGHTTP2_HEADERS:
            return 0
        proto = <CHttpProtocol>user_data
        proto._h2_begin_stream(frame.hd.stream_id)
        return 0
    except Exception:
        _log.exception("HTTP/2 callback failed; closing the connection")
        return NGHTTP2_ERR_CALLBACK_FAILURE


cdef int _h2_on_header(
    nghttp2_session* session,
    const nghttp2_frame* frame,
    const uint8_t* name,
    size_t namelen,
    const uint8_t* value,
    size_t valuelen,
    uint8_t flags,
    void* user_data,
) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>user_data
    try:
        if proto._h2_on_header(frame.hd.stream_id, name, namelen, value, valuelen):
            # nghttp2 resets the stream and skips the rest of the block
            # without validating it. No client sends such a block by
            # accident, and each one still cost a decode up to the cutoff:
            # they draw from the reset budget too.
            proto._h2_count_cancelled_handler()
            return NGHTTP2_ERR_TEMPORAL_CALLBACK_FAILURE
    except Exception:
        _log.exception("HTTP/2 stream callback failed; resetting the stream")
        return NGHTTP2_ERR_TEMPORAL_CALLBACK_FAILURE
    return 0


cdef int _h2_on_data(
    nghttp2_session* session,
    uint8_t flags,
    int32_t stream_id,
    const uint8_t* data,
    size_t length,
    void* user_data,
) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>user_data
    try:
        proto._h2_on_data(stream_id, data, length)
    except Exception:
        _log.exception("HTTP/2 stream callback failed; resetting the stream")
        return NGHTTP2_ERR_TEMPORAL_CALLBACK_FAILURE
    return 0


cdef int _h2_on_frame_recv(
    nghttp2_session* session,
    const nghttp2_frame* frame,
    void* user_data,
) noexcept:
    cdef CHttpProtocol proto
    cdef uint8_t ftype
    try:
        proto = <CHttpProtocol>user_data
        ftype = frame.hd.type
        if ftype == NGHTTP2_HEADERS:
            proto._h2_headers_frame(
                frame.hd.stream_id, (frame.hd.flags & NGHTTP2_FLAG_END_STREAM) != 0
            )
        elif ftype == NGHTTP2_DATA and (frame.hd.flags & NGHTTP2_FLAG_END_STREAM):
            proto._h2_end_stream(frame.hd.stream_id)
        elif (
            ftype == NGHTTP2_PING
            and (frame.hd.flags & NGHTTP2_FLAG_ACK)
            and proto.h2_goaway_pending
        ):
            proto._h2_final_goaway()
        return 0
    except Exception:
        _log.exception("HTTP/2 callback failed; closing the connection")
        return NGHTTP2_ERR_CALLBACK_FAILURE


cdef int _h2_on_frame_send(
    nghttp2_session* session,
    const nghttp2_frame* frame,
    void* user_data,
) noexcept:
    cdef CHttpProtocol proto
    cdef uint8_t ftype = frame.hd.type
    if not (frame.hd.flags & NGHTTP2_FLAG_END_STREAM):
        return 0
    if ftype != NGHTTP2_HEADERS and ftype != NGHTTP2_DATA:
        return 0
    try:
        proto = <CHttpProtocol>user_data
        proto._h2_response_sent(frame.hd.stream_id)
        return 0
    except Exception:
        _log.exception("HTTP/2 callback failed; closing the connection")
        return NGHTTP2_ERR_CALLBACK_FAILURE


cdef int _h2_on_stream_close(
    nghttp2_session* session,
    int32_t stream_id,
    uint32_t error_code,
    void* user_data,
) noexcept:
    cdef CHttpProtocol proto
    try:
        proto = <CHttpProtocol>user_data
        proto._h2_stream_closed(stream_id)
        return 0
    except Exception:
        _log.exception("HTTP/2 callback failed; closing the connection")
        return NGHTTP2_ERR_CALLBACK_FAILURE


cdef ssize_t _h2_data_source_read(
    nghttp2_session* session,
    int32_t stream_id,
    uint8_t* buf,
    size_t length,
    uint32_t* data_flags,
    nghttp2_data_source* source,
    void* user_data,
) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>user_data
    cdef RequestExchange ex
    if source != NULL and source.ptr != NULL:
        ex = <RequestExchange>source.ptr
        return proto._h2_read_data(ex, stream_id, buf, length, data_flags)
    return NGHTTP2_ERR_CALLBACK_FAILURE


cdef int _h2_send_data(
    nghttp2_session* session,
    nghttp2_frame* frame,
    const uint8_t* framehd,
    size_t length,
    nghttp2_data_source* source,
    void* user_data,
) noexcept:
    cdef CHttpProtocol proto = <CHttpProtocol>user_data
    return proto._h2_send_data_frame(frame, framehd, length, source)


cdef class Connections:
    """One event loop's live connections, their timeout tick, and the clock.

    The ``Server`` creates one per loop and hands it to every protocol on
    that loop. It is the only thing that reads a clock:

    - ``tick()`` runs from one ``loop.call_later`` handle while a connection
      with timeouts exists, reads ``loop.time()`` once, and walks the live
      list in C comparing stored deadlines (``sweep``). No Task, no wakeup
      when idle, no Python call per connection.
    - ``c_date()`` is the ``date: ...\\r\\n`` line for responses: one coarse
      wall-clock read per call, reformatted only when the second changed.
      ``pin_date()`` fixes it for tests.

    Nothing here is shared between loops, so nothing is locked. Iterable and
    sized like the set it replaces.
    """

    cdef list _live
    cdef Py_ssize_t _timed
    cdef object _tick_handle
    cdef object _loop
    cdef double _interval
    # ``loop.time()`` at the last tick, for anything that wants a cheap,
    # slightly stale clock.
    cdef public double now
    cdef long long _date_second
    cdef object _date_line
    cdef bint _date_fixed
    # 0 means unlimited; `rejected` counts connections refused at the cap.
    cdef public Py_ssize_t max_connections
    cdef public Py_ssize_t rejected
    # ``Task(coro, loop=..., eager_start=...)`` kwargs, built once per loop
    # and borrowed by every protocol on it (``_bind_task_kwargs``).
    cdef object _task_loop
    cdef dict _task_kw_eager
    cdef dict _task_kw_lazy

    def __cinit__(self):
        self._live = []
        self._timed = 0
        self._tick_handle = None
        self._loop = None
        self._interval = 1.0
        self.now = 0.0
        self._date_second = -1
        self._date_line = b""
        self._date_fixed = False
        self.max_connections = 0
        self.rejected = 0
        self._task_loop = None
        self._task_kw_eager = None
        self._task_kw_lazy = None

    def __init__(self, *, sweep_interval=1.0, max_connections=0, date=None):
        interval = float(sweep_interval)
        if interval < 0.001:
            interval = 0.001
        self._interval = interval
        self.max_connections = max(int(max_connections), 0)
        if date is not None:
            self.pin_date(date)

    def pin_date(self, bytes line not None):
        """Serve ``line`` (``date: ...\\r\\n``) on every response; tests only."""
        if not line.endswith(b"\r\n"):
            raise ValueError("date line must end with CRLF")
        self._date_line = line
        self._date_fixed = True

    cdef inline object c_date(self):
        cdef long long second
        if self._date_fixed:
            return self._date_line
        second = stario_wall_second()
        if second != self._date_second:
            self._refresh_date(second)
        return self._date_line

    cdef void _refresh_date(self, long long second):
        cdef char buf[48]
        cdef int n = stario_date_line(second, buf, sizeof(buf))
        if n > 0 and n < <int>sizeof(buf):
            self._date_line = PyBytes_FromStringAndSize(buf, n)
            self._date_second = second

    @property
    def date_line(self):
        """The current ``date: ...\\r\\n`` line."""
        return self.c_date()

    def add(self, proto):
        if not isinstance(proto, CHttpProtocol):
            raise TypeError("Connections holds HttpProtocol objects")
        self._connection_opened(<CHttpProtocol>proto)

    def discard(self, proto):
        if isinstance(proto, CHttpProtocol):
            self._connection_closed(<CHttpProtocol>proto)

    def __len__(self):
        return len(self._live)

    def __iter__(self):
        # A snapshot: a close inside the loop body unregisters from the list.
        return iter(list(self._live))

    def __contains__(self, proto):
        cdef Py_ssize_t i
        if not isinstance(proto, CHttpProtocol):
            return False
        i = (<CHttpProtocol>proto)._live_index
        return i >= 0 and i < len(self._live) and self._live[i] is proto

    def __bool__(self):
        return len(self._live) > 0

    @property
    def sweep_interval(self):
        return self._interval

    @property
    def sweeping(self):
        """True while a tick is scheduled (a connection with timeouts exists)."""
        return self._tick_handle is not None

    cpdef void sweep(self, double now):
        """Compare every connection's stored deadlines to ``now``."""
        cdef Py_ssize_t i = 0
        cdef list live = self._live
        cdef CHttpProtocol proto
        self.now = now
        # A firing timeout may close the connection, which swaps the last
        # entry into this slot: only advance when the slot is unchanged.
        while i < len(live):
            proto = <CHttpProtocol>live[i]
            try:
                proto._check_timeouts(now)
            except Exception:
                # One connection's failure must not leave the rest unchecked.
                _log.exception("Timeout sweep failed for a connection")
            if i < len(live) and live[i] is proto:
                i += 1

    def _tick(self):
        self._tick_handle = None
        try:
            self.sweep(self._loop.time())
        finally:
            # A failing sweep must not stop every later timeout on this loop.
            if self._timed > 0:
                self._schedule()

    cdef void _schedule(self):
        self._tick_handle = self._loop.call_later(self._interval, self._tick)

    cdef void _bind_task_kwargs(self, object loop):
        """Build the task kwargs for ``loop`` once; later protocols reuse them."""
        if self._task_loop is not loop:
            self._task_loop = loop
            self._task_kw_eager = {"loop": loop, "eager_start": True}
            self._task_kw_lazy = {"loop": loop, "eager_start": False}

    cdef void _connection_opened(self, CHttpProtocol proto):
        if proto._live_index >= 0:
            return
        proto._live_index = len(self._live)
        self._live.append(proto)
        if proto.timeout_cleanup != CLEANUP_SWEEP:
            return
        self._timed += 1
        if self._tick_handle is None:
            self._loop = proto.loop
            self._schedule()

    cdef void _connection_closed(self, CHttpProtocol proto):
        cdef Py_ssize_t i = proto._live_index
        cdef CHttpProtocol last
        if i < 0:
            return
        proto._live_index = -1
        last = <CHttpProtocol>self._live[len(self._live) - 1]
        if last is not proto:
            self._live[i] = last
            last._live_index = i
        self._live.pop()
        if proto.timeout_cleanup != CLEANUP_SWEEP:
            return
        self._timed -= 1
        if self._timed == 0 and self._tick_handle is not None:
            self._tick_handle.cancel()
            self._tick_handle = None


cdef class CHttpProtocol(HttpConnection):
    """asyncio connection: socket, parser, pipeline queue, and timeouts."""

    cdef llhttp_t* parser
    cdef public object loop
    cdef object app
    cdef object tracer
    cdef object noop_span
    cdef object compression
    cdef Connections connections
    # Slot in ``connections``' live list, -1 when not registered.
    cdef Py_ssize_t _live_index
    # This thread's ``app.tasks`` set, bound once: handler Tasks that did not
    # finish eagerly join it so shutdown drain can await them.
    cdef set _tasks
    cdef public object transport
    cdef public object disconnect
    cdef public bint closed
    # The handler Task of the most recent dispatch (done or not). TestClient
    # reads it right after feeding one request; one pointer store per request.
    cdef public object last_task
    cdef RequestExchange reading_exchange
    cdef RequestExchange active_exchange
    cdef RequestExchange idle_exchange
    cdef object pending_exchanges
    cdef int head_bytes
    cdef int max_header_bytes
    cdef int max_body_bytes
    cdef int max_pipelined_requests
    cdef double header_timeout
    cdef double keep_alive_timeout
    cdef double body_timeout
    cdef double write_timeout
    # 0.0 = arm on the next sweep while output is stuck.
    cdef double write_stall_deadline
    cdef Py_ssize_t write_stall_size
    cdef int timeout_kind
    cdef double timeout_deadline
    cdef bint header_timeout_reset
    cdef bint rejected
    cdef bint request_dispatched
    cdef int pause_reasons
    cdef object body_pause_owner
    cdef object held_data
    cdef Py_ssize_t held_offset
    cdef bint pump_scheduled
    cdef AppState _app_state
    cdef object _in_buf
    cdef object _in_view
    cdef public int parse_mode
    cdef bytearray preface_hold
    cdef nghttp2_session* h2
    cdef dict h2_streams
    cdef object h2_out
    cdef Py_ssize_t h2_out_used
    # Output of one ``_h2_send`` pass: ``h2_out`` slices and zero-copy DATA
    # payloads, in frame order, written with one ``writelines`` at the end.
    cdef list h2_batch
    cdef bint h2_in_recv
    # nghttp2 forbids mem_send from its own callbacks; the outer loop drains.
    cdef bint h2_in_send
    cdef bint h2_goaway_sent
    # Shutdown notice sent; the final GOAWAY waits for our PING's ACK.
    cdef bint h2_goaway_pending
    # 0.0 = arm on the next tick; the final GOAWAY goes out at the deadline.
    cdef double h2_goaway_deadline
    cdef double h2_cancel_tokens
    cdef double h2_cancel_ts
    # Streams holding their WINDOW_UPDATE (body backpressure).
    cdef int h2_paused_streams
    # Set while a lingering close drops input (``close_lingering``); the
    # socket closes at ``linger_deadline`` (0.0 = arm on the next tick).
    cdef bint lingering
    cdef double linger_deadline
    cdef bint h1_headers_too_large
    # Set at headers-complete: later header callbacks are chunked trailers.
    cdef bint h1_in_trailers
    cdef int deferred_status
    cdef list _write_waiters
    # Bound once per connection: skips a method lookup on every request.
    cdef object _t_is_closing
    # Borrowed from ``Connections``: one pair of dicts per loop.
    cdef dict _task_kw_eager
    cdef dict _task_kw_lazy
    cdef object _task_factory
    cdef object deferred_body
    cdef bint deferred_head

    def __cinit__(self):
        _bind_settings()
        self.parser = stario_parser_new()
        if self.parser == NULL:
            raise MemoryError()
        llhttp_init(self.parser, HTTP_REQUEST, _SETTINGS)
        stario_parser_set_data(self.parser, <void*>self)
        self.closed = False
        self.disconnect = None
        self.reading_exchange = None
        self.active_exchange = None
        self.idle_exchange = None
        self.pause_reasons = 0
        self.body_pause_owner = None
        self.held_data = None
        self.held_offset = 0
        self.pump_scheduled = False
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0
        self.timeout_cleanup = CLEANUP_SWEEP
        self.header_timeout_reset = False
        self.parse_mode = PARSE_NONE
        self.preface_hold = bytearray()
        self.h2 = NULL
        self.h2_streams = {}
        self.h2_out = bytearray()
        self.h2_batch = []
        self.h2_paused_streams = 0
        self.h2_out_used = 0
        self.h2_in_recv = False
        self.h2_in_send = False
        self.h2_cancel_tokens = H2_RST_BURST
        self.h2_cancel_ts = 0.0
        self.h2_goaway_sent = False
        self.h2_goaway_pending = False
        self.h2_goaway_deadline = 0.0
        self.lingering = False
        self.linger_deadline = 0.0
        self._live_index = -1
        self.h1_headers_too_large = False
        self.deferred_status = 0
        self.deferred_body = None
        self.deferred_head = False
        self._write_waiters = []
        self._t_is_closing = None
        self._task_kw_eager = None
        self._task_kw_lazy = None
        self._task_factory = None
        self._app_state = None
        self._tasks = None
        self.last_task = None
        self._in_buf = None
        self._in_view = None

    def __dealloc__(self):
        if self.parser != NULL:
            stario_parser_del(self.parser)
            self.parser = NULL
        if self.h2 != NULL:
            nghttp2_session_del(self.h2)
            self.h2 = NULL

    def __init__(
        self,
        loop,
        app,
        tracer,
        compression,
        connections,
        max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
        max_body_bytes=DEFAULT_MAX_BODY_SIZE,
        header_timeout=DEFAULT_HEADER_TIMEOUT,
        keep_alive_timeout=DEFAULT_KEEP_ALIVE_TIMEOUT,
        body_timeout=DEFAULT_BODY_TIMEOUT,
        max_pipelined_requests=DEFAULT_MAX_PIPELINED_REQUESTS,
        write_timeout=DEFAULT_WRITE_TIMEOUT,
        timeout_cleanup=None,
    ):
        if not isinstance(connections, Connections):
            raise TypeError(
                "connections must be a stario_cython.protocol.Connections "
                f"(got {type(connections).__name__})"
            )
        self.loop = loop
        self.app = app
        self.tracer = tracer
        self.connections = <Connections>connections
        # Task(coro, loop=..., eager_start=...) without building kwargs per
        # call: the dicts live on the loop's Connections. A loop with a task
        # factory set gets create_task() so the factory is honoured; the
        # default fast path builds the Task directly.
        self.connections._bind_task_kwargs(loop)
        self._task_kw_eager = self.connections._task_kw_eager
        self._task_kw_lazy = self.connections._task_kw_lazy
        self._task_factory = loop.get_task_factory()
        self.noop_span = (
            tracer.create("request") if isinstance(tracer, NoOpTracer) else None
        )
        self.compression = compression
        self._app_state = getattr(app, "_app_state", None)
        # Protocols are built on the loop's thread, so this is the same set
        # App.tasks returns here; binding it skips a threading.local lookup
        # per request.
        tasks = getattr(app, "tasks", None)
        self._tasks = tasks if isinstance(tasks, set) else None

        self.transport = None
        self.pending_exchanges = deque()
        self.head_bytes = 0
        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self.max_pipelined_requests = max_pipelined_requests
        self.header_timeout = header_timeout
        self.keep_alive_timeout = keep_alive_timeout
        self.body_timeout = body_timeout
        self.write_timeout = write_timeout
        self.write_stall_deadline = 0.0
        self.write_stall_size = 0

        if timeout_cleanup is None:
            self.timeout_cleanup = CLEANUP_SWEEP
        elif isinstance(timeout_cleanup, str):
            self.timeout_cleanup = (
                CLEANUP_OFF
                if timeout_cleanup.strip().lower() in ("0", "off", "none", "false", "no")
                else CLEANUP_SWEEP
            )
        else:
            self.timeout_cleanup = (
                CLEANUP_OFF if timeout_cleanup == 0 else CLEANUP_SWEEP
            )
        self.rejected = False
        self.request_dispatched = False

    def connection_made(self, transport):
        cdef object ssl_object
        cdef object selected
        cdef Connections conns = self.connections
        if (
            conns.max_connections > 0
            and len(conns._live) >= conns.max_connections
        ):
            # Refuse before registering: nothing tracks this socket.
            conns.rejected += 1
            self.rejected = True
            if transport.get_extra_info("ssl_object") is None:
                transport.write(H1_OVERLOADED)
            transport.close()
            return
        self.transport = transport
        self._t_is_closing = transport.is_closing
        self.closed = False
        self.disconnect = None
        self.pause_reasons = 0
        self.body_pause_owner = None
        self.held_data = None
        self.held_offset = 0
        self.pump_scheduled = False
        self.rejected = False
        self.lingering = False
        self.linger_deadline = 0.0
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0
        self.parse_mode = PARSE_NONE
        self.preface_hold.clear()
        self.h1_headers_too_large = False

        self.connections._connection_opened(self)

        ssl_object = transport.get_extra_info("ssl_object")
        if ssl_object is not None:
            selected = ssl_object.selected_alpn_protocol()
            if isinstance(selected, bytes):
                try:
                    selected = selected.decode("ascii")
                except UnicodeDecodeError:
                    selected = None
            if selected == "h2":
                self._start_h2()
            else:
                # RFC 9113: ALPN is authoritative over TLS. Do not sniff
                # the cleartext preface after negotiating http/1.1.
                self.parse_mode = PARSE_H1
        # First request: header deadline only. Keep-alive stores a
        # deadline; the sweeper compares it. No TimerHandle per request.
        self._arm_timeout(TIMEOUT_HEADER, self.header_timeout)

    cdef object ensure_disconnect(self):
        if self.disconnect is None:
            self.disconnect = self.loop.create_future()
            if self.closed and not self.disconnect.done():
                self.disconnect.set_result(None)
        return self.disconnect

    cdef object ensure_stream_disconnect(self, RequestExchange ex):
        # Stream-scoped disconnect: resolves on stream reset or connection
        # loss (both route through RequestExchange.c_abort).
        if ex._stream_disc is None:
            ex._stream_disc = self.loop.create_future()
            if self.closed and not ex._stream_disc.done():
                ex._stream_disc.set_result(None)
        return ex._stream_disc

    def connection_lost(self, exc):
        cdef RequestExchange exchange
        self.closed = True
        self._cancel_timeout()
        self.h2_goaway_pending = False
        self.h2_goaway_deadline = 0.0
        self.lingering = False
        self.linger_deadline = 0.0
        self.connections._connection_closed(self)

        if self.h2 != NULL:
            nghttp2_session_del(self.h2)
            self.h2 = NULL
        for exchange in list(self.h2_streams.values()):
            # The session is gone, so no response bytes of this stream can
            # still be in flight: the exchange may recycle once its handler
            # and response are done.
            exchange._h2_outbound = False
            exchange.c_abort()
            if not exchange.handler_started:
                exchange.cancel_before_start()
        self.h2_streams.clear()

        if self.idle_exchange is not None:
            self.idle_exchange.recycle()
            self.idle_exchange = None
        if self.reading_exchange is not None:
            self.reading_exchange.c_abort()
        for exchange in self.pending_exchanges:
            exchange.cancel_before_start()
        self.pending_exchanges.clear()
        if (
            self.reading_exchange is not None
            and not self.reading_exchange.handler_started
        ):
            self.reading_exchange.cancel_before_start()
        if self.active_exchange is not None:
            self.active_exchange.c_abort()

        if self.disconnect is not None and not self.disconnect.done():
            self.disconnect.set_result(None)
        wake_waiters(self._write_waiters)

        self.pause_reasons = 0
        self.body_pause_owner = None
        self.held_data = None
        self.held_offset = 0
        self.pump_scheduled = False
        self.transport = None
        self._t_is_closing = None
        # Dead connection: a retained ``c`` must not keep the buffers alive.
        self._in_view = None
        self._in_buf = None
        self.h2_out = bytearray()
        self.h2_batch = []
        self.preface_hold = bytearray()
        self.last_task = None

    cdef void release_exchange(self, RequestExchange exchange):
        """Detach, then keep one idle on this connection or recycle on this thread."""
        cdef int32_t stream_id
        if exchange._http2 and self.h2 != NULL:
            stream_id = exchange._h2_stream_id
            if stream_id != 0:
                nghttp2_session_set_stream_user_data(self.h2, stream_id, NULL)
                self.h2_streams.pop(stream_id, None)
        if exchange._handler_task is self.last_task:
            # Do not keep a finished Task alive on an idle keep-alive connection.
            self.last_task = None
        exchange.detach()
        if not self.closed and self.idle_exchange is None:
            self.idle_exchange = exchange
        else:
            exchange.recycle()

    def eof_received(self):
        return False

    def data_received(self, data):
        if self.rejected or not data:
            return
        if self.parse_mode != PARSE_H2 and self.parser == NULL:
            return
        if self.timeout_kind == TIMEOUT_IDLE:
            if PyBytes_Check(data):
                self._refresh_idle(PyBytes_AS_STRING(data), PyBytes_GET_SIZE(data))
            else:
                self._refresh_idle(NULL, 0)
        if self.held_data is not None:
            self._held_append(data)
            return
        if self.pause_reasons:
            self.held_data = data
            self.held_offset = 0
            return
        self._pump_data(data, 0)
        self._after_pump()

    cdef void _held_append(self, object data) noexcept:
        """Append ``data`` onto paused input without ``held[off:] + data``."""
        cdef object held = self.held_data
        cdef Py_ssize_t off = self.held_offset
        cdef Py_ssize_t keep
        cdef Py_ssize_t add_n
        cdef Py_ssize_t new_n
        cdef const char* src = NULL
        cdef const char* oldp = NULL
        cdef char* dst
        cdef object ba
        if not _as_buf(data, &src, &add_n):
            self.held_data = held[off:] + data
            self.held_offset = 0
            return

        if PyBytes_Check(held):
            oldp = PyBytes_AS_STRING(held)
            keep = PyBytes_GET_SIZE(held) - off
            if keep < 0:
                keep = 0
            new_n = keep + add_n
            ba = bytearray()
            if PyByteArray_Resize(ba, new_n) < 0:
                PyErr_Clear()
                self.held_data = held[off:] + data
                self.held_offset = 0
                return
            dst = PyByteArray_AS_STRING(ba)
            if keep:
                memcpy(dst, oldp + off, <size_t>keep)
            if add_n:
                memcpy(dst + keep, src, <size_t>add_n)
            self.held_data = ba
            self.held_offset = 0
            return

        if PyByteArray_Check(held):
            oldp = PyByteArray_AS_STRING(held)
            keep = PyByteArray_GET_SIZE(held) - off
            if keep < 0:
                keep = 0
            if off:
                if keep:
                    memmove(<char*>oldp, oldp + off, <size_t>keep)
                self.held_offset = 0
            new_n = keep + add_n
            if PyByteArray_Resize(held, new_n) < 0:
                # OOM mid-append: held input is unrecoverable, so close.
                PyErr_Clear()
                self._protocol_error(500, "Internal Server Error")
                return
            if add_n:
                memcpy(PyByteArray_AS_STRING(held) + keep, src, <size_t>add_n)
            return
        self.held_data = held[off:] + data
        self.held_offset = 0

    cdef void _refresh_idle(self, const char* data, Py_ssize_t n) noexcept:
        """Keep-alive traffic drops the idle deadline.

        Not refreshed by: bytes that only drain a leftover body, HTTP/2
        control frames (a new stream clears it in ``_h2_begin_stream``), and
        bare CRLFs between HTTP/1 requests.
        """
        cdef Py_ssize_t i
        if self.parse_mode == PARSE_H2:
            return
        if self.reading_exchange is not None:
            if self.parse_mode == PARSE_H1 and (
                self.reading_exchange._discard_body or self.h1_headers_too_large
            ):
                return
        elif n > 0:
            for i in range(n):
                if data[i] != 13 and data[i] != 10:
                    break
            else:
                return
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0

    cdef void _arm_timeout(self, int kind, double seconds):
        """Store a header or idle deadline on the connection.

        The sweeper compares ``timeout_deadline`` to one ``loop.time()``
        per wake. Keep-alive is a double store, not ``call_later``.
        """
        if self.timeout_cleanup == CLEANUP_OFF:
            return
        if seconds <= 0 or self._closing():
            return
        self.timeout_kind = kind
        # Sweeper fills now+seconds on the next Date tick. Avoid loop.time()
        # on every keep-alive request.
        self.timeout_deadline = 0.0

    cdef inline void _arm_timeout_open(self, int kind, double seconds) noexcept:
        """``_arm_timeout`` for a caller that just saw the transport open."""
        if self.timeout_cleanup == CLEANUP_OFF or seconds <= 0:
            return
        self.timeout_kind = kind
        self.timeout_deadline = 0.0

    cdef void _cancel_timeout(self):
        self.timeout_kind = TIMEOUT_NONE
        self.timeout_deadline = 0.0

    cdef void _check_body_stall(self, RequestExchange exchange, double now):
        if exchange is None or exchange._timeout <= 0 or not exchange._waiting:
            return
        if exchange._stall_touch != exchange._stall_seen:
            exchange._stall_seen = exchange._stall_touch
            exchange._stall_deadline = now + exchange._timeout
            return
        if exchange._stall_deadline > 0.0 and now >= exchange._stall_deadline:
            exchange.fire_body_stall()

    cpdef void check_timeouts(self, double now):
        """Compare stored deadlines against ``now`` (one value per sweep)."""
        self._check_timeouts(now)

    cdef object date_line(self):
        return self.connections.c_date()

    cdef void _check_timeouts(self, double now):
        cdef RequestExchange reading
        cdef RequestExchange active
        cdef RequestExchange exchange
        cdef object transport
        cdef double seconds
        cdef int kind
        if self.lingering:
            # Input is being dropped after the response; hang up once the
            # client had LINGER_TIME to read it (RFC 9112 9.6). close()
            # still waits for the peer to take the buffered response, so a
            # peer that stopped reading is aborted one more LINGER_TIME
            # later; the write-stall check does not run while lingering.
            transport = self.transport
            if transport is None:
                return
            if self.linger_deadline <= 0.0:
                self.linger_deadline = now + LINGER_TIME
            elif now >= self.linger_deadline:
                self.linger_deadline = 0.0
                if transport.is_closing():
                    transport.abort()
                else:
                    transport.close()
            return
        if self._check_write_stall(now) or self.rejected:
            return

        if self.timeout_kind != TIMEOUT_NONE:
            if self.timeout_deadline <= 0.0:
                if self.timeout_kind == TIMEOUT_HEADER:
                    seconds = self.header_timeout
                elif self.timeout_kind == TIMEOUT_BODY:
                    seconds = self.body_timeout
                else:
                    seconds = self.keep_alive_timeout
                if seconds <= 0.0:
                    self.timeout_kind = TIMEOUT_NONE
                    self.timeout_deadline = 0.0
                else:
                    self.timeout_deadline = now + seconds
            elif now >= self.timeout_deadline:
                kind = self.timeout_kind
                self.timeout_kind = TIMEOUT_NONE
                self.timeout_deadline = 0.0
                if kind == TIMEOUT_BODY:
                    # Deferred small upload stalled: answer 408 and close.
                    self._protocol_error(408, "Request timeout")
                else:
                    self._close_on_timeout()
                return

        reading = self.reading_exchange
        active = self.active_exchange
        self._check_body_stall(reading, now)
        if active is not None and active is not reading:
            self._check_body_stall(active, now)

        if self.parse_mode == PARSE_H2:
            if self.h2_goaway_pending:
                # A client that never ACKs the shutdown PING gets the final
                # GOAWAY after H2_GOAWAY_GRACE anyway.
                if self.h2_goaway_deadline <= 0.0:
                    self.h2_goaway_deadline = now + H2_GOAWAY_GRACE
                elif now >= self.h2_goaway_deadline:
                    self._h2_final_goaway()
                    self._h2_send()
            for exchange in list(self.h2_streams.values()):
                if exchange.in_pool:
                    continue
                self._check_h2_header_timeout(exchange, now)
                if not exchange.in_pool:
                    self._check_body_stall(exchange, now)
                    self._check_h2_send_stall(exchange, now)

    cdef bint _check_write_stall(self, double now):
        """Abort a connection whose peer stopped reading its output.

        Output counts as stuck while the transport paused writing, or while
        a closing transport still holds unsent bytes. Any shrink of the
        buffer re-arms the deadline, so slow readers are not cut off.
        """
        cdef object transport = self.transport
        cdef object buffer_size
        cdef Py_ssize_t size
        if transport is None or self.write_timeout <= 0.0:
            return False
        if not (self.pause_reasons & PAUSE_WRITE) and not transport.is_closing():
            self.write_stall_deadline = 0.0
            return False

        buffer_size = getattr(transport, "get_write_buffer_size", None)
        size = buffer_size() if buffer_size is not None else 0
        if size <= 0:
            self.write_stall_deadline = 0.0
            return False

        if self.write_stall_deadline <= 0.0 or size < self.write_stall_size:
            self.write_stall_deadline = now + self.write_timeout
            self.write_stall_size = size
            return False
        self.write_stall_size = size
        if now < self.write_stall_deadline:
            return False
        self.write_stall_deadline = 0.0
        transport.abort()
        return True

    cdef void _close_on_timeout(self):
        cdef object transport = self.transport
        if transport is None or transport.is_closing():
            return
        if self.parse_mode == PARSE_H2 and self.h2 != NULL:
            # RFC 9113 6.8: tell the client which streams were processed.
            self._h2_final_goaway()
            self._h2_send()
        transport.close()

    cdef void _check_h2_header_timeout(self, RequestExchange ex, double now) noexcept:
        """Reset a stream that stalls on headers or a deferred body.

        Small bodies defer dispatch to message-complete, so ``headers_done``
        means the stream graduated to a body deadline that resets on DATA
        progress. Pre-headers it is the plain header deadline.
        """
        if (
            ex is None
            or ex._h2_dispatched
            or self.timeout_cleanup == CLEANUP_OFF
        ):
            return
        if ex._h2_headers_done:
            if self.body_timeout <= 0.0:
                return
            if ex._h2_body_mark < 0 or ex._total_read != ex._h2_body_mark:
                ex._h2_body_mark = ex._total_read
                ex._h2_header_deadline = now + self.body_timeout
                return
            if ex._h2_header_deadline <= 0.0:
                ex._h2_header_deadline = now + self.body_timeout
                return
            if now >= ex._h2_header_deadline:
                ex._h2_header_deadline = 0.0
                self._h2_reject_stream(ex._h2_stream_id, NGHTTP2_CANCEL)
                self._h2_send()
            return
        if self.header_timeout <= 0.0:
            return
        if ex._h2_header_deadline <= 0.0:
            ex._h2_header_deadline = now + self.header_timeout
            return
        if now >= ex._h2_header_deadline:
            ex._h2_awaiting_headers = False
            self._h2_reject_stream(ex._h2_stream_id, NGHTTP2_CANCEL)
            self._h2_send()

    cdef void _check_h2_send_stall(self, RequestExchange ex, double now) noexcept:
        """Reset a stream whose unsent DATA has made no progress.

        Flow-control-blocked bytes live on the exchange, not the transport
        buffer, so ``_check_write_stall`` cannot see them. Sent progress
        re-arms the deadline; a window that never opens gets CANCEL.
        """
        if ex is None:
            return
        if (
            ex._h2_end_sent
            or self.write_timeout <= 0.0
            or self.timeout_cleanup == CLEANUP_OFF
            or _h2_unsent(ex) <= 0
        ):
            ex._h2_send_deadline = 0.0
            return
        if ex._h2_sent_total != ex._h2_send_mark:
            ex._h2_send_mark = ex._h2_sent_total
            ex._h2_send_deadline = now + self.write_timeout
            return
        if ex._h2_send_deadline <= 0.0:
            ex._h2_send_deadline = now + self.write_timeout
            return
        if now >= ex._h2_send_deadline:
            ex._h2_send_deadline = 0.0
            self._h2_reject_stream(ex._h2_stream_id, NGHTTP2_CANCEL)
            self._h2_send()

    cdef void _after_pump(self):
        """Arm header or idle deadlines after this read.

        Unfinished headers get HEADER. A pump that left the HTTP/1
        connection empty with no timer — a protocol 431 never goes through
        ``response_completed`` — gets IDLE.
        Full keep-alive requests dispatch inside the pump, so HEADER is a
        no-op on that path.
        """
        if self.header_timeout_reset:
            self.header_timeout_reset = False
            if (
                not self.rejected
                and self.timeout_kind != TIMEOUT_BODY
                and self.reading_exchange is not None
                and (
                    not self.request_dispatched
                    or self.h1_headers_too_large
                )
            ):
                self._arm_timeout(TIMEOUT_HEADER, self.header_timeout)
        if self.timeout_kind != TIMEOUT_NONE or self.rejected:
            return

        if self.parse_mode == PARSE_H2:
            if not self.h2_streams:
                self._arm_timeout(TIMEOUT_IDLE, self.keep_alive_timeout)
            return

        if (
            self.parse_mode == PARSE_H1
            and self.reading_exchange is None
            and self.active_exchange is None
            and not self.pending_exchanges
        ):
            self._arm_timeout(TIMEOUT_IDLE, self.keep_alive_timeout)
            return
        if (
            self.parse_mode == PARSE_H1
            and self.reading_exchange is not None
            and (
                self.reading_exchange._discard_body
                or self.h1_headers_too_large
            )
            and not self.reading_exchange._body_complete
        ):
            self._arm_timeout(TIMEOUT_IDLE, self.keep_alive_timeout)

    cdef void _pump_data(self, object data, Py_ssize_t offset) noexcept:
        cdef const char* ptr = NULL
        cdef Py_ssize_t n
        if not _as_buf(data, &ptr, &n):
            n = len(data)
            ptr = <const char*>data
        if offset >= n:
            return
        self._pump_ptr(ptr + offset, n - offset)

    cdef bint _maybe_start_h2(self, const char* p, Py_ssize_t n) noexcept:
        """Detect the H2 preface before llhttp sees the first bytes.

        Incomplete preface prefixes are held. Anything else is HTTP/1.
        When the hold already contains this chunk, H1 fallback executes
        the hold only (do not append ``data`` again).
        """
        cdef const char* pref = PyBytes_AS_STRING(H2_PREFACE)
        cdef bint from_hold = False
        if self.preface_hold:
            if _preface_append(self.preface_hold, p, n) != 0:
                self._protocol_error(500, "Internal Server Error")
                return False
            p = PyByteArray_AS_STRING(self.preface_hold)
            n = PyByteArray_GET_SIZE(self.preface_hold)
            from_hold = True

        if n >= H2_PREFACE_LEN and memcmp(p, pref, <size_t>H2_PREFACE_LEN) == 0:
            self._start_h2()
            if from_hold:
                self._h2_recv(p, <size_t>n)
                self.preface_hold.clear()
                return False
            return True
        if n > 0 and n < H2_PREFACE_LEN and memcmp(p, pref, <size_t>n) == 0:
            if not from_hold:
                if _preface_append(self.preface_hold, p, n) != 0:
                    self._protocol_error(500, "Internal Server Error")
            return False

        self.parse_mode = PARSE_H1
        return True

    cdef void _h1_execute_held(self) noexcept:
        cdef object held = self.preface_hold
        self.preface_hold = bytearray()
        self._h1_execute_ptr(
            PyByteArray_AS_STRING(held), PyByteArray_GET_SIZE(held)
        )

    cdef void _set_pause_reason(self, int reason, bint paused):
        cdef int previous = self.pause_reasons
        cdef object transport = self.transport
        if paused:
            self.pause_reasons |= reason
        else:
            self.pause_reasons &= ~reason
        if previous == self.pause_reasons:
            return

        if previous == 0:
            # First pause reason: stop the read side.
            if transport is not None and not transport.is_closing():
                transport.pause_reading()
            return
        if self.pause_reasons != 0:
            return

        # Last reason cleared: drain held input first, then resume reads.
        if self.held_data is not None:
            if not self.pump_scheduled:
                self.pump_scheduled = True
                self.loop.call_soon(self._resume_held_input)
            return
        if transport is not None and not transport.is_closing():
            transport.resume_reading()

    def _resume_held_input(self):
        cdef object data
        cdef Py_ssize_t offset
        cdef object transport
        self.pump_scheduled = False
        if self.pause_reasons or self.held_data is None:
            return
        data = self.held_data
        offset = self.held_offset
        self.held_data = None
        self.held_offset = 0
        self._pump_data(data, offset)
        self._after_pump()

        if self.pause_reasons == 0 and self.held_data is None:
            transport = self.transport
            if transport is not None and not transport.is_closing():
                transport.resume_reading()

    cdef void close_lingering(self):
        """Close an HTTP/1 connection whose client may still be sending.

        A close with unread input makes the kernel send RST, and the client
        can lose a response it has not read yet (a 413 in the middle of an
        upload). So send FIN after the response, then read and drop input
        until the client closes or LINGER_TIME passes (RFC 9112 9.6).
        """
        cdef object transport = self.transport
        if transport is None or transport.is_closing() or self.lingering:
            return
        if self.timeout_cleanup == CLEANUP_OFF:
            # No tick to end the linger window (the test client): close now.
            transport.close()
            return
        # ``rejected`` drops input before the parser; reading stays on.
        self.rejected = True
        self._cancel_timeout()
        self.held_data = None
        self.held_offset = 0
        self.pause_reasons = 0
        self.body_pause_owner = None
        try:
            transport.resume_reading()
            if transport.can_write_eof():
                transport.write_eof()
        except Exception:
            transport.close()
            return
        self.lingering = True
        self.linger_deadline = 0.0

    cdef void _close_after_response(self, RequestExchange exchange):
        # The client may still be sending the body of this request.
        if exchange._body_active and not exchange._body_complete:
            self.close_lingering()
        else:
            self.transport.close()

    cdef void set_body_paused(self, RequestExchange exchange, bint paused):
        if exchange._http2:
            # HTTP/2: hold this stream's WINDOW_UPDATE instead of pausing the
            # socket, so one slow body consumer cannot stall other streams.
            # The stream window (H2_STREAM_WINDOW) bounds what one stream
            # buffers; the connection window (H2_CONN_WINDOW) bounds them all.
            if exchange._h2_flow_paused == paused:
                return
            exchange._h2_flow_paused = paused
            self.h2_paused_streams += 1 if paused else -1
            if (
                not paused
                and self.h2 != NULL
                and not exchange.in_pool
                and exchange._h2_stream_id != 0
            ):
                self._h2_window_update(exchange._h2_stream_id, True)
                self._h2_window_update(0, True)
                self._h2_send()
            return
        if paused:
            self.body_pause_owner = exchange
            self._set_pause_reason(PAUSE_BODY, True)
        elif self.body_pause_owner is exchange:
            self.body_pause_owner = None
            self._set_pause_reason(PAUSE_BODY, False)

    def close_if_idle(self) -> bool:
        """Close the connection if it is waiting for a new request.

        Called by the ``Server`` drain path on shutdown.
        """
        cdef object transport
        if self.parse_mode == PARSE_H2 and self.h2 != NULL and not self.rejected:
            # Graceful HTTP/2 drain (RFC 9113 6.8): a shutdown notice and a
            # PING first, so streams the client opened in flight are not
            # refused; the final GOAWAY follows the PING ACK. Running
            # streams finish; the connection closes once nghttp2 is done.
            if not self.h2_goaway_sent:
                self.h2_goaway_sent = True
                if (
                    self.timeout_cleanup == CLEANUP_SWEEP
                    and nghttp2_submit_shutdown_notice(self.h2) == 0
                    and nghttp2_submit_ping(self.h2, NGHTTP2_FLAG_NONE, NULL) == 0
                ):
                    # The final GOAWAY follows the PING ACK, or the tick
                    # sends it at H2_GOAWAY_GRACE for a client that never ACKs.
                    self.h2_goaway_pending = True
                    self.h2_goaway_deadline = 0.0
                else:
                    self._h2_final_goaway()
                self._h2_send()
            transport = self.transport
            return transport is None or transport.is_closing()

        if (
            self.active_exchange is not None
            or self.reading_exchange is not None
            or self.pending_exchanges
            or self.held_data is not None
            or self.rejected
            or self.h2_streams
        ):
            return False
        transport = self.transport
        if transport is None or transport.is_closing():
            return False
        transport.close()
        self._cancel_timeout()
        return True

    def pause_writing(self):
        self._set_pause_reason(PAUSE_WRITE, True)

    def resume_writing(self):
        self._set_pause_reason(PAUSE_WRITE, False)
        self.write_stall_deadline = 0.0
        wake_waiters(self._write_waiters)
        if (
            self.active_exchange is None
            and self.pending_exchanges
            and not self._closing()
        ):
            self._start_next_pending()

    cdef object drain_waiter(self, RequestExchange exchange):
        """A future to await before writing more, or None to go ahead."""
        cdef object fut
        cdef object transport = self.transport
        if self.closed or transport is None or transport.is_closing():
            return None
        if self.pause_reasons & PAUSE_WRITE:
            fut = self.loop.create_future()
            self._write_waiters.append(fut)
            return fut
        if exchange._http2 and _h2_unsent(exchange) > H2_DRAIN_HIGH:
            # Stream-level drain: wait until nghttp2 sends down to H2_DRAIN_LOW.
            fut = self.loop.create_future()
            exchange._drain_waiters.append(fut)
            return fut
        return None

    cdef inline bint _closing(self) noexcept:
        cdef object is_closing = self._t_is_closing
        if is_closing is None:
            return True
        try:
            return bool(is_closing())
        except Exception:
            return True

    cdef bint _header_too_large(self, size_t length) noexcept:
        self.head_bytes += <int>length
        if self.head_bytes > self.max_header_bytes:
            self.h1_headers_too_large = True
            return True
        return False

    cdef inline RequestExchange _take_exchange(self):
        """Reuse this connection's idle exchange, else this thread's spare."""
        cdef RequestExchange ex = self.idle_exchange
        if ex is not None:
            self.idle_exchange = None
            ex.reset(
                self,
                self.app,
                self.transport,
                self.compression,
                self.max_body_bytes,
                self.body_timeout,
            )
            return ex
        return acquire_exchange(
            self,
            self.app,
            self.transport,
            self.compression,
            self.max_body_bytes,
            self.body_timeout,
        )

    cdef void _on_message_begin(self) noexcept:
        if self.rejected:
            return
        # Pool miss: constructing an exchange can raise. Keep-alive reuse
        # in ``_take_exchange`` is noexcept.
        try:
            self.reading_exchange = self._take_exchange()
        except Exception:
            _log.exception("Failed to allocate a request exchange")
            self._protocol_error(400, "Invalid HTTP request")
            return
        # Seed the header budget: the request line itself costs a field.
        self.head_bytes = 40
        self.request_dispatched = False
        self.header_timeout_reset = True
        self.h1_headers_too_large = False
        self.h1_in_trailers = False

    cdef void _on_url(self, const char* at, size_t length) noexcept:
        if self.rejected or self.reading_exchange is None:
            return
        if self.h1_headers_too_large or self._header_too_large(length):
            return
        if self.reading_exchange.append_request_url(at, length) != 0:
            self.h1_headers_too_large = True

    cdef void _on_header_field(self, const char* at, size_t length) noexcept:
        if self.rejected or self.reading_exchange is None or self.h1_in_trailers:
            return
        if self.h1_headers_too_large or self._header_too_large(length):
            return
        if self.reading_exchange.append_request_header_name(at, length) != 0:
            self._protocol_error(400, "Invalid HTTP request")

    cdef void _on_header_value(self, const char* at, size_t length) noexcept:
        if self.rejected or self.reading_exchange is None or self.h1_in_trailers:
            return
        if self.h1_headers_too_large or self._header_too_large(length):
            return
        if self.reading_exchange.append_request_header_value(at, length) != 0:
            self._protocol_error(400, "Invalid HTTP request")

    cdef void _on_header_value_complete(self) noexcept:
        # Trailers (RFC 9110 §6.5) are dropped, never merged into headers.
        if self.h1_headers_too_large or self.rejected or self.h1_in_trailers:
            return
        if self._header_too_large(HEADER_FIELD_OVERHEAD):
            return
        if self.reading_exchange is not None:
            if self.reading_exchange.finish_request_header() != 0:
                self._protocol_error(400, "Invalid HTTP request")

    cdef void _on_headers_complete(self) noexcept:
        cdef RequestExchange exchange
        cdef uint16_t flags
        cdef uint64_t content_length
        cdef bint has_body
        cdef int http_major
        cdef int http_minor
        cdef int method
        if self.rejected or self.reading_exchange is None:
            return
        exchange = self.reading_exchange
        self.h1_in_trailers = True

        flags = stario_parser_flags(self.parser)
        content_length = stario_parser_content_length(self.parser)
        http_major = <int>llhttp_get_http_major(self.parser)
        http_minor = <int>llhttp_get_http_minor(self.parser)
        method = <int>llhttp_get_method(self.parser)
        exchange._head_request = method == 2
        if http_minor == 0 and http_major == 1:
            # RFC 9110 10.1.1: ignore the expectation in an HTTP/1.0 request,
            # whose sender may take a 1xx for the final response.
            exchange._req_expect_continue = False
        has_body = (
            (flags & F_CHUNKED) != 0
            or ((flags & F_CONTENT_LENGTH) != 0 and content_length > 0)
        )
        # Recycle resets this; snapshot first if the connection still needs it.
        exchange._keep_alive = (
            llhttp_should_keep_alive(self.parser) != 0
            or (
                http_major == 1
                and http_minor == 1
                and not exchange._req_connection_close
            )
        )

        if http_major != 1:
            self._protocol_error(400, "Invalid HTTP request")
            return
        if llhttp_get_upgrade(self.parser):
            self._protocol_error(400, "Upgrade not supported")
            return
        # RFC 7230 3.3.3: TE without chunked as the final coding — body
        # length is undefined. Reject before dispatch (llhttp errors after
        # on_headers_complete, which would otherwise 404 then 400).
        if (flags & F_TRANSFER_ENCODING) and not (flags & F_CHUNKED):
            self._protocol_error(400, "Invalid HTTP request")
            return
        # RFC 9112 6.1: Transfer-Encoding in HTTP/1.0 is faulty framing;
        # answer, then close.
        if (flags & F_TRANSFER_ENCODING) and http_minor == 0:
            exchange._keep_alive = False

        if self.h1_headers_too_large:
            # Keep-alive only when there is no body to drain. A huge POST
            # after oversize headers would keep the socket reading forever.
            # Trust llhttp when it says this message cannot be keep-alive
            # (TE-without-chunked is already rejected above).
            self._protocol_error(
                431,
                "Request header fields too large",
                has_body
                or not exchange._keep_alive
                or llhttp_should_keep_alive(self.parser) == 0,
            )
            return

        if exchange.finish_request_header() != 0:
            self._protocol_error(400, "Invalid HTTP request")
            return
        if http_minor == 1 and exchange._req_host_index < 0:
            # HTTP/1.1 requires Host.
            self._protocol_error(400, "Invalid HTTP request")
            return
        # max_body_bytes is only the default for a read: the handler decides
        # (body(max_size=...)). Here only a length no read can accept is 413.
        if flags & F_CONTENT_LENGTH and content_length > <uint64_t>BODY_LIMIT_MAX:
            self._protocol_error(413, "Request body too large")
            return

        exchange.cache_hot_request_headers()
        if self.request_dispatched or self.rejected or self._closing():
            return
        try:
            if (
                (flags & F_CHUNKED)
                or (flags & F_CONTENT_LENGTH and content_length > 0)
                or exchange._req_expect_continue
            ):
                exchange.reset_body(
                    exchange._req_expect_continue,
                    <Py_ssize_t>content_length if flags & F_CONTENT_LENGTH else -1,
                )
                # Small Content-Length bodies that fit in the current read complete
                # before the handler runs, so body() hits the cached bytes instead
                # of waiting on an Event during llhttp_execute. Expect: 100-continue
                # and large/chunked bodies still dispatch at headers-complete.
                if (
                    not exchange._req_expect_continue
                    and (flags & F_CONTENT_LENGTH)
                    and content_length <= <uint64_t>SMALL_BODY
                ):
                    # The request is not dispatched, so the header deadline
                    # cannot keep running: a dribbled body would die at
                    # header_timeout. BODY gets body_timeout with progress
                    # resets in _on_body.
                    self._arm_timeout(TIMEOUT_BODY, self.body_timeout)
                    return
            else:
                exchange.mark_nobody()
            self._bind_request(exchange)
            self._dispatch(exchange)
        except Exception:
            _log.exception("Failed to dispatch request")
            self._protocol_error(400, "Invalid HTTP request")

    cdef void _on_body(self, const char* at, size_t length) noexcept:
        if self.rejected or self.reading_exchange is None:
            return
        if self.timeout_kind == TIMEOUT_BODY:
            # Deferred-body progress: re-arm on the next sweep, no loop.time().
            self.timeout_deadline = 0.0
        if self.reading_exchange.c_feed(at, length) != 0:
            self._protocol_error(400, "Invalid HTTP request")

    cdef void _on_message_complete(self) noexcept:
        cdef RequestExchange exchange
        cdef object transport
        cdef bint keep_alive
        if self.rejected:
            return

        exchange = self.reading_exchange
        if self.h1_headers_too_large:
            # Snapshot before handler_finished() recycles the exchange and
            # _clear_request_binding() resets _keep_alive to True.
            keep_alive = exchange is not None and exchange._keep_alive
            if exchange is not None:
                if exchange._body_active and not exchange._body_complete:
                    if exchange.c_complete() != 0:
                        self._protocol_error(400, "Invalid HTTP request")
                        self.reading_exchange = None
                        self.h1_headers_too_large = False
                        return
                # A queued 431 is finished by its in-order answer.
                if exchange.handler_started:
                    exchange.handler_finished()
            self.reading_exchange = None
            self.h1_headers_too_large = False
            if self.timeout_kind == TIMEOUT_HEADER:
                self._cancel_timeout()
            if not keep_alive:
                transport = self.transport
                if transport is not None and not transport.is_closing():
                    transport.close()
            return

        # llhttp's message-complete means the whole body is in — close it out.
        if exchange is not None and exchange._body_active:
            if exchange.c_complete() != 0:
                self._protocol_error(400, "Invalid HTTP request")
                self.reading_exchange = None
                return

        if (
            not self.request_dispatched
            and exchange is not None
            and not self.rejected
        ):
            try:
                if self._closing():
                    self.reading_exchange = None
                    return
                self._bind_request(exchange)
                self._dispatch(exchange)
            except Exception:
                _log.exception("Failed to dispatch request")
                self._protocol_error(400, "Invalid HTTP request")
                self.reading_exchange = None
                return

        self.reading_exchange = None

    cdef void _bind_request(self, RequestExchange exchange):
        """Bind method/version and classify the request-target (RFC 9112 §3.2).

        origin-form ``/path?query`` routes. ``OPTIONS *`` (asterisk-form) is
        answered by the server. HTTP/1 absolute-form ``http://host/path`` routes
        on its path and uses its authority as the host (the Host header is
        ignored, §3.2.2). Anything else — other methods with ``*``, a ``#``,
        a path that is not valid percent-encoded UTF-8, a decoded control
        byte — is flagged ``PATH_BAD`` and answered 400 in order, like any
        response. Dot segments and a trailing slash get ``PATH_REDIRECT``.
        Nothing is decoded here: ASCII paths stay arena bytes.
        """
        cdef object method
        cdef object version
        cdef const char* url
        cdef const char* q
        cdef Py_ssize_t n
        cdef Py_ssize_t path_end
        cdef Py_ssize_t start = 0
        cdef Py_ssize_t auth_end
        cdef int flags
        if exchange._http2:
            method = exchange._h2_method or METH_GET
            version = VER_20
            exchange._keep_alive = True
        else:
            method = _method_str(<int>llhttp_get_method(self.parser))
            version = _version_str(
                <int>llhttp_get_http_major(self.parser),
                <int>llhttp_get_http_minor(self.parser),
            )

        exchange._method = method
        exchange._version = version
        exchange._path = None
        exchange._target_host = None
        exchange._path_off = 0
        exchange._path_n = 0
        exchange._query_off = 0
        exchange._query_len = 0
        if method is METH_HEAD:
            exchange._head_request = True

        n = exchange._req_url_length
        if n <= 0 or exchange._req_arena == NULL:
            exchange._path_flags = PATH_BAD
            return

        url = exchange._req_arena + exchange._req_url_offset
        q = <const char*>memchr(url, 63, <size_t>n)
        path_end = n if q == NULL else <Py_ssize_t>(q - url)
        if q != NULL and path_end + 1 < n:
            # A fragment is never part of a request-target (the path scan
            # rejects '#' too).
            if memchr(q + 1, 35, <size_t>(n - path_end - 1)) != NULL:
                exchange._path_flags = PATH_BAD
                return
            # A NUL in the query (bare or ``%00``) is a 400, like one in the path.
            if query_has_nul(q + 1, n - path_end - 1):
                exchange._path_flags = PATH_BAD
                return
            exchange._query_off = exchange._req_url_offset + path_end + 1
            exchange._query_len = n - path_end - 1

        if url[0] == 47:
            start = 0
        elif n == 1 and url[0] == 42:
            exchange._path_flags = (
                PATH_OPTIONS_STAR if method is METH_OPTIONS else PATH_BAD
            )
            return
        elif not exchange._http2 and _absolute_form_authority(url, path_end, &start, &auth_end):
            if not self._bind_authority(exchange, url + start, auth_end - start):
                exchange._path_flags = PATH_BAD
                return
            start = auth_end
            if start == path_end:
                # ``http://host`` / ``http://host?q``: the path is ``/``.
                exchange._path_flags = 0
                return
        else:
            exchange._path_flags = PATH_BAD
            return

        flags = scan_request_path(url + start, path_end - start, False)
        if flags < 0:
            exchange._path_flags = PATH_BAD
            return
        exchange._path_flags = flags
        exchange._path_off = start
        exchange._path_n = path_end - start

    cdef bint _bind_authority(
        self, RequestExchange exchange, const char* p, Py_ssize_t n
    ) noexcept:
        """Validate an absolute-form authority and bind it as the request host."""
        if n <= 0 or memchr(p, 64, <size_t>n) != NULL:
            return False
        if not host_value_ok(p, <size_t>n):
            return False
        try:
            exchange._target_host = _host_without_port_n(p, n)
        except Exception:
            return False
        return True

    cdef void _dispatch(self, RequestExchange exchange):
        cdef object span
        self.request_dispatched = True
        if (
            self.timeout_kind == TIMEOUT_HEADER
            or self.timeout_kind == TIMEOUT_BODY
        ):
            self.timeout_kind = TIMEOUT_NONE
            self.timeout_deadline = 0.0

        if self.noop_span is not None:
            span = self.noop_span
        else:
            span = self.tracer.create("request") if self.tracer is not None else None
        exchange.span = span

        if exchange._http2:
            self._start_exchange(exchange, True)
            return
        if self.active_exchange is None and not (self.pause_reasons & PAUSE_WRITE):
            self._start_exchange(exchange, True)
        else:
            # Queued behind the in-flight response, or behind output the
            # client has not read yet (resume_writing starts it).
            if len(self.pending_exchanges) >= self.max_pipelined_requests:
                self._protocol_error(429, "Too many pipelined requests")
                return
            self.pending_exchanges.append(exchange)
            self._set_pause_reason(PAUSE_PIPELINE, True)

    cdef void _answer_target(self, RequestExchange exchange, object method):
        """Server-level answer for a request-target that never reaches a route.

        Written through the exchange like a handler response, so pipelined
        order holds, HTTP/1 keep-alive survives, and HTTP/2 answers only the
        stream.
        """
        cdef int flags = exchange._path_flags
        cdef const char* p
        cdef Py_ssize_t n = 0
        cdef object loc
        cdef object canonical
        cdef object span_name
        cdef int status
        exchange.start_response()
        if exchange._protocol_status:
            status = exchange._protocol_status
            if self.noop_span is None:
                if exchange.span is not None:
                    exchange.span.rename(method)
                self._finish_protocol_span(
                    status, exchange.span, method, exchange.decode_request_path()
                )
            exchange.respond(
                exchange._protocol_body, b"text/plain; charset=utf-8", status
            )
            exchange.handler_finished()
            return

        span_name = method
        if flags & PATH_BAD:
            status = 400
        elif flags & PATH_OPTIONS_STAR:
            status = 204
        else:
            status = 308
            p = exchange.path_ptr(&n)
            canonical = canonical_path(p, n)
            loc = canonical
            if exchange._query_len > 0:
                loc = loc + b"?" + PyBytes_FromStringAndSize(
                    exchange._req_arena + exchange._query_off, exchange._query_len
                )
            exchange.headers.c_set(b"location", loc)
            if self.noop_span is None:
                # Name the span after the route the redirect lands on, so
                # `/search/` and `/search` roll up together in telemetry.
                span_name = self._canonical_route_name(
                    exchange, canonical, method
                ) or method

        if self.noop_span is None:
            if exchange.span is not None:
                exchange.span.rename(span_name)
            self._finish_protocol_span(
                status, exchange.span, method, exchange.decode_request_path()
            )

        if status == 400:
            exchange.respond(
                b"Invalid HTTP request", b"text/plain; charset=utf-8", 400
            )
        else:
            exchange.respond(b"", b"text/plain; charset=utf-8", status)
        exchange.handler_finished()

    cdef object _canonical_route_name(
        self, RequestExchange exchange, object canonical, object method
    ):
        """Pattern of the route a 308 target resolves to, or None (telemetry only)."""
        cdef AppState state = self._app_state
        cdef CRouter router
        cdef object hit
        try:
            if state is None:
                return None
            router = <CRouter>self.app.compiled()
            hit = router.c_lookup(
                exchange.host_from_arena() if state.host_routing else "",
                PyUnicode_DecodeLatin1(
                    PyBytes_AS_STRING(canonical), PyBytes_GET_SIZE(canonical), NULL
                ),
                method,
            )
            return hit[2].pattern or None
        except Exception:
            return None

    cdef void _start_exchange(self, RequestExchange exchange, bint eager_start):
        """Route and start the handler Task, or answer inline.

        Never raises: this also runs from ``response_completed`` inside the
        previous handler's ``respond()``, which must not see our failure.
        """
        try:
            self._start_exchange_inner(exchange, eager_start)
        except Exception as exc:
            _log.error("Could not start request handler", exc_info=exc)
            try:
                if not exchange._completed and exchange._handler_task is None:
                    if exchange._status_code < 0:
                        exchange.headers.c_clear()
                        exchange.respond(
                            b"Internal Server Error",
                            b"text/plain; charset=utf-8",
                            500,
                        )
                    else:
                        exchange.abort()
                if exchange._handler_task is None:
                    exchange.handler_finished()
            except Exception:
                _log.exception("Failed to start handler")
                transport = self.transport
                if transport is not None and not transport.is_closing():
                    transport.close()

    cdef void _start_exchange_inner(self, RequestExchange exchange, bint eager_start):
        cdef object path
        cdef object handler
        cdef object route
        cdef object match
        cdef object task
        cdef object tasks
        cdef object span
        cdef object host
        cdef object method
        cdef const char* path_p
        cdef Py_ssize_t path_n = 0
        cdef AppState state
        cdef CRouter router
        cdef RequestHandle h
        cdef bint host_routing
        cdef object kw
        if not exchange._http2:
            self.active_exchange = exchange
        method = exchange._method
        if exchange._protocol_status or exchange._path_flags & (
            PATH_BAD | PATH_OPTIONS_STAR | PATH_REDIRECT
        ):
            self._answer_target(exchange, method)
            return

        exchange.start_response()
        span = exchange.span
        if span is not None and self.noop_span is None:
            path = exchange.decode_request_path()
            span.start()
            span.attrs({"request.method": method, "request.path": path})

        state = self._app_state
        router = None
        host_routing = False
        if state is not None:
            if state.router is None:
                # First request after a registration: compile once, here.
                router = <CRouter>self.app.compiled()
            else:
                router = <CRouter>state.router
            host_routing = state.host_routing
        elif self.app.host_routing:
            host_routing = True
        if host_routing:
            host = exchange.host_from_arena()
        else:
            host = ""
        path_p = exchange.path_ptr(&path_n)
        if router is not None:
            router.c_lookup_into(
                host,
                path_p,
                path_n,
                (exchange._path_flags & PATH_HAS_PCT) != 0,
                method,
                exchange,
            )
        else:
            path = PyUnicode_DecodeLatin1(path_p, path_n, NULL)
            handler, route, match = self.app.find_handler(host, path, method)
            exchange._handler = handler
            exchange._route = route
            exchange.match = match

        if span is not None and self.noop_span is None:
            if exchange.match.pattern:
                span.rename(exchange.match.pattern)
                span.attr("http.route", exchange._route.path)
            else:
                # 404 / 405: the method is the only stable name.
                span.rename(method)

        # A real asyncio.Task at dispatch (headers-complete for
        # large/chunked/expect-continue, else message-complete). respond()/end()
        # frees the connection via response_completed; the Task may outlive the
        # response. Incomplete work joins app.tasks so shutdown drain sees it.
        h = exchange.start_handle()
        kw = self._task_kw_eager if eager_start else self._task_kw_lazy
        if self._task_factory is None:
            task = PyObject_Call(_asyncio_Task, (exchange._handler(h, h),), kw)
        else:
            task = self._factory_task(exchange._handler(h, h), eager_start)
        # Drop the local so recycle can tell whether the handler kept ``c``.
        h = None
        exchange._handler_task = task
        self.last_task = task

        if _task_done(task):
            # Skip only a clean NoOp success. Write-then-raise / cancel / an
            # incomplete response must still hit on_handler_done (log + abort).
            if not exchange._completed or self.noop_span is None:
                exchange.on_handler_done(task)
            elif _task_clean(task):
                exchange.handler_finished()
            else:
                exchange.on_handler_done(task)
        else:
            tasks = self._tasks if self._tasks is not None else self.app.tasks
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            task.add_done_callback(exchange.on_handler_done)

    cdef object _factory_task(self, object coro, bint eager_start):
        """``loop.create_task`` so a user task factory sees handler tasks.

        3.14 forwards ``eager_start``; an older loop or factory that rejects
        it gets a plain create_task (no eager start).
        """
        try:
            return self.loop.create_task(coro, eager_start=eager_start)
        except TypeError:
            return self.loop.create_task(coro)

    cdef void _drop_pending(self):
        cdef RequestExchange exchange
        cdef object span
        for exchange in self.pending_exchanges:
            if self.noop_span is None:
                span = exchange.span
                if span is not None:
                    try:
                        finish_request_span(
                            span,
                            method=exchange._method,
                            path=exchange.decode_request_path(),
                        )
                    except Exception:
                        pass
            exchange.cancel_before_start()
        self.pending_exchanges.clear()

    cdef void _start_next_pending(self):
        cdef RequestExchange next_exchange = self.pending_exchanges.popleft()
        self._start_exchange(next_exchange, False)
        if not self.pending_exchanges:
            self._set_pause_reason(PAUSE_PIPELINE, False)

    cdef void response_completed(self, RequestExchange exchange):
        """Advance the connection after the response is fully sent.

        Fired from ``respond()`` / ``end()`` / ``abort()`` via ``_done`` — not when
        the handler coroutine returns. The handler (or ``app.create_task`` work)
        may still run after this; the connection is free to start the next
        pipelined/keep-alive exchange immediately.
        """
        cdef object transport = self.transport
        cdef object conn
        cdef bint keep
        if exchange._http2:
            # Keep the stream mapped until on_stream_close so the DATA
            # provider cannot be recycled while nghttp2 still holds it.
            return
        if self.active_exchange is not exchange:
            return
        self.active_exchange = None

        if self._closing():
            self._drop_pending()
            return

        conn = exchange.headers.c_get(b"connection")
        if conn is not None and (
            conn == b"close" or conn.lower() == b"close"
        ):
            self._close_after_response(exchange)
            self._drop_pending()
            return

        keep = exchange._keep_alive
        if not keep or (
            self._app_state.shutting_down
            if self._app_state is not None
            else self.app.shutting_down
        ):
            self._close_after_response(exchange)
            self._drop_pending()
            return

        if self.pending_exchanges:
            # A peer that is not reading gets no more responses queued;
            # resume_writing() starts the next one.
            if not (self.pause_reasons & PAUSE_WRITE):
                self._start_next_pending()
            return
        if self.deferred_status:
            # Every request before the failed one is answered: its error
            # goes last, then the connection closes.
            self._write_h1_error(
                self.deferred_status, self.deferred_body, self.deferred_head
            )
            self.deferred_status = 0
            transport.close()
            return

        self._set_pause_reason(PAUSE_PIPELINE, False)
        # Headers of the next request may already be trickling in: their
        # deadline keeps running, or slow headers never time out. A pipelined
        # deferred body has its own BODY deadline; idle must not clobber it.
        if (
            self.timeout_kind != TIMEOUT_HEADER
            and self.timeout_kind != TIMEOUT_BODY
        ):
            # ``_closing()`` was checked above and nothing since can close.
            self._arm_timeout_open(TIMEOUT_IDLE, self.keep_alive_timeout)

    cdef void _finish_protocol_span(
        self,
        int status,
        object span,
        object method,
        object path,
    ) noexcept:
        if self.noop_span is not None:
            return
        try:
            if span is None and self.tracer is not None:
                span = self.tracer.create("request")
            finish_request_span(span, status=status, method=method, path=path)
        except Exception:
            _log.exception("Failed to finish the protocol span")

    cdef void _abort_reading_and_pending(self) noexcept:
        cdef RequestExchange exchange = self.reading_exchange
        # Cleared first: the abort may recycle the exchange, and
        # ``connection_lost`` must not abort it again once it serves
        # another request.
        self.reading_exchange = None
        if exchange is not None:
            exchange.c_abort()
            if not exchange.handler_started:
                exchange.cancel_before_start()
        self._drop_pending()

    cdef void _write_h1_error(self, int status, object body, bint head_only) noexcept:
        cdef object transport = self.transport
        cdef object date
        if transport is None or transport.is_closing():
            return
        date = self.connections.c_date()
        if head_only:
            transport.write(
                b"".join((
                    _status_line(status),
                    date,
                    b"content-type: text/plain; charset=utf-8\r\n",
                    b"content-length: %d\r\n" % len(body),
                    b"connection: close\r\n",
                    b"\r\n",
                ))
            )
        else:
            transport.write(
                b"".join((
                    _status_line(status),
                    date,
                    b"content-type: text/plain; charset=utf-8\r\n",
                    b"content-length: %d\r\n" % len(body),
                    b"connection: close\r\n",
                    b"\r\n",
                    body,
                ))
            )

    cdef void _protocol_error(
        self,
        int status,
        object message,
        bint close_conn=True,
    ) noexcept:
        """Write a protocol HTTP status without dispatch.

        Keep-alive 431 (``close_conn=False``) uses ``respond()`` so the
        connection stays open. Close writes Connection: close (HTTP/1
        only, and only if nothing is already on the wire) then hangs up.
        """
        cdef RequestExchange exchange
        cdef object body
        cdef object span
        cdef object method
        cdef object path
        cdef object transport

        if self.rejected:
            return
        # A keep-alive 431 already dispatched; do not write a second status.
        if not close_conn and self.request_dispatched:
            return

        transport = self.transport
        exchange = self.reading_exchange
        span = None
        method = None
        path = None
        if exchange is not None:
            span = exchange.span
            try:
                if exchange._req_url_length > 0:
                    path = _span_path(
                        exchange._req_arena + exchange._req_url_offset,
                        exchange._req_url_length,
                    )
                    if exchange._http2:
                        method = exchange._h2_method or METH_GET
                    elif self.parser != NULL:
                        method = _method_str(<int>llhttp_get_method(self.parser))
                elif self.request_dispatched and exchange._method is not None:
                    method = exchange._method
                    path = exchange.decode_request_path()
            except Exception:
                method = None
                path = None

        if not close_conn:
            # Keep-alive 431: answered through the normal dispatch queue,
            # so it goes out after any pipelined responses ahead of it.
            if exchange is None:
                return
            try:
                exchange.mark_nobody()
                self._bind_request(exchange)
                exchange._protocol_status = status
                exchange._protocol_body = message.encode("utf-8")
                self._dispatch(exchange)
            except Exception:
                _log.exception("Failed to queue a protocol response")
                self.rejected = True
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        pass
            return

        self.request_dispatched = True
        self.rejected = True
        self._cancel_timeout()
        try:
            if transport is None or transport.is_closing():
                self._abort_reading_and_pending()
                return

            if self.parse_mode == PARSE_H1 and self._earlier_in_flight(exchange):
                # Earlier pipelined requests still owe responses. Stop reading,
                # drop this request, and write the error (then close) only
                # after those responses, so it cannot be read as theirs.
                self.deferred_status = status
                self.deferred_body = message.encode("utf-8")
                self.deferred_head = exchange is not None and exchange._head_request
                if exchange is not None:
                    self._forget_pending(exchange)
                    exchange.c_abort()
                    if not exchange.handler_started:
                        exchange.cancel_before_start()
                    self.reading_exchange = None
                self._finish_protocol_span(status, span, method, path)
                self._set_pause_reason(PAUSE_REJECTED, True)
                return

            if exchange is not None and (
                exchange._status_code >= 0 or exchange._completed
            ):
                # The failing request already has its response on the wire,
                # or complete (a handler that answered before reading the
                # rest of a body that then turned out malformed). A second
                # status would be an unsolicited response that the client,
                # or a front end that reuses the connection, reads as the
                # answer to its next request. Its handler path owns the span.
                self._abort_reading_and_pending()
                transport.close()
                return

            # HTTP/2 setup failures must not emit HTTP/1 text. A response
            # already on the wire must not get a second status spliced in.
            if (
                self.parse_mode == PARSE_H2
                or (
                    self.active_exchange is not None
                    and self.active_exchange._status_code >= 0
                )
            ):
                self._abort_reading_and_pending()
                self._finish_protocol_span(status, span, method, path)
                transport.close()
                return

            body = message.encode("utf-8")
            # Write before abort so recycle cannot reuse the exchange
            # (or clear _head_request) while we still need it.
            self._write_h1_error(
                status,
                body,
                exchange is not None and exchange._head_request,
            )
            self._finish_protocol_span(status, span, method, path)
            self._abort_reading_and_pending()
            if status == 413:
                # The client is likely still sending the body.
                self.close_lingering()
            else:
                transport.close()
        except Exception:
            _log.exception("Failed to write a protocol error")
            try:
                if transport is not None:
                    transport.close()
            except Exception:
                pass

    cdef bint _earlier_in_flight(self, RequestExchange failing) noexcept:
        """True if a request parsed before ``failing`` still owes a response."""
        cdef RequestExchange active = self.active_exchange
        cdef RequestExchange pending
        if active is not None and active is not failing and not active._completed:
            return True
        for pending in self.pending_exchanges:
            if pending is not failing:
                return True
        return False

    cdef void _forget_pending(self, RequestExchange exchange) noexcept:
        try:
            self.pending_exchanges.remove(exchange)
        except ValueError:
            pass

    cdef void _start_h2(self) noexcept:
        cdef nghttp2_session_callbacks* cbs = NULL
        cdef nghttp2_option* opt = NULL
        cdef nghttp2_settings_entry iv[6]
        cdef int rv
        cdef size_t max_cont
        if self.h2 != NULL:
            self.parse_mode = PARSE_H2
            return
        self.parse_mode = PARSE_H2

        rv = nghttp2_session_callbacks_new(&cbs)
        if rv != 0:
            self._protocol_error(500, "Internal Server Error")
            return
        nghttp2_session_callbacks_set_on_begin_headers_callback(cbs, _h2_on_begin_headers)
        nghttp2_session_callbacks_set_on_header_callback(cbs, _h2_on_header)
        nghttp2_session_callbacks_set_on_data_chunk_recv_callback(cbs, _h2_on_data)
        nghttp2_session_callbacks_set_on_frame_recv_callback(cbs, _h2_on_frame_recv)
        nghttp2_session_callbacks_set_on_frame_send_callback(cbs, _h2_on_frame_send)
        nghttp2_session_callbacks_set_on_stream_close_callback(cbs, _h2_on_stream_close)
        nghttp2_session_callbacks_set_send_data_callback(cbs, _h2_send_data)

        rv = nghttp2_option_new(&opt)
        if rv != 0:
            nghttp2_session_callbacks_del(cbs)
            self._protocol_error(500, "Internal Server Error")
            return
        nghttp2_option_set_no_auto_window_update(opt, 1)
        nghttp2_option_set_no_closed_streams(opt, 1)
        nghttp2_option_set_stream_reset_rate_limit(
            opt, <uint64_t>H2_RST_BURST, <uint64_t>H2_RST_RATE
        )
        # HEADERS + CONTINUATION at 16KiB/frame. Cap at nghttp2's default 8 so
        # a large max_header_bytes does not loosen the flood guard.
        max_cont = <size_t>((self.max_header_bytes + 16383) // 16384) + 2
        if max_cont < 1:
            max_cont = 1
        if max_cont > 8:
            max_cont = 8
        nghttp2_option_set_max_continuations(opt, max_cont)

        rv = nghttp2_session_server_new2(&self.h2, cbs, <void*>self, opt)
        nghttp2_option_del(opt)
        nghttp2_session_callbacks_del(cbs)
        if rv != 0:
            self.h2 = NULL
            self._protocol_error(500, "Internal Server Error")
            return

        iv[0].settings_id = NGHTTP2_SETTINGS_INITIAL_WINDOW_SIZE
        iv[0].value = <uint32_t>H2_STREAM_WINDOW
        iv[1].settings_id = NGHTTP2_SETTINGS_MAX_CONCURRENT_STREAMS
        iv[1].value = <uint32_t>H2_MAX_CONCURRENT
        iv[2].settings_id = NGHTTP2_SETTINGS_ENABLE_PUSH
        iv[2].value = 0
        iv[3].settings_id = NGHTTP2_SETTINGS_MAX_HEADER_LIST_SIZE
        iv[3].value = <uint32_t>self.max_header_bytes
        iv[4].settings_id = NGHTTP2_SETTINGS_NO_RFC7540_PRIORITIES
        iv[4].value = 1
        iv[5].settings_id = NGHTTP2_SETTINGS_HEADER_TABLE_SIZE
        iv[5].value = <uint32_t>H2_HEADER_TABLE_SIZE
        nghttp2_submit_settings(self.h2, NGHTTP2_FLAG_NONE, iv, 6)
        nghttp2_session_set_local_window_size(
            self.h2, NGHTTP2_FLAG_NONE, 0, <int32_t>H2_CONN_WINDOW
        )
        self._h2_send()

    cdef void _h2_recv(self, const char* data, size_t n) noexcept:
        cdef ssize_t rv
        if self.h2 == NULL:
            return
        # Queue WINDOW_UPDATE / responses during this recv; one mem_send after
        # so multiplexed streams share a write instead of one flush each.
        self.h2_in_recv = True
        rv = nghttp2_session_mem_recv(self.h2, <const uint8_t*>data, n)
        self.h2_in_recv = False
        if rv < 0:
            # nghttp2 queues a GOAWAY for fatal errors: send it before closing.
            self._h2_send()
            self._protocol_error(400, "Invalid HTTP request")
            return
        self._h2_window_update(0, True)
        self._h2_send()

    cdef inline void _h2_window_update(self, int32_t stream_id, bint force=False) noexcept:
        """Ack recv credit. ``stream_id == 0`` is the connection window.

        Batching alone never fires for small POSTs; force-ack so keep-alive
        cannot stall once the window is exhausted.
        """
        cdef int32_t unacked
        cdef int32_t local = 0
        cdef int thresh
        cdef RequestExchange ex
        if self.h2 == NULL:
            return
        if stream_id == 0:
            unacked = nghttp2_session_get_effective_recv_data_length(self.h2)
            if self.h2_paused_streams > 0:
                # Bytes buffered for a handler that is not reading stay
                # unacked, so the connection window caps them.
                for ex in self.h2_streams.values():
                    if ex._h2_flow_paused:
                        unacked -= nghttp2_session_get_stream_effective_recv_data_length(
                            self.h2, ex._h2_stream_id
                        )
            thresh = H2_CONN_UPDATE_THRESH
            if not force:
                local = nghttp2_session_get_local_window_size(self.h2)
        else:
            unacked = nghttp2_session_get_stream_effective_recv_data_length(
                self.h2, stream_id
            )
            thresh = H2_STREAM_UPDATE_THRESH
            if not force:
                local = nghttp2_session_get_stream_local_window_size(
                    self.h2, stream_id
                )

        if unacked <= 0:
            return
        if not force and unacked < thresh and local >= thresh:
            return
        nghttp2_submit_window_update(
            self.h2, NGHTTP2_FLAG_NONE, stream_id, unacked
        )

    cdef int _h2_out_append(self, const char* src, Py_ssize_t n) noexcept:
        """Append to the grow-only outbound scratch; ``_h2_flush_out`` rewinds it."""
        cdef object buf = self.h2_out
        cdef Py_ssize_t used
        cdef Py_ssize_t cap
        if n <= 0:
            return 0
        used = self.h2_out_used
        cap = PyByteArray_GET_SIZE(buf)
        if used + n > cap:
            cap = used + n
            if cap < 256:
                cap = 256
            if PyByteArray_Resize(buf, cap) < 0:
                PyErr_Clear()
                return -1
        memcpy(PyByteArray_AS_STRING(buf) + used, src, <size_t>n)
        self.h2_out_used = used + n
        return 0

    cdef void _h2_flush_out(self) noexcept:
        """Move what nghttp2 wrote into ``h2_out`` onto the batch."""
        cdef Py_ssize_t used = self.h2_out_used
        if used <= 0:
            return
        self.h2_out_used = 0
        self.h2_batch.append(
            PyBytes_FromStringAndSize(PyByteArray_AS_STRING(self.h2_out), used)
        )

    cdef void _h2_write_batch(self) noexcept:
        """One transport call per send pass: every frame nghttp2 produced,
        HEADERS and DATA of all streams, in order."""
        cdef list batch = self.h2_batch
        cdef object transport = self.transport
        if not batch:
            return
        self.h2_batch = []
        if transport is None:
            return
        try:
            if len(batch) == 1:
                transport.write(batch[0])
            else:
                transport.writelines(batch)
        except Exception:
            _log.exception("HTTP/2 write failed; closing the connection")
            if not transport.is_closing():
                transport.close()

    cdef void _h2_send(self) noexcept:
        cdef const uint8_t* data = NULL
        cdef ssize_t n
        if self.h2_in_recv or self.h2_in_send:
            return
        if self.h2 == NULL or self.transport is None:
            return
        self.h2_in_send = True
        try:
            while True:
                n = nghttp2_session_mem_send(self.h2, &data)
                if n == 0:
                    break
                if n < 0:
                    self._protocol_error(400, "Invalid HTTP request")
                    return
                if self._h2_out_append(<const char*>data, n) != 0:
                    self._protocol_error(500, "Internal Server Error")
                    return
        finally:
            self.h2_in_send = False
        self._h2_flush_out()
        self._h2_write_batch()

        if (
            self.h2 != NULL
            and nghttp2_session_want_read(self.h2) == 0
            and nghttp2_session_want_write(self.h2) == 0
        ):
            # GOAWAY sent or received and no stream left: the session is done.
            if self.transport is not None and not self.transport.is_closing():
                self.transport.close()

    cdef RequestExchange _h2_stream_ex(self, int32_t stream_id):
        """Stream lookup: nghttp2's user_data is the fast path; the dict is fallback."""
        cdef void* ptr
        if self.h2 != NULL:
            ptr = nghttp2_session_get_stream_user_data(self.h2, stream_id)
            if ptr != NULL:
                return <RequestExchange>ptr
        return self.h2_streams.get(stream_id)

    cdef void _h2_dispatch_stream(self, RequestExchange ex) noexcept:
        if ex is None or ex._h2_dispatched or self.rejected:
            return
        if self.transport is None or self.transport.is_closing():
            return
        try:
            self._bind_request(ex)
            self._dispatch(ex)
            ex._h2_dispatched = True
        except Exception:
            _log.exception("Failed to dispatch request")
            self._h2_fail_stream(ex._h2_stream_id)

    cdef void _h2_begin_stream(self, int32_t stream_id):
        cdef RequestExchange ex
        if stream_id in self.h2_streams:
            return
        ex = self._take_exchange()
        ex.bind_http2(stream_id)
        self.h2_streams[stream_id] = ex
        if self.h2 != NULL:
            nghttp2_session_set_stream_user_data(self.h2, stream_id, <void*>ex)
        # Per-stream deadlines from here: the connection's HEADER or IDLE
        # timer stops while a stream is open.
        if self.timeout_kind != TIMEOUT_NONE:
            self._cancel_timeout()

    cdef bint _h2_header_oversize(
        self,
        RequestExchange ex,
        size_t namelen,
        size_t valuelen,
    ) noexcept:
        """True when this field would exceed ``max_header_bytes`` (H1 ``head_bytes``)."""
        cdef Py_ssize_t add
        cdef Py_ssize_t limit
        if ex is None:
            return True
        add = <Py_ssize_t>namelen + <Py_ssize_t>valuelen + HEADER_FIELD_OVERHEAD
        if add < 0:
            return True
        limit = <Py_ssize_t>self.max_header_bytes
        if ex._h2_head_bytes > limit - add:
            return True
        ex._h2_head_bytes += add
        return False

    cdef bint _h2_ignored_header(
        self,
        RequestExchange ex,
        size_t namelen,
        size_t valuelen,
    ) noexcept:
        """Count a field nobody reads; True once the block must stop."""
        cdef Py_ssize_t limit = <Py_ssize_t>self.max_header_bytes
        cdef Py_ssize_t add = (
            <Py_ssize_t>namelen + <Py_ssize_t>valuelen + HEADER_FIELD_OVERHEAD
        )
        if add < 0:
            return False
        if ex._h2_head_bytes <= 2 * limit - add:
            ex._h2_head_bytes += add
            return False
        self._h2_reject_stream(ex._h2_stream_id, NGHTTP2_ENHANCE_YOUR_CALM)
        return True

    cdef void _h2_protocol_status(self, RequestExchange ex, int status) noexcept:
        """Write a protocol HTTP status on this stream (431 / similar). No handler."""
        cdef object method
        cdef object path
        if ex is None or self.rejected:
            return
        ex._h2_awaiting_headers = False
        try:
            ex._h2_dispatched = True
            ex._h2_headers_done = True
            ex.mark_nobody()
            ex.start_response()
            method = ex._h2_method or ex._method
            if ex._req_url_length > 0:
                path = _span_path(
                    ex._req_arena + ex._req_url_offset, ex._req_url_length
                )
            else:
                path = None
            self._finish_protocol_span(status, ex.span, method, path)
            ex.respond(b"", b"text/plain; charset=utf-8", status)
            ex.handler_finished()
        except Exception:
            _log.exception("Failed to write an HTTP/2 protocol status")
            self._h2_reject_stream(ex._h2_stream_id, NGHTTP2_ENHANCE_YOUR_CALM)

    cdef bint _h2_on_header(
        self,
        int32_t stream_id,
        const uint8_t* name,
        size_t namelen,
        const uint8_t* value,
        size_t valuelen,
    ) except? -1:
        cdef RequestExchange ex = self._h2_stream_ex(stream_id)
        cdef const char* hn = <const char*>name
        cdef const char* hv = <const char*>value
        if ex is None:
            return False
        if ex._h2_dispatched or ex._h2_headers_done or ex._h2_headers_too_large:
            # Ignored fields (trailers, or a block already over the limit)
            # still cost nghttp2 a validation pass each, and an HPACK index
            # can name a 4 KiB value in one byte. Past twice the header
            # limit, stop the block instead of reading it out.
            return self._h2_ignored_header(ex, namelen, valuelen)
        if self._h2_header_oversize(ex, namelen, valuelen):
            ex._h2_headers_too_large = True
            ex._h2_head_bytes = <Py_ssize_t>self.max_header_bytes
            return False

        if namelen > 0 and name[0] == 58:
            # Pseudo-headers: validated here, bound at dispatch.
            if namelen == 7 and memcmp(hn, ":method", 7) == 0:
                if ex._h2_got_method:
                    self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                    return True
                if valuelen == 7 and memcmp(hv, "CONNECT", 7) == 0:
                    self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                    return True
                ex._h2_got_method = True
                ex._h2_method = _method_from_bytes(hv, valuelen)
                if ex._h2_method is METH_HEAD:
                    ex._head_request = True
                return False

            if namelen == 5 and memcmp(hn, ":path", 5) == 0:
                if ex._h2_got_path:
                    self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                    return True
                ex._h2_got_path = True
                if ex.append_request_url(hv, valuelen) != 0:
                    ex._h2_headers_too_large = True
                return False

            if namelen == 10 and memcmp(hn, ":authority", 10) == 0:
                if ex._h2_got_authority:
                    self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                    return True
                if not host_value_ok(hv, valuelen):
                    self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                    return True
                if ex._req_host_index >= 0:
                    if not ex.header_value_equals(ex._req_host_index, hv, valuelen):
                        self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                        return True
                    ex._h2_got_authority = True
                    return False
                ex._h2_got_authority = True
                if ex.append_request_header("host", 4, hv, valuelen, True) != 0:
                    ex._h2_headers_too_large = True
                return False

            if namelen == 9 and memcmp(hn, ":protocol", 9) == 0:
                # Extended CONNECT / WebSocket — not supported.
                self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                return True
            return False

        if not _h2_header_name_ok(hn, namelen):
            self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
            return True

        if namelen == 4 and memcmp(hn, "host", 4) == 0:
            if not host_value_ok(hv, valuelen):
                self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                return True
            if ex._req_host_index >= 0:
                if not ex.header_value_equals(ex._req_host_index, hv, valuelen):
                    self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
                    return True
                return False
            if ex.append_request_header(hn, namelen, hv, valuelen, True) != 0:
                ex._h2_headers_too_large = True
            return False
        if ex.append_request_header(hn, namelen, hv, valuelen, True) != 0:
            ex._h2_headers_too_large = True
        return False

    cdef void _h2_on_data(self, int32_t stream_id, const uint8_t* data, size_t length) except *:
        cdef RequestExchange ex = self._h2_stream_ex(stream_id)
        if ex is None or length == 0:
            return
        if ex.in_pool or ex._h2_stream_id != stream_id:
            return
        if not ex._body_active:
            if ex._h2_headers_done and not ex._h2_dispatched:
                self._h2_reject_stream(stream_id, NGHTTP2_PROTOCOL_ERROR)
            return

        if ex.c_feed(<const char*>data, length) != 0:
            nghttp2_submit_rst_stream(
                self.h2, NGHTTP2_FLAG_NONE, stream_id, NGHTTP2_INTERNAL_ERROR
            )
            return
        if not ex._h2_flow_paused:
            self._h2_window_update(stream_id)

        if ex._abort_reason == ABORT_TOO_LARGE and ex._status_code < 0:
            # Stream 413; never close the multiplexed connection from c_feed.
            # A running handler is reading: its read raises RequestBodyError
            # and the handler path answers 413, as on HTTP/1. Answering here
            # too would make that path a "response already started" failure.
            if not ex.handler_started:
                self._h2_protocol_status(ex, 413)
            return

        if (
            not ex._h2_dispatched
            and not self.rejected
            and ex._total_read > SMALL_BODY
        ):
            self._h2_dispatch_stream(ex)

    cdef void _h2_headers_frame(self, int32_t stream_id, bint end_stream):
        cdef RequestExchange ex = self._h2_stream_ex(stream_id)
        cdef Py_ssize_t content_length
        cdef bint expect
        if ex is None or self.rejected:
            return
        if ex._h2_dispatched or ex._h2_headers_done:
            # Trailers (or a later HEADERS) with END_STREAM finish the body.
            if end_stream:
                self._h2_end_stream(stream_id)
            return

        ex._h2_awaiting_headers = False
        if ex._h2_headers_too_large:
            self._h2_protocol_status(ex, 431)
            return

        ex.finish_request_header()
        ex.cache_hot_request_headers()
        content_length = ex._req_content_length
        # Only a length no read can accept; the handler applies the limit.
        if content_length > BODY_LIMIT_MAX:
            self._h2_protocol_status(ex, 413)
            return

        try:
            expect = ex._req_expect_continue
            # H2 bodies are END_STREAM-delimited; Content-Length is optional.
            # Arm upload state whenever DATA may still arrive (or 100-continue
            # / a declared length). mark_nobody only when this HEADERS already
            # ended the stream with nothing to read.
            if (not end_stream) or expect or content_length > 0:
                ex.reset_body(
                    expect,
                    content_length if content_length >= 0 else -1,
                )
                ex._h2_headers_done = True
                if end_stream and ex._body_active:
                    ex.c_complete()
                if (
                    not expect
                    and content_length > 0
                    and content_length <= SMALL_BODY
                    and not end_stream
                ):
                    return
            else:
                ex.mark_nobody()
                ex._h2_headers_done = True
            if not ex._h2_dispatched and not self.rejected:
                self._h2_dispatch_stream(ex)
        except Exception:
            _log.exception("Failed to dispatch request")
            self._h2_fail_stream(stream_id)

    cdef void _h2_end_stream(self, int32_t stream_id):
        cdef RequestExchange ex = self._h2_stream_ex(stream_id)
        # Return recv credit for this stream (and the connection share).
        # Batching alone never fires for small POSTs; without this, keep-alive
        # stalls once the connection window is exhausted.
        self._h2_window_update(stream_id, True)
        if ex is None:
            return
        if ex._body_active and not ex._body_complete:
            ex.c_complete()

        if not ex._h2_dispatched and not self.rejected:
            if not ex._h2_headers_done:
                self._h2_headers_frame(stream_id, True)
                return
            self._h2_dispatch_stream(ex)

    cdef void _h2_stream_closed(self, int32_t stream_id):
        cdef RequestExchange ex
        cdef object task
        if self.h2 != NULL:
            nghttp2_session_set_stream_user_data(self.h2, stream_id, NULL)
        ex = self.h2_streams.pop(stream_id, None)
        if ex is None:
            if not self.h2_streams and not self.rejected:
                self._arm_timeout(TIMEOUT_IDLE, self.keep_alive_timeout)
            return

        ex._h2_outbound = False
        if ex._h2_flow_paused:
            ex._h2_flow_paused = False
            self.h2_paused_streams -= 1
            self._h2_window_update(0, True)

        if not ex._completed:
            task = ex._handler_task
            ex.c_abort()
            if task is not None and not task.done():
                # alive() already cancelled through the stream disconnect
                # future; a second bare cancel here would escape __aexit__
                # as a foreign CancelledError, so defer it one tick.
                if ex._stream_disc is not None:
                    self.loop.call_soon(_cancel_unless_cancelling, task)
                else:
                    # Recycle from on_handler_done after CancelledError so
                    # the handler cannot touch a pooled exchange.
                    task.cancel()
                self._h2_count_cancelled_handler()
            elif not ex.handler_started:
                ex.cancel_before_start()
            else:
                ex._completed = True
                # A finished task whose done-callback is still queued
                # recycles from that callback, not here.
                if task is None or ex.handler_done:
                    ex.handler_finished()
        else:
            ex._maybe_recycle()

        if not self.h2_streams and not self.rejected:
            self._arm_timeout(TIMEOUT_IDLE, self.keep_alive_timeout)

    cdef void _h2_count_cancelled_handler(self):
        """Rate-limit streams the server had to give up on.

        Called for a stream that closes while its handler is running, and
        for a header block rejected or cut off mid-way.

        nghttp2's reset limit counts RST_STREAM frames the client sends. A
        client can also make the server reset a stream it already dispatched
        (MadeYouReset, CVE-2025-8671: a DATA frame past content-length,
        malformed trailers, a window overflow), which starts and cancels a
        handler per stream under any concurrency limit. Both kinds draw
        from this bucket; an empty one ends the connection.
        """
        cdef double now
        if self.h2 == NULL or self.rejected:
            return
        now = self.loop.time()
        self.h2_cancel_tokens += (now - self.h2_cancel_ts) * H2_RST_RATE
        if self.h2_cancel_tokens > H2_RST_BURST:
            self.h2_cancel_tokens = H2_RST_BURST
        self.h2_cancel_ts = now
        if self.h2_cancel_tokens >= 1.0:
            self.h2_cancel_tokens -= 1.0
            return
        # Queued here (this runs inside nghttp2 callbacks); the send that
        # follows every recv writes the GOAWAY, and the connection closes
        # once nghttp2 has nothing left to read or write.
        nghttp2_session_terminate_session(self.h2, NGHTTP2_ENHANCE_YOUR_CALM)

    cdef void _h2_final_goaway(self):
        self.h2_goaway_pending = False
        self.h2_goaway_deadline = 0.0
        if self.h2 == NULL:
            return
        nghttp2_submit_goaway(
            self.h2,
            NGHTTP2_FLAG_NONE,
            nghttp2_session_get_last_proc_stream_id(self.h2),
            NGHTTP2_NO_ERROR,
            NULL,
            0,
        )

    cdef void _h2_response_sent(self, int32_t stream_id):
        cdef RequestExchange ex = self.h2_streams.get(stream_id)
        if ex is None:
            return
        ex._h2_end_sent = True
        self._h2_reset_if_half_open(ex)

    cdef void _h2_reset_if_half_open(self, RequestExchange ex):
        # RFC 9113 8.1: the response is out and nobody will read the rest of
        # the request, so stop the client sending it (and pinning the stream).
        if self.h2 == NULL or not ex._h2_end_sent or not ex.handler_done:
            return
        if self.h2_streams.get(ex._h2_stream_id) is not ex:
            return
        if nghttp2_session_get_stream_remote_close(self.h2, ex._h2_stream_id) != 0:
            return
        nghttp2_submit_rst_stream(
            self.h2, NGHTTP2_FLAG_NONE, ex._h2_stream_id, NGHTTP2_NO_ERROR
        )

    cdef void h2_handler_finished(self, RequestExchange ex):
        self._h2_reset_if_half_open(ex)
        self._h2_send()

    cdef void _h2_fail_stream(self, int32_t stream_id) noexcept:
        """Reset one stream after a server-side failure; others keep going."""
        self._h2_reject_stream(stream_id, NGHTTP2_INTERNAL_ERROR)
        self._h2_send()

    cdef void _h2_reject_stream(self, int32_t stream_id, uint32_t error_code) noexcept:
        cdef RequestExchange ex = self.h2_streams.get(stream_id)
        if ex is not None:
            ex._h2_dispatched = True
        if self.h2 != NULL:
            nghttp2_submit_rst_stream(self.h2, NGHTTP2_FLAG_NONE, stream_id, error_code)

    cdef ssize_t _h2_read_data(
        self,
        RequestExchange ex,
        int32_t stream_id,
        uint8_t* buf,
        size_t length,
        uint32_t* data_flags,
    ) noexcept:
        cdef Py_ssize_t avail
        cdef Py_ssize_t take
        cdef object pending
        cdef const char* src = NULL
        cdef Py_ssize_t n = 0
        if ex is None or ex.in_pool or ex._h2_stream_id != stream_id:
            return NGHTTP2_ERR_TEMPORAL_CALLBACK_FAILURE
        pending = ex._h2_pending
        if pending is None or pending == b"":
            avail = 0
        elif _as_buf(pending, &src, &n):
            avail = n - ex._h2_pending_off
        else:
            avail = 0
        if avail <= 0:
            if ex._h2_body_done:
                data_flags[0] |= NGHTTP2_DATA_FLAG_EOF
                return 0
            # Nothing to send yet: park the provider until h2_write_data resumes it.
            return NGHTTP2_ERR_DEFERRED

        take = avail if avail < <Py_ssize_t>length else <Py_ssize_t>length
        # Payload stays in _h2_pending; send_data appends it into h2_out.
        data_flags[0] |= NGHTTP2_DATA_FLAG_NO_COPY
        if take == avail and ex._h2_body_done:
            data_flags[0] |= NGHTTP2_DATA_FLAG_EOF
        return <ssize_t>take

    cdef int _h2_send_data_frame(
        self,
        nghttp2_frame* frame,
        const uint8_t* framehd,
        size_t length,
        nghttp2_data_source* source,
    ) noexcept:
        cdef RequestExchange ex
        cdef list batch
        cdef Py_ssize_t off
        cdef Py_ssize_t n = 0
        cdef const char* src = NULL
        if source == NULL or source.ptr == NULL:
            return NGHTTP2_ERR_CALLBACK_FAILURE
        # We never ask nghttp2 to pad DATA; a padlen would need extra writes.
        if frame != NULL and frame.data.padlen > 0:
            return NGHTTP2_ERR_CALLBACK_FAILURE
        ex = <RequestExchange>source.ptr
        if ex.in_pool or (
            frame != NULL and ex._h2_stream_id != frame.hd.stream_id
        ):
            return NGHTTP2_ERR_CALLBACK_FAILURE

        if self.transport is None:
            return NGHTTP2_ERR_CALLBACK_FAILURE
        off = ex._h2_pending_off
        if length > 0:
            if not _as_buf(ex._h2_pending, &src, &n):
                return NGHTTP2_ERR_CALLBACK_FAILURE
            if off < 0 or off + <Py_ssize_t>length > n:
                return NGHTTP2_ERR_CALLBACK_FAILURE

        if length <= H2_INLINE_DATA_MAX:
            # Small frame: header and payload go into ``h2_out`` behind the
            # frames nghttp2 already wrote there, so a small response is one
            # bytes object and one ``write`` (no header bytes, memoryview, or
            # writelines per frame). The copy is cheaper than those objects.
            if self._h2_out_append(<const char*>framehd, 9) != 0:
                return NGHTTP2_ERR_CALLBACK_FAILURE
            if length > 0 and self._h2_out_append(
                src + off, <Py_ssize_t>length
            ) != 0:
                return NGHTTP2_ERR_CALLBACK_FAILURE
        else:
            # Frames nghttp2 wrote before this DATA go first; the payload is
            # queued behind its 9-byte header and written with the rest of
            # the pass by ``_h2_write_batch``. No transport call runs inside
            # this nghttp2 callback.
            self._h2_flush_out()
            batch = self.h2_batch
            batch.append(PyBytes_FromStringAndSize(<const char*>framehd, 9))
            if PyBytes_Check(ex._h2_pending):
                batch.append(
                    memoryview(ex._h2_pending)[off:off + <Py_ssize_t>length]
                )
            else:
                # A bytearray is extended in place by later writes: copy the slice.
                batch.append(
                    PyBytes_FromStringAndSize(src + off, <Py_ssize_t>length)
                )
        if length == 0:
            return 0

        ex._h2_pending_off = off + <Py_ssize_t>length
        ex._h2_sent_total += <Py_ssize_t>length
        if ex._drain_waiters and n - ex._h2_pending_off <= H2_DRAIN_LOW:
            wake_waiters(ex._drain_waiters)
        if ex._h2_pending_off >= n:
            ex._h2_pending = b""
            ex._h2_pending_off = 0
        return 0

    cdef int _h2_fill_nva(self, object pairs, nghttp2_nv* nvs, size_t maxn) except -1:
        cdef Py_ssize_t i, n, nn, vn
        cdef bytes name, value
        n = len(pairs)
        if n > <Py_ssize_t>maxn:
            n = <Py_ssize_t>maxn
        for i in range(n):
            name = pairs[i][0]
            value = pairs[i][1]
            nn = PyBytes_GET_SIZE(name)
            vn = PyBytes_GET_SIZE(value)
            nvs[i].name = <uint8_t*>PyBytes_AS_STRING(name)
            nvs[i].namelen = <size_t>nn
            nvs[i].value = <uint8_t*>PyBytes_AS_STRING(value)
            nvs[i].valuelen = <size_t>vn
            nvs[i].flags = NGHTTP2_NV_FLAG_NONE
        return <int>n

    cdef int _h2_fill_user_nvs(
        self,
        Headers h,
        nghttp2_nv* nvs,
        int cap,
        bint skip_ce,
        bint skip_cl,
        bint skip_ct,
    ) except -1:
        cdef Py_ssize_t i
        cdef Py_ssize_t n
        cdef object nb
        cdef object vb
        cdef char* np
        cdef char* vp
        cdef Py_ssize_t nl
        cdef Py_ssize_t vl
        cdef int filled = 0
        if h is None or cap <= 0:
            return 0

        n = h._n
        for i in range(n):
            nb = h._names[i]
            vb = h._values[i]
            np = PyBytes_AS_STRING(nb)
            nl = PyBytes_GET_SIZE(nb)
            vp = PyBytes_AS_STRING(vb)
            vl = PyBytes_GET_SIZE(vb)

            # Drop fields HTTP/2 must not carry: the three the caller covers
            # (content-type/length/encoding), the ones nva already holds
            # (date), and HTTP/1 hop-by-hop framing.
            if skip_ct and nl == 14 and memcmp(np, "content-type: ", 14) == 0:
                continue
            if skip_cl and nl == 16 and memcmp(np, "content-length: ", 16) == 0:
                continue
            if skip_ce and nl == 18 and memcmp(np, "content-encoding: ", 18) == 0:
                continue
            if nl == 6 and memcmp(np, "date: ", 6) == 0:
                continue
            if nl == 19 and memcmp(np, "transfer-encoding: ", 19) == 0:
                continue
            if nl == 12 and memcmp(np, "connection: ", 12) == 0:
                continue
            if nl == 12 and memcmp(np, "keep-alive: ", 12) == 0:
                continue
            if nl == 18 and memcmp(np, "proxy-connection: ", 18) == 0:
                continue
            if nl == 9 and memcmp(np, "upgrade: ", 9) == 0:
                continue

            # Stored lines end in CRLF; the nv lengths exclude it.
            if nl >= 2:
                nl -= 2
            if vl >= 2:
                vl -= 2
            if filled >= cap:
                break
            nvs[filled].name = <uint8_t*>np
            nvs[filled].namelen = <size_t>nl
            nvs[filled].value = <uint8_t*>vp
            nvs[filled].valuelen = <size_t>vl
            nvs[filled].flags = NGHTTP2_NV_FLAG_NONE
            filled += 1
        return filled

    cdef inline bint _h2_stream_live(self, RequestExchange ex) noexcept:
        """The stream is still mapped: a reset or released stream must not
        be written to, and must not mark the exchange outbound (it would
        never return to the pool)."""
        return self.h2_streams.get(ex._h2_stream_id) is ex

    cdef int _h2_submit(
        self,
        RequestExchange ex,
        object nva,
        bint with_user,
        bint skip_ce,
        bint skip_cl,
        bint skip_ct,
        bint interim,
        bint with_provider,
    ) except? -1:
        """Encode ``nva`` plus ``w.headers`` and submit them on the stream.

        ``interim`` sends a non-final HEADERS (100 Continue); otherwise this
        is the response, with the exchange as DATA provider when
        ``with_provider``. Returns nghttp2's result (0 on success, negative
        on a submit error), or 1 when the stream was already reset here
        because the header buffer could not be allocated.
        """
        cdef nghttp2_nv nvs_stack[64]
        cdef nghttp2_nv* nvs
        cdef int nvlen
        cdef int cap
        cdef int heap = 0
        cdef int rv
        cdef nghttp2_data_provider prd
        # Up to 64 header fields stay on the stack; a bigger set mallocs once.
        cap = <int>len(nva)
        if with_user and ex.headers is not None:
            cap += <int>ex.headers._n
        if cap < 8:
            cap = 8
        if cap <= 64:
            nvs = nvs_stack
        else:
            nvs = <nghttp2_nv*>malloc(sizeof(nghttp2_nv) * <size_t>cap)
            if nvs == NULL:
                # Reset this one stream (as the old per-call prologue did)
                # instead of raising into the handler's response path.
                self._h2_fail_stream(ex._h2_stream_id)
                return 1
            heap = 1
        try:
            nvlen = self._h2_fill_nva(nva, nvs, <size_t>cap)
            if with_user:
                nvlen += self._h2_fill_user_nvs(
                    ex.headers, nvs + nvlen, cap - nvlen, skip_ce, skip_cl, skip_ct
                )
            if interim:
                rv = nghttp2_submit_headers(
                    self.h2,
                    NGHTTP2_FLAG_NONE,
                    ex._h2_stream_id,
                    NULL,
                    nvs,
                    <size_t>nvlen,
                    NULL,
                )
            elif with_provider:
                prd.source.ptr = <void*>ex
                prd.read_callback = _h2_data_source_read
                rv = nghttp2_submit_response(
                    self.h2, ex._h2_stream_id, nvs, <size_t>nvlen, &prd
                )
            else:
                rv = nghttp2_submit_response(
                    self.h2, ex._h2_stream_id, nvs, <size_t>nvlen, NULL
                )
        finally:
            if heap:
                free(nvs)
        return rv

    cdef void _h2_submit_failed(self, RequestExchange ex, int rv):
        _log.error(
            "nghttp2 rejected response headers: %s",
            nghttp2_strerror(rv).decode(),
        )
        self._h2_fail_stream(ex._h2_stream_id)

    cdef void h2_respond(
        self,
        RequestExchange ex,
        object nva,
        object body,
        bint skip_ce=False,
        bint skip_cl=True,
        bint skip_ct=True,
    ):
        cdef object payload
        cdef int rv
        if self.h2 == NULL or ex is None or not self._h2_stream_live(ex):
            return
        if body is None:
            payload = b""
        elif PyBytes_Check(body):
            payload = body
        else:
            # Sent after respond() returns: never alias a mutable buffer.
            payload = bytes(body)
        if ex._head_request:
            payload = b""
        ex._h2_body_done = True
        if payload:
            ex._h2_pending = payload
            ex._h2_pending_off = 0
        rv = self._h2_submit(
            ex, nva, True, skip_ce, skip_cl, skip_ct, False, len(payload) > 0
        )
        if rv != 0:
            # rv == 1: the stream was already reset for an allocation failure.
            if rv < 0:
                self._h2_submit_failed(ex, rv)
            return
        ex._h2_outbound = True
        ex._h2_headers_sent = True
        self._h2_send()

    cdef void h2_write_headers(
        self,
        RequestExchange ex,
        object nva,
        bint skip_ce=False,
        bint skip_cl=False,
        bint skip_ct=False,
        bint skip_user=False,
    ):
        cdef int rv
        if self.h2 == NULL or ex is None or not self._h2_stream_live(ex):
            return
        if nva and nva[0][0] == b":status" and nva[0][1] == b"100":
            # Interim 100-continue: non-final HEADERS; the real response follows.
            self._h2_submit(
                ex, nva, not skip_user, skip_ce, skip_cl, skip_ct, True, False
            )
            self._h2_send()
            return
        if ex._h2_headers_sent:
            return
        if ex._head_request:
            ex._h2_body_done = True
        rv = self._h2_submit(
            ex,
            nva,
            not skip_user,
            skip_ce,
            skip_cl,
            skip_ct,
            False,
            not ex._head_request,
        )
        if rv != 0:
            if rv < 0:
                self._h2_submit_failed(ex, rv)
            return
        ex._h2_headers_sent = True
        ex._h2_outbound = True
        self._h2_send()

    cdef void _h2_pending_extend(self, RequestExchange ex, object data) noexcept:
        cdef object pending = ex._h2_pending
        cdef Py_ssize_t off
        cdef Py_ssize_t keep
        cdef Py_ssize_t n
        if pending == b"" or pending is None:
            # A caller's bytearray is copied: it is extended in place below
            # and may be reused by the caller before nghttp2 sends it.
            if PyBytes_Check(data):
                ex._h2_pending = data
            else:
                pending = bytearray()
                pending.extend(data)
                ex._h2_pending = pending
            ex._h2_pending_off = 0
            return

        off = ex._h2_pending_off
        if not PyByteArray_Check(pending):
            # bytes cannot grow: copy the unsent tail into a bytearray, append.
            n = PyBytes_GET_SIZE(pending) if PyBytes_Check(pending) else 0
            keep = n - off if n > off else 0
            pending = bytearray()
            if keep:
                if PyByteArray_Resize(pending, keep) < 0:
                    PyErr_Clear()
                    return
                memcpy(
                    PyByteArray_AS_STRING(pending),
                    PyBytes_AS_STRING(ex._h2_pending) + off,
                    <size_t>keep,
                )
            ex._h2_pending = pending
            ex._h2_pending_off = 0
            pending.extend(data)
            return

        if off:
            # Compact the sent prefix out of the bytearray only once it is at
            # least half of it, so a slow reader costs one move per two bytes
            # sent instead of one move of the whole backlog per DATA frame.
            n = PyByteArray_GET_SIZE(pending)
            if off * 2 < n:
                pending.extend(data)
                return
            keep = n - off if n > off else 0
            if keep:
                memmove(
                    PyByteArray_AS_STRING(pending),
                    PyByteArray_AS_STRING(pending) + off,
                    <size_t>keep,
                )
            if PyByteArray_Resize(pending, keep) < 0:
                PyErr_Clear()
                return
            ex._h2_pending_off = 0
        pending.extend(data)

    cdef void h2_write_data(self, RequestExchange ex, object data, bint end):
        if self.h2 == NULL or ex is None or ex.in_pool:
            return
        if not self._h2_stream_live(ex):
            return
        if data:
            self._h2_pending_extend(ex, data)
        if end:
            ex._h2_body_done = True
        nghttp2_session_resume_data(self.h2, ex._h2_stream_id)
        self._h2_send()

    cdef void h2_end(self, RequestExchange ex):
        self.h2_write_data(ex, None, True)

    cdef void h2_abort(self, RequestExchange ex):
        if self.h2 == NULL or ex is None or not self._h2_stream_live(ex):
            return
        nghttp2_submit_rst_stream(
            self.h2, NGHTTP2_FLAG_NONE, ex._h2_stream_id, NGHTTP2_INTERNAL_ERROR
        )
        self._h2_send()

    cdef object _get_buffer(self, Py_ssize_t sizehint):
        """Reusable read buffer. Consume it in buffer_updated before reuse.

        SSL/uvloop want a writable memoryview. Cache one for the current
        bytearray so a keep-alive GET does not allocate a view every read.
        """
        cdef Py_ssize_t want = sizehint
        if want < 64 * 1024:
            want = 64 * 1024
        if want > 256 * 1024:
            want = 256 * 1024
        if self._in_buf is None or PyByteArray_GET_SIZE(self._in_buf) < want:
            # Skip zero-fill: only pages the kernel writes become resident,
            # so an idle keep-alive connection costs ~a page, not 64 KiB.
            # We never read past ``nbytes``.
            self._in_buf = PyByteArray_FromStringAndSize(NULL, want)
            self._in_view = memoryview(self._in_buf)
        # Return the whole cached view: ``want`` only bounds growth, and a
        # slice would allocate one memoryview per sub-capacity read. uvloop
        # reports what it actually wrote in ``buffer_updated``.
        return self._in_view

    cdef void _buffer_updated(self, Py_ssize_t nbytes):
        cdef const char* ptr
        if self.rejected or nbytes <= 0 or self._in_buf is None:
            return
        if self.parse_mode != PARSE_H2 and self.parser == NULL:
            return
        ptr = PyByteArray_AS_STRING(self._in_buf)
        if self.timeout_kind == TIMEOUT_IDLE:
            self._refresh_idle(ptr, nbytes)
        if self.held_data is not None:
            self._held_append(PyBytes_FromStringAndSize(ptr, nbytes))
            return
        if self.pause_reasons:
            self.held_data = PyBytes_FromStringAndSize(ptr, nbytes)
            self.held_offset = 0
            return
        self._pump_ptr(ptr, nbytes)
        self._after_pump()

    cdef void _pump_ptr(self, const char* ptr, Py_ssize_t n) noexcept:
        """Route inbound bytes: the h2 session, the preface sniff, or llhttp."""
        if n <= 0:
            return
        if self.parse_mode == PARSE_H2:
            self._h2_recv(ptr, <size_t>n)
            return
        if self.parse_mode == PARSE_NONE:
            if not self._maybe_start_h2(ptr, n):
                return
            if self.parse_mode == PARSE_H2:
                self._h2_recv(ptr, <size_t>n)
                return
            if self.preface_hold:
                self._h1_execute_held()
                return
        self._h1_execute_ptr(ptr, n)

    cdef void _h1_execute_ptr(self, const char* ptr, Py_ssize_t n) noexcept:
        cdef Py_ssize_t offset = 0
        cdef Py_ssize_t end
        cdef int err
        if self.parser == NULL or n <= 0:
            return
        if n <= PARSER_QUANTUM:
            err = llhttp_execute(self.parser, ptr, <size_t>n)
            if err != HPE_OK:
                self._protocol_error(400, "Invalid HTTP request")
            return
        # Bound each execute so a pause flag set by a callback can stop the
        # rest of a large read; the remainder is held, not lost.
        while offset < n:
            end = offset + PARSER_QUANTUM
            if end > n:
                end = n
            err = llhttp_execute(self.parser, ptr + offset, <size_t>(end - offset))
            if err != HPE_OK:
                self._protocol_error(400, "Invalid HTTP request")
                return
            offset = end
            if self.pause_reasons:
                if offset < n:
                    self.held_data = PyBytes_FromStringAndSize(ptr + offset, n - offset)
                    self.held_offset = 0
                return


class HttpProtocol(CHttpProtocol, asyncio.BufferedProtocol):
    """Python subclass so uvloop/asyncio can find BufferedProtocol methods."""

    def get_buffer(self, sizehint):
        return (<CHttpProtocol>self)._get_buffer(sizehint)

    def buffer_updated(self, nbytes):
        (<CHttpProtocol>self)._buffer_updated(nbytes)
