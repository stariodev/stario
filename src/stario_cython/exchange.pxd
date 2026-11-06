from libc.stddef cimport size_t
from libc.stdint cimport int32_t, uint8_t, uint32_t, uint64_t

from stario_cython.compression_buf cimport StarioBrotli, StarioGzip, StarioZstd

cdef inline void _lower_copy(
    char* dst,
    const char* src,
    size_t n,
) noexcept:
    cdef size_t i
    cdef uint8_t ch
    for i in range(n):
        ch = <uint8_t>src[i]
        if 65 <= ch <= 90:
            ch += 32
        dst[i] = <char>ch

cdef enum:
    HEADER_NAME_STACK = 256
    ABORT_NONE = 0
    ABORT_TOO_LARGE = 1
    ABORT_DISCONNECTED = 2
    ABORT_TIMEOUT = 3
    # Bodies this size dispatch once complete (body() is already bytes).
    # An unread declared body this small is drained on keep-alive
    # instead of closing the connection.
    SMALL_BODY = 256 * 1024
    # The largest body a read can accept (the limits are C ints).
    # ``RequestPolicy.max_body_bytes`` is only the default for one read.
    BODY_LIMIT_MAX = 2147483647

# ``scan_request_path`` result bits (a negative result means 400).
cdef enum:
    PATH_HAS_PCT = 1
    PATH_REDIRECT = 2
    PATH_RAW_UTF8 = 4
    # Set by the protocol, never by the scanner.
    PATH_BAD = 8
    PATH_OPTIONS_STAR = 16

cdef int _fold_header_name(object name, char* buf, Py_ssize_t* out_n) except -1
cdef object _intern_name(const char* src, size_t n)
cdef object _encode_name(str name)
cdef bint host_value_ok(const char* value, size_t n) noexcept
cdef object _host_without_port_n(const char* s, Py_ssize_t n)
cdef int scan_request_path(const char* p, Py_ssize_t n, bint allow_raw_utf8) noexcept
cdef bint query_has_nul(const char* s, Py_ssize_t n) noexcept
cdef object decode_path_full(const char* p, Py_ssize_t n)
cdef bytes canonical_path(const char* p, Py_ssize_t n)
cdef void wake_waiters(list waiters) noexcept

cdef class Headers:
    cdef list _names
    cdef list _values
    cdef Py_ssize_t _n

    cdef Py_ssize_t _find_n(self, const char* name, Py_ssize_t n) noexcept
    cdef Py_ssize_t _compact_except(self, const char* name, Py_ssize_t n) noexcept
    cdef void _store_at(self, Py_ssize_t index, object wire, object line)
    cdef object c_get(self, object name)
    cdef void c_set(self, object name, object value)
    cdef void c_add(self, object name, object value)
    cdef void c_remove(self, object name)
    cdef void c_clear(self)
    cdef bint c_empty(self)
    cdef bint c_vary_contains(self, object token)
    cdef void c_merge_vary(self, object token)
    cdef int _add_ba(self, object buf, Py_ssize_t* length, const char* src, Py_ssize_t n) except -1
    cdef int _write_pair_at(self, object buf, Py_ssize_t* length, Py_ssize_t index) except -1
    cdef int c_write_wire_ba(self, object buf, Py_ssize_t* length) except -1
    cdef object c_scan_respond(self, object content_type)
    cdef void c_require_respond_length(self, object existing_cl, object expected) except *
    cdef int c_write_respond_pairs(
        self,
        object buf,
        Py_ssize_t* length,
        bint skip_ce,
    ) except -1

ctypedef struct RawHeader:
    uint32_t name_offset
    uint32_t name_length
    uint32_t value_offset
    uint32_t value_length

cdef object _status_line(int status)

cdef class ParsedCookies:
    cdef object _headers
    cdef list _lines
    cdef object _parsed

    cdef void bind_request_headers(self, object headers) noexcept
    cdef void _extend_lines(self, object lines) except *
    cdef list _cookie_lines(self)
    cdef object _get_arena(self, const char* name, Py_ssize_t nlen)
    cdef object _get_lines(self, const char* name, Py_ssize_t nlen)
    cdef bint _has_any(self)

