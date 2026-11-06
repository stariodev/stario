# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, freethreading_compatible=True
"""xxHash64 (seed 0) for asset fingerprints. Matches the ``xxhash`` package API."""

from libc.stdint cimport uint32_t, uint64_t
from libc.string cimport memcpy

cdef extern from *:
    uint64_t P1 "0x9E3779B185EBCA87"
    uint64_t P2 "0xC2B2AE3D27D4EB4F"
    uint64_t P3 "0x165667B19E3779F9"
    uint64_t P4 "0x85EBCA77C2B2AE63"
    uint64_t P5 "0x27D4EB2F165667C5"


cdef inline uint64_t rotl(uint64_t x, int r) noexcept nogil:
    return (x << r) | (x >> (64 - r))


cdef inline uint64_t rd64(const unsigned char *p) noexcept nogil:
    cdef uint64_t v
    memcpy(&v, p, 8)
    return v


cdef inline uint64_t rd32(const unsigned char *p) noexcept nogil:
    cdef uint32_t v
    memcpy(&v, p, 4)
    return v


cdef class xxh64:
    cdef uint64_t v1, v2, v3, v4, total
    cdef unsigned char tail[32]
    cdef Py_ssize_t tail_len

    def __cinit__(self):
        self.v1, self.v2, self.v3, self.v4 = P1 + P2, P2, 0, -P1

    def __init__(self, data=None):
        if data is not None:
            self.update(data)

    def update(self, const unsigned char[::1] data):
        cdef Py_ssize_t n = data.shape[0], i = 0
        cdef const unsigned char *p
        if n == 0:
            return self
        p = &data[0]
        self.total += n
        with nogil:
            if self.tail_len + n < 32:
                memcpy(self.tail + self.tail_len, p, n)
                self.tail_len += n
            else:
                if self.tail_len:
                    i = 32 - self.tail_len
                    memcpy(self.tail + self.tail_len, p, i)
                    self._stripe(self.tail)
                    self.tail_len = 0
                while i + 32 <= n:
                    self._stripe(p + i)
                    i += 32
                if i < n:
                    memcpy(self.tail, p + i, n - i)
                    self.tail_len = n - i
        return self

    cdef inline void _stripe(self, const unsigned char *p) noexcept nogil:
        self.v1 = rotl(self.v1 + rd64(p) * P2, 31) * P1
        self.v2 = rotl(self.v2 + rd64(p + 8) * P2, 31) * P1
        self.v3 = rotl(self.v3 + rd64(p + 16) * P2, 31) * P1
        self.v4 = rotl(self.v4 + rd64(p + 24) * P2, 31) * P1

    cdef uint64_t _digest(self) noexcept:
        cdef uint64_t h
        cdef Py_ssize_t n = self.tail_len, i = 0
        if self.total >= 32:
            h = rotl(self.v1, 1) + rotl(self.v2, 7) + rotl(self.v3, 12) + rotl(self.v4, 18)
            h ^= rotl(self.v1 * P2, 31) * P1; h = h * P1 + P4
            h ^= rotl(self.v2 * P2, 31) * P1; h = h * P1 + P4
            h ^= rotl(self.v3 * P2, 31) * P1; h = h * P1 + P4
            h ^= rotl(self.v4 * P2, 31) * P1; h = h * P1 + P4
        else:
            h = P5
        h += self.total
        while i + 8 <= n:
            h ^= rotl(rd64(self.tail + i) * P2, 31) * P1
            h = rotl(h, 27) * P1 + P4
            i += 8
        if i + 4 <= n:
            h ^= rd32(self.tail + i) * P1
            h = rotl(h, 23) * P2 + P3
            i += 4
        while i < n:
            h ^= <uint64_t>self.tail[i] * P5
            h = rotl(h, 11) * P1
            i += 1
        h ^= h >> 33; h *= P2; h ^= h >> 29; h *= P3; h ^= h >> 32
        return h

    def digest(self):
        return self._digest().to_bytes(8, "big")

    def intdigest(self):
        return self._digest()

    def hexdigest(self):
        return f"{self._digest():016x}"
