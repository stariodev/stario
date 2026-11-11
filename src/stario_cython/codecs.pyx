# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, freethreading_compatible=True
"""Brotli and zstd, using the same C codecs as the HTTP writer."""

cimport cython
from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_FromStringAndSize, PyBytes_GET_SIZE

from stario.exceptions import StarioError, StarioRuntime

from stario_cython.compression_buf cimport (
    StarioBrotli,
    StarioBrotliDecoder,
    StarioZstd,
    StarioZstdDecoder,
    stario_brotli_acquire,
    stario_brotli_block_borrowed,
    stario_brotli_compress_borrowed,
    stario_brotli_decoder_finished,
    stario_brotli_decoder_free,
    stario_brotli_decoder_new,
    stario_brotli_decoder_push_borrowed,
    stario_brotli_decoder_version,
    stario_brotli_decompress_borrowed,
    stario_brotli_finish_borrowed,
    stario_brotli_release,
    stario_zstd_acquire,
    stario_zstd_block_borrowed,
    stario_zstd_decoder_finished,
    stario_zstd_decoder_free,
    stario_zstd_decoder_new,
    stario_zstd_decoder_push_borrowed,
    stario_zstd_decompress_borrowed,
    stario_zstd_finish_borrowed,
    stario_zstd_release,
)


cdef object _owned_bytes(const unsigned char* data, size_t n):
    # ``*_borrowed`` out-pointers live in the codec's scratch buffer, valid
    # only until its next call — copy into owned bytes before returning.
    if n == 0:
        return b""
    return PyBytes_FromStringAndSize(<const char*>data, <Py_ssize_t>n)


cdef inline const unsigned char* _bytes_ptr(bytes data, size_t* n) noexcept:
    n[0] = <size_t>PyBytes_GET_SIZE(data)
    if n[0] == 0:
        return NULL
    return <const unsigned char*>PyBytes_AS_STRING(data)


def brotli_compress(bytes data, int level=4, int window=0) -> bytes:
    """Whole buffer in one call (no stream encoder)."""
    cdef const unsigned char* out = NULL
    cdef size_t out_len = 0
    cdef const unsigned char* ptr
    cdef size_t n
    ptr = _bytes_ptr(data, &n)
    if stario_brotli_compress_borrowed(level, window, ptr, n, &out, &out_len) != 0:
        raise StarioError("brotli compression failed")
    return _owned_bytes(out, out_len)


def brotli_decoder_version() -> tuple:
    """Linked ``libbrotlidec`` as ``(major, minor, patch)``.

    Packed with ``BROTLI_MAKE_HEX_VERSION`` (minor and patch are 12 bits).
    """
    cdef unsigned int packed = stario_brotli_decoder_version()
    return (
        int(packed >> 24),
        int((packed >> 12) & 0xFFF),
        int(packed & 0xFFF),
    )


def brotli_decompress(bytes data) -> bytes:
    cdef const unsigned char* out = NULL
    cdef size_t out_len = 0
    cdef const unsigned char* ptr
    cdef size_t n
    ptr = _bytes_ptr(data, &n)
    if stario_brotli_decompress_borrowed(ptr, n, &out, &out_len) != 0:
        raise StarioError("brotli decompression failed")
    return _owned_bytes(out, out_len)


def zstd_compress(bytes data, int level=3, int window=0) -> bytes:
    cdef StarioZstd* enc = stario_zstd_acquire(level, window, 0, 0)
    cdef const unsigned char* out = NULL
    cdef size_t out_len = 0
    cdef const unsigned char* ptr
    cdef size_t n
    if enc == NULL:
        raise StarioError("zstd stream init failed")
    try:
        ptr = _bytes_ptr(data, &n)
        if stario_zstd_finish_borrowed(enc, ptr, n, &out, &out_len) != 0:
            raise StarioError("zstd compression failed")
        return _owned_bytes(out, out_len)
    finally:
        stario_zstd_release(enc)


def zstd_decompress(bytes data) -> bytes:
    cdef const unsigned char* out = NULL
    cdef size_t out_len = 0
    cdef const unsigned char* ptr
    cdef size_t n
    ptr = _bytes_ptr(data, &n)
    if stario_zstd_decompress_borrowed(ptr, n, &out, &out_len) != 0:
        raise StarioError("zstd decompression failed")
    return _owned_bytes(out, out_len)