cdef class RequestExchange
cdef class RequestHeaders
cdef class RequestHandle

cdef class Request:
    cdef public object method
    cdef public object path
    cdef readonly RequestHeaders headers
    cdef public object protocol_version
    cdef public bint keep_alive
    cdef object _query_bytes
    cdef object _raw_path
    cdef RequestHeaders _view
    cdef Py_ssize_t _p_off
    cdef Py_ssize_t _p_len
    cdef Py_ssize_t _q_off
    cdef Py_ssize_t _q_len
    cdef object _body
    # Default read limit once the body is detached bytes.
    cdef Py_ssize_t _body_limit
    cdef object _query
    cdef object _cookies
    cdef object _host

    cdef object _materialize_query(self)
    cdef object _ensure_query(self)
    cdef void prefetch_host(self) noexcept
    cdef void detach_body(self, RequestExchange exchange) noexcept

cdef Request make_request(
    object method,
    object path,
    object protocol_version,
    bint keep_alive,
    RequestHeaders headers,
    object body,
)

cdef class HttpConnection:
    """``cdef`` hooks implemented by ``CHttpProtocol``. Per-request flags live on RequestExchange."""
    cdef public int timeout_cleanup
    cdef void release_exchange(self, RequestExchange exchange)
    cdef void response_completed(self, RequestExchange exchange)
    cdef void close_lingering(self)
    cdef object date_line(self)
    cdef void set_body_paused(self, RequestExchange exchange, bint paused)
    cdef object ensure_disconnect(self)
    cdef object ensure_stream_disconnect(self, RequestExchange ex)
    cdef object drain_waiter(self, RequestExchange exchange)
    cdef void h2_respond(
        self,
        RequestExchange ex,
        object nva,
        object body,
        bint skip_ce=*,
        bint skip_cl=*,
        bint skip_ct=*,
    )
    cdef void h2_write_headers(
        self,
        RequestExchange ex,
        object nva,
        bint skip_ce=*,
        bint skip_cl=*,
        bint skip_ct=*,
        bint skip_user=*,
    )
    cdef void h2_write_data(self, RequestExchange ex, object data, bint end)
    cdef void h2_end(self, RequestExchange ex)
    cdef void h2_abort(self, RequestExchange ex)
    cdef void h2_handler_finished(self, RequestExchange ex)

cdef class AppState:
    cdef public bint host_routing
    cdef public bint shutting_down
    cdef public object router

cdef class CNode

cdef class CEdge:
    cdef object key
    cdef const char* key_p
    cdef Py_ssize_t key_n
    cdef CNode child

cdef class CNode:
    cdef list edges
    cdef Py_ssize_t n_edges
    cdef CEdge one_edge
    cdef int edge_start[256]
    cdef int edge_count[256]
    cdef object wildcard_name
    cdef CNode wildcard
    cdef object catchall_name
    cdef CNode catchall
    cdef dict endpoints
    cdef object one_method
    cdef object one_hit
    cdef object nf_hit
    cdef object mna_hit
    cdef object method_set
    cdef object not_found
    cdef object method_na
    cdef bint not_found_custom

cdef class CRouter:
    cdef CNode path
    cdef dict hosts_exact
    cdef CNode hosts_param
    cdef bint host_routing
    cdef bint has_param_hosts
    cdef object c_lookup(self, object host, object path, object method)
    cdef void c_lookup_into(
        self,
        object host,
        const char* path_p,
        Py_ssize_t path_n,
        bint decode,
        object method,
        RequestExchange exchange,
    )

cpdef CRouter compile_router(object router)

