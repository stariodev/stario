from libc.stddef cimport size_t
from libc.stdint cimport int32_t, uint8_t, uint32_t, uint64_t

cdef extern from *:
    """
    /* nghttp2.h uses ssize_t, which MSVC does not define (POSIX type), and
       Python.h defines only Py_ssize_t there. Must precede nghttp2.h. */
    #if defined(_MSC_VER)
    #include <BaseTsd.h>
    typedef SSIZE_T ssize_t;
    #endif
    """
    # Pointer-sized and signed everywhere; ``long`` is 32 bits on Win64.
    ctypedef Py_ssize_t ssize_t

cdef extern from "nghttp2/nghttp2.h":
    cdef enum:
        NGHTTP2_FLAG_NONE
        NGHTTP2_FLAG_END_STREAM
        NGHTTP2_DATA
        NGHTTP2_HEADERS
        NGHTTP2_PING
        NGHTTP2_FLAG_ACK
        NGHTTP2_NO_ERROR
        NGHTTP2_PROTOCOL_ERROR
        NGHTTP2_INTERNAL_ERROR
        NGHTTP2_CANCEL
        NGHTTP2_ENHANCE_YOUR_CALM
        NGHTTP2_ERR_CALLBACK_FAILURE
        NGHTTP2_ERR_TEMPORAL_CALLBACK_FAILURE
        NGHTTP2_ERR_DEFERRED
        NGHTTP2_DATA_FLAG_EOF
        NGHTTP2_DATA_FLAG_NO_COPY
        NGHTTP2_NV_FLAG_NONE
        NGHTTP2_SETTINGS_HEADER_TABLE_SIZE
        NGHTTP2_SETTINGS_ENABLE_PUSH
        NGHTTP2_SETTINGS_MAX_CONCURRENT_STREAMS
        NGHTTP2_SETTINGS_INITIAL_WINDOW_SIZE
        NGHTTP2_SETTINGS_MAX_HEADER_LIST_SIZE
        NGHTTP2_SETTINGS_NO_RFC7540_PRIORITIES

    ctypedef struct nghttp2_option:
        pass

    ctypedef struct nghttp2_settings_entry:
        int32_t settings_id
        uint32_t value

    ctypedef struct nghttp2_session:
        pass

    ctypedef struct nghttp2_session_callbacks:
        pass

    ctypedef struct nghttp2_nv:
        uint8_t* name
        uint8_t* value
        size_t namelen
        size_t valuelen
        uint8_t flags

    ctypedef struct nghttp2_frame_hd:
        size_t length
        int32_t stream_id
        uint8_t type
        uint8_t flags
        uint8_t reserved

    ctypedef struct nghttp2_headers:
        nghttp2_frame_hd hd
        int cat

    ctypedef struct nghttp2_data:
        nghttp2_frame_hd hd
        size_t padlen

    ctypedef union nghttp2_frame:
        nghttp2_frame_hd hd
        nghttp2_headers headers
        nghttp2_data data

    ctypedef union nghttp2_data_source:
        int fd
        void* ptr

    ctypedef ssize_t (*nghttp2_data_source_read_callback)(
        nghttp2_session* session,
        int32_t stream_id,
        uint8_t* buf,
        size_t length,
        uint32_t* data_flags,
        nghttp2_data_source* source,
        void* user_data,
    )

    ctypedef struct nghttp2_data_provider:
        nghttp2_data_source source
        nghttp2_data_source_read_callback read_callback

    ctypedef int (*nghttp2_on_begin_headers_callback)(
        nghttp2_session* session,
        const nghttp2_frame* frame,
        void* user_data,
    )
    ctypedef int (*nghttp2_on_header_callback)(
        nghttp2_session* session,
        const nghttp2_frame* frame,
        const uint8_t* name,
        size_t namelen,
        const uint8_t* value,
        size_t valuelen,
        uint8_t flags,
        void* user_data,
    )
    ctypedef int (*nghttp2_on_frame_recv_callback)(
        nghttp2_session* session,
        const nghttp2_frame* frame,
        void* user_data,
    )
    ctypedef int (*nghttp2_on_frame_send_callback)(
        nghttp2_session* session,
        const nghttp2_frame* frame,
        void* user_data,
    )
    ctypedef int (*nghttp2_on_data_chunk_recv_callback)(
        nghttp2_session* session,
        uint8_t flags,
        int32_t stream_id,
        const uint8_t* data,
        size_t len,
        void* user_data,
    )
    ctypedef int (*nghttp2_on_stream_close_callback)(
        nghttp2_session* session,
        int32_t stream_id,
        uint32_t error_code,
        void* user_data,
    )
    ctypedef int (*nghttp2_send_data_callback)(
        nghttp2_session* session,
        nghttp2_frame* frame,
        const uint8_t* framehd,
        size_t length,
        nghttp2_data_source* source,
        void* user_data,
    )

    int nghttp2_session_callbacks_new(nghttp2_session_callbacks** callbacks_ptr)
    void nghttp2_session_callbacks_del(nghttp2_session_callbacks* callbacks)
    void nghttp2_session_callbacks_set_on_begin_headers_callback(
        nghttp2_session_callbacks* cbs,
        nghttp2_on_begin_headers_callback on_begin_headers_callback,
    )
    void nghttp2_session_callbacks_set_on_header_callback(
        nghttp2_session_callbacks* cbs,
        nghttp2_on_header_callback on_header_callback,
    )
    void nghttp2_session_callbacks_set_on_frame_recv_callback(
        nghttp2_session_callbacks* cbs,
        nghttp2_on_frame_recv_callback on_frame_recv_callback,
    )
    void nghttp2_session_callbacks_set_on_frame_send_callback(
        nghttp2_session_callbacks* cbs,
        nghttp2_on_frame_send_callback on_frame_send_callback,
    )
    void nghttp2_session_callbacks_set_on_data_chunk_recv_callback(
        nghttp2_session_callbacks* cbs,
        nghttp2_on_data_chunk_recv_callback on_data_chunk_recv_callback,
    )
    void nghttp2_session_callbacks_set_on_stream_close_callback(
        nghttp2_session_callbacks* cbs,
        nghttp2_on_stream_close_callback on_stream_close_callback,
    )
    void nghttp2_session_callbacks_set_send_data_callback(
        nghttp2_session_callbacks* cbs,
        nghttp2_send_data_callback send_data_callback,
    )

    int nghttp2_option_new(nghttp2_option** option_ptr)
    void nghttp2_option_del(nghttp2_option* option)
    void nghttp2_option_set_no_auto_window_update(nghttp2_option* option, int val)
    void nghttp2_option_set_no_closed_streams(nghttp2_option* option, int val)
    void nghttp2_option_set_max_continuations(nghttp2_option* option, size_t val)
    void nghttp2_option_set_stream_reset_rate_limit(
        nghttp2_option* option,
        uint64_t burst,
        uint64_t rate,
    )

    int nghttp2_session_server_new2(
        nghttp2_session** session_ptr,
        const nghttp2_session_callbacks* callbacks,
        void* user_data,
        const nghttp2_option* option,
    )
    int nghttp2_session_set_local_window_size(
        nghttp2_session* session,
        uint8_t flags,
        int32_t stream_id,
        int32_t window_size,
    )
    int32_t nghttp2_session_get_effective_recv_data_length(
        nghttp2_session* session,
    )
    int32_t nghttp2_session_get_local_window_size(nghttp2_session* session)
    int32_t nghttp2_session_get_stream_effective_recv_data_length(
        nghttp2_session* session,
        int32_t stream_id,
    )
    int32_t nghttp2_session_get_stream_local_window_size(
        nghttp2_session* session,
        int32_t stream_id,
    )
    void nghttp2_session_del(nghttp2_session* session)
    ssize_t nghttp2_session_mem_recv(
        nghttp2_session* session,
        const uint8_t* data,
        size_t datalen,
    )
    ssize_t nghttp2_session_mem_send(nghttp2_session* session, const uint8_t** data_ptr)
    int nghttp2_session_resume_data(nghttp2_session* session, int32_t stream_id)
    void* nghttp2_session_get_stream_user_data(
        nghttp2_session* session,
        int32_t stream_id,
    )
    int nghttp2_session_set_stream_user_data(
        nghttp2_session* session,
        int32_t stream_id,
        void* stream_user_data,
    )
    int nghttp2_submit_response(
        nghttp2_session* session,
        int32_t stream_id,
        const nghttp2_nv* nva,
        size_t nvlen,
        const nghttp2_data_provider* data_prd,
    )
    int nghttp2_submit_headers(
        nghttp2_session* session,
        uint8_t flags,
        int32_t stream_id,
        const void* pri_spec,
        const nghttp2_nv* nva,
        size_t nvlen,
        void* stream_user_data,
    )
    int nghttp2_submit_rst_stream(
        nghttp2_session* session,
        uint8_t flags,
        int32_t stream_id,
        uint32_t error_code,
    )
    int nghttp2_session_want_read(nghttp2_session* session)
    int nghttp2_session_want_write(nghttp2_session* session)
    int nghttp2_session_get_stream_remote_close(
        nghttp2_session* session, int32_t stream_id
    )
    int32_t nghttp2_session_get_last_proc_stream_id(nghttp2_session* session)
    int nghttp2_submit_shutdown_notice(nghttp2_session* session)
    int nghttp2_submit_ping(
        nghttp2_session* session, uint8_t flags, const uint8_t* opaque_data
    )
    int nghttp2_submit_goaway(
        nghttp2_session* session,
        uint8_t flags,
        int32_t last_stream_id,
        uint32_t error_code,
        const uint8_t* opaque_data,
        size_t opaque_data_len,
    )
    int nghttp2_submit_settings(
        nghttp2_session* session,
        uint8_t flags,
        const nghttp2_settings_entry* iv,
        size_t niv,
    )
    int nghttp2_submit_window_update(
        nghttp2_session* session,
        uint8_t flags,
        int32_t stream_id,
        int32_t window_size_increment,
    )
    const char* nghttp2_strerror(int lib_error_code)