# The stream classes hold one C codec state each. On a free-threaded build two
# threads calling one object at once would race on that state (and a double
# finish() would put one encoder in the pool twice), so every method runs
# in a critical section on the object; with the GIL it costs nothing.


cdef class BrotliEncoder:
    cdef StarioBrotli* _enc

    def __cinit__(self, int level, int window=0):
        self._enc = stario_brotli_acquire(level, window, 0)
        if self._enc == NULL:
            raise StarioError("brotli stream init failed")

    def __dealloc__(self):
        if self._enc != NULL:
            stario_brotli_release(self._enc)
            self._enc = NULL

    def block(self, bytes data) -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        with cython.critical_section(self):
            if self._enc == NULL:
                raise StarioRuntime("brotli encoder is finished")
            ptr = _bytes_ptr(data, &n)
            if stario_brotli_block_borrowed(self._enc, ptr, n, &out, &out_len) != 0:
                raise StarioError("brotli stream failed")
            return _owned_bytes(out, out_len)

    def finish(self, bytes data=b"") -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        with cython.critical_section(self):
            if self._enc == NULL:
                return b""
            try:
                ptr = _bytes_ptr(data, &n)
                if stario_brotli_finish_borrowed(self._enc, ptr, n, &out, &out_len) != 0:
                    raise StarioError("brotli finish failed")
                return _owned_bytes(out, out_len)
            finally:
                stario_brotli_release(self._enc)
                self._enc = NULL


cdef class ZstdEncoder:
    cdef StarioZstd* _enc

    def __cinit__(self, int level, int window=0):
        self._enc = stario_zstd_acquire(level, window, 0, 0)
        if self._enc == NULL:
            raise StarioError("zstd stream init failed")

    def __dealloc__(self):
        if self._enc != NULL:
            stario_zstd_release(self._enc)
            self._enc = NULL

    def block(self, bytes data) -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        with cython.critical_section(self):
            if self._enc == NULL:
                raise StarioRuntime("zstd encoder is finished")
            ptr = _bytes_ptr(data, &n)
            if stario_zstd_block_borrowed(self._enc, ptr, n, &out, &out_len) != 0:
                raise StarioError("zstd stream failed")
            return _owned_bytes(out, out_len)

    def finish(self, bytes data=b"") -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        with cython.critical_section(self):
            if self._enc == NULL:
                return b""
            try:
                ptr = _bytes_ptr(data, &n)
                if stario_zstd_finish_borrowed(self._enc, ptr, n, &out, &out_len) != 0:
                    raise StarioError("zstd finish failed")
                return _owned_bytes(out, out_len)
            finally:
                stario_zstd_release(self._enc)
                self._enc = NULL


cdef class BrotliDecoder:
    cdef StarioBrotliDecoder* _dec

    def __cinit__(self):
        self._dec = stario_brotli_decoder_new()
        if self._dec == NULL:
            raise StarioError("brotli decoder init failed")

    def __dealloc__(self):
        if self._dec != NULL:
            stario_brotli_decoder_free(self._dec)
            self._dec = NULL

    def decompress(self, bytes data) -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        with cython.critical_section(self):
            ptr = _bytes_ptr(data, &n)
            if stario_brotli_decoder_push_borrowed(self._dec, ptr, n, &out, &out_len) != 0:
                raise StarioError("brotli decompression failed")
            return _owned_bytes(out, out_len)

    def is_finished(self) -> bool:
        with cython.critical_section(self):
            return stario_brotli_decoder_finished(self._dec) != 0


cdef class ZstdDecoder:
    cdef StarioZstdDecoder* _dec

    def __cinit__(self):
        self._dec = stario_zstd_decoder_new()
        if self._dec == NULL:
            raise StarioError("zstd decoder init failed")

    def __dealloc__(self):
        if self._dec != NULL:
            stario_zstd_decoder_free(self._dec)
            self._dec = NULL

    def decompress(self, bytes data) -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        with cython.critical_section(self):
            ptr = _bytes_ptr(data, &n)
            if stario_zstd_decoder_push_borrowed(self._dec, ptr, n, &out, &out_len) != 0:
                raise StarioError("zstd decompression failed")
            return _owned_bytes(out, out_len)

    def is_finished(self) -> bool:
        with cython.critical_section(self):
            return stario_zstd_decoder_finished(self._dec) != 0