cdef class RequestExchange:
    # HttpConnection-owned writer targets (bound in reset(), live across keep-alive).
    cdef object _transport
    cdef object _t_is_closing
    cdef object _t_writelines
    cdef object _compression
    # Request-scoped flags reused by dispatch, body, and the writer.
    cdef int _req_encoding
    cdef bint _req_expect_continue
    cdef bint _req_connection_close
    cdef Py_ssize_t _req_content_length
    cdef char* _req_arena
    cdef Py_ssize_t _req_arena_len
    cdef Py_ssize_t _req_arena_cap
    cdef RawHeader* _req_raw_headers
    cdef Py_ssize_t _req_raw_count
    cdef Py_ssize_t _req_raw_headers_cap
    cdef Py_ssize_t _req_pending_name_offset
    cdef Py_ssize_t _req_pending_name_length
    cdef Py_ssize_t _req_pending_value_offset
    cdef Py_ssize_t _req_pending_value_length
    cdef bint _req_pending_header
    cdef Py_ssize_t _req_host_index
    cdef Py_ssize_t _req_cookie_index
    cdef Py_ssize_t _req_authorization_index
    cdef Py_ssize_t _req_url_offset
    cdef Py_ssize_t _req_url_length
    cdef bint _req_accept_present
    cdef int _req_br_q
    cdef int _req_zstd_q
    cdef int _req_gzip_q
    cdef int _req_wildcard_q
    cdef int _req_identity_q
    cdef public Headers headers
    cdef StarioBrotli* _brotli
    cdef StarioZstd* _zstd
    cdef StarioGzip* _gzip
    cdef StarioZstd* _zstd_1s
    cdef StarioGzip* _gzip_1s
    cdef object _out_buf
    cdef Py_ssize_t _out_len
    cdef int _status_code
    cdef Py_ssize_t _declared_length
    # HTTP/1.0 streaming: no chunked coding, the body ends when we close.
    cdef bint _close_delimited
    cdef Py_ssize_t _bytes_written
    cdef bint _brotli_enabled
    cdef bint _zstd_enabled
    cdef bint _gzip_enabled
    cdef int _enc_order[3]
    cdef int _brotli_level
    cdef int _brotli_window
    cdef int _zstd_level
    cdef int _zstd_window
    cdef int _gzip_level
    cdef int _gzip_window
    cdef int _s_brotli_level
    cdef int _s_brotli_window
    cdef int _s_brotli_lgblock
    cdef int _s_zstd_window
    cdef int _s_zstd_hash
    cdef int _s_zstd_chain
    cdef int _s_gzip_window
    cdef int _s_gzip_mem
    cdef Py_ssize_t _compress_min_size
    cdef bint _completed

    cdef public object app
    cdef public object span
    cdef public object match
    cdef HttpConnection _connection
    cdef RequestHeaders _req_view
    cdef RequestHandle _handle
    # Unreferenced leftovers from the previous request, reused as-is.
    cdef RequestHeaders _spare_view
    cdef RequestHandle _spare_handle
    cdef Request _req
    cdef object _method
    cdef object _path
    cdef object _version
    cdef object _target_host
    # HTTP/1 keep-alive for this request. Set at headers-complete.
    # Recycle resets it; snapshot first if the connection still needs it.
    cdef bint _keep_alive
    cdef int _path_flags
    # Protocol-level answer (keep-alive 413/431) sent in order at dispatch.
    cdef int _protocol_status
    cdef object _protocol_body
    cdef Py_ssize_t _path_off
    cdef Py_ssize_t _path_n
    cdef Py_ssize_t _query_off
    cdef Py_ssize_t _query_len
    cdef object _handler
    cdef object _route
    cdef bint handler_done
    cdef bint handler_started
    cdef bint in_pool

    cdef object _cached
    cdef object _data_ready
    # Bumped on detach: body readers from an earlier request stop reading.
    cdef unsigned long _generation
    cdef double _stall_deadline
    cdef uint64_t _stall_touch
    cdef uint64_t _stall_seen
    cdef int _total_read
    cdef int _max_size
    cdef Py_ssize_t _read_max_size
    cdef double _timeout
    cdef int _consumed_as
    cdef int _abort_reason
    cdef bint _body_active
    cdef bint _body_complete
    cdef bint _http2
    cdef int32_t _h2_stream_id
    cdef object _h2_pending
    cdef Py_ssize_t _h2_pending_off
    cdef bint _h2_body_done
    cdef object _h2_method
    cdef bint _h2_got_method
    cdef bint _h2_got_path
    cdef bint _h2_got_authority
    cdef bint _h2_dispatched
    cdef bint _h2_headers_done
    cdef bint _h2_headers_sent
    cdef bint _h2_headers_too_large
    cdef Py_ssize_t _h2_head_bytes
    cdef bint _h2_awaiting_headers
    cdef double _h2_header_deadline
    cdef bint _h2_outbound
    # END_STREAM for the response has left nghttp2.
    cdef bint _h2_end_sent
    # Body consumer is behind: hold this stream's WINDOW_UPDATE (HTTP/2).
    cdef bint _h2_flow_paused
    # ``w.drain()`` waiters for this stream's unsent DATA (HTTP/2).
    cdef list _drain_waiters
    # Per-stream response send progress for the flow-control stall sweep.
    cdef Py_ssize_t _h2_sent_total
    cdef Py_ssize_t _h2_send_mark
    cdef double _h2_send_deadline
    # Deferred request bodies: bytes counted at the last body sweep (HTTP/2).
    cdef Py_ssize_t _h2_body_mark
    # Stream-scoped disconnect future, resolved when this stream dies (HTTP/2).
    cdef object _stream_disc
    cdef object _h2_date_line
    cdef object _h2_date_bare
    cdef object _handler_task
    cdef bint _head_request
    cdef bint _expect_continue
    cdef bint _waiting
    cdef bint _discard_body
    cdef object _body_buf
    cdef Py_ssize_t _body_used
    cdef Py_ssize_t _body_cap
    cdef Py_ssize_t _expected_size
    cdef Py_ssize_t _stream_max_chunk

    cdef void reset(
        self,
        HttpConnection connection,
        object app,
        object transport,
        object compression,
        int max_body_size,
        double body_timeout,
    ) noexcept
    cdef void bind_http2(self, int32_t stream_id) noexcept
    cdef object _h2_date_value(self)
    cdef void _h2_respond(self, object body, object content_type, int status, Py_ssize_t nbytes)
    cdef void start_response(self)
    cdef void _clear_request_binding(self) noexcept
    cdef const char* path_ptr(self, Py_ssize_t* n) noexcept
    cdef RequestHeaders _ensure_view(self)
    cdef Request ensure_request(self)
    cdef RequestHandle start_handle(self)
    cdef void _detach_views(self)
    cdef object decode_request_path(self)
    cdef object host_from_arena(self)
    cpdef void respond(self, object body, object content_type, int status=*)
    cdef int _stream_encoding(self, Headers headers, bint allocate) except -1
    cpdef object write_headers(self, int status_code)
    cpdef object write(self, object data)
    cpdef void end(self, object data=*)
    cpdef void abort(self)
    cdef void handler_finished(self)
    cdef void cancel_before_start(self)
    cdef void _maybe_recycle(self)
    cdef void detach(self)
    cdef void recycle(self)
    cdef void reset_body(self, bint expect_continue, Py_ssize_t expected_size) noexcept
    cdef void mark_nobody(self) noexcept
    cdef int _reserve_request_arena(self, Py_ssize_t bytes_needed) noexcept
    cdef int _reserve_request_headers(self) noexcept
    cdef int append_request_url(self, const char* data, size_t length) noexcept
    cdef int append_request_header_name(self, const char* data, size_t length) noexcept
    cdef int append_request_header_value(self, const char* data, size_t length) noexcept
    cdef int append_request_header(
        self,
        const char* name,
        size_t name_length,
        const char* value,
        size_t value_length,
        bint names_already_lower,
    ) noexcept
    cdef bint header_value_equals(
        self,
        Py_ssize_t index,
        const char* value,
        size_t n,
    ) noexcept
    cdef int finish_request_header(self) noexcept
    cdef int _commit_request_header(self) noexcept
    cdef void _scan_request_accept_encoding(
        self,
        const char* value,
        size_t length,
    ) noexcept
    cdef void _clear_request_headers(self) noexcept
    cdef void _clear_hot_request_headers(self) noexcept
    cdef void cache_hot_request_headers(self) noexcept
    cdef int c_feed(self, const char* at, size_t length) noexcept
    cdef int c_complete(self) noexcept
    cdef void c_abort(self)
    cdef void _clear_body_storage(self) noexcept
    cdef char* _body_ptr(self) noexcept
    cdef int _body_reserve(self, Py_ssize_t need) noexcept
    cdef int _adopt_expected_body_buffer(self) noexcept
    cdef object _body_to_bytes(self)
    cdef object _body_take(self, Py_ssize_t max_n)
    cdef void reset_response(self, int encoding)
    cdef void _apply_compression(self, object compression)
    cdef int _buf_add(self, const char* src, Py_ssize_t n) except -1
    cdef int _buf_bytes(self, object data) except -1
    cdef int _buf_uint(self, size_t n, int base) except -1
    cdef void _flush(self)
    cdef Py_ssize_t _body_nbytes(self, object body) except -2
    cdef object _body_as_bytes(self, object body)
    cdef bint _may_compress(
        self,
        object data,
        object content_type,
        bint streaming,
        Py_ssize_t nbytes,
    )
    cdef int _frame(self, object data, object encoding, const unsigned char** out, size_t* out_len) except -1
    cdef int _block(self, object data, const unsigned char** out, size_t* out_len) except -1
    cdef int _finish(self, const unsigned char** out, size_t* out_len) except -1
    cdef int _write_native_chunk(self, const unsigned char* data, size_t n) except -1
    cdef int _block_raw(self, const char* ptr, size_t n, const unsigned char** out, size_t* out_len) except -1
    cdef int _ensure_brotli(self) except -1
    cdef int _ensure_zstd(self) except -1
    cdef int _ensure_gzip(self) except -1
    cdef int _ensure_zstd_1s(self) except -1
    cdef int _ensure_gzip_1s(self) except -1
    cdef void _free_compressors(self)
    cdef void _raise_abort(self)
    cdef void _wake(self)
    cdef void _cancel_stall_timer(self) noexcept
    cdef void _reset_stall_timer(self) noexcept
    cdef void fire_body_stall(self)
    cdef void _maybe_continue(self)
    cdef void _done(self)
    cdef void _finish_head(self)
    cdef void _maybe_pause(self)
    cdef Py_ssize_t _feed_limit(self) noexcept
    cdef Py_ssize_t _read_limit(self, object max_size) except -1

cdef class RequestHeaders:
    # Live view of the exchange arena while the request runs; owns a copy
    # once the exchange is recycled and something still holds this view.
    cdef RequestExchange _ex
    cdef char* _own_arena
    cdef Py_ssize_t _own_arena_len
    cdef RawHeader* _own_headers
    cdef Py_ssize_t _own_count
    cdef Py_ssize_t _own_host
    cdef Py_ssize_t _own_cookie
    cdef Py_ssize_t _own_auth

    cdef const char* _arena(self) noexcept
    cdef Py_ssize_t _arena_len(self) noexcept
    cdef RawHeader* _headers(self) noexcept
    cdef Py_ssize_t _count(self) noexcept
    cdef Py_ssize_t _host_index(self) noexcept
    cdef Py_ssize_t _cookie_index(self) noexcept
    cdef Py_ssize_t _auth_index(self) noexcept
    cdef void _take_ownership(self) noexcept
    cdef int _own_pairs(self, list pairs) except -1
    cdef object c_get(self, object name)
    cdef Py_ssize_t c_find_n(self, const char* query, Py_ssize_t query_length) noexcept
    cdef object c_value_str(self, Py_ssize_t index)
    cdef object c_get_n(self, const char* query, Py_ssize_t query_length)
    cdef object c_getlist_n(self, const char* query, Py_ssize_t query_length)
    cdef object c_request_indexed(self, Py_ssize_t index)
    cdef void c_parse_cookies(self, dict out) except *

cdef class RequestHandle:
    # Per-request ``c`` / ``w``. ``_ex`` is None once the exchange is recycled.
    cdef RequestExchange _ex
    cdef HttpConnection _conn
    # Disconnect future snapshotted at detach; `_conn` is None by then.
    cdef object _disc
    cdef public object app
    cdef public object span
    cdef public object match
    cdef object _state
    cdef Request _req
    cdef int _final_status

    cpdef void respond(self, object body, object content_type, int status=*)

cdef RequestExchange acquire_exchange(
    HttpConnection connection,
    object app,
    object transport,
    object compression,
    int max_body_size,
    double body_timeout,
)
