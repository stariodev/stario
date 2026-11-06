"""Cython xxHash64 (seed 0). Vectors come from the reference ``xxhash`` package."""

import pytest

from stario_cython.xxh64 import xxh64

VECTORS = [
    (b"", "ef46db3751d8e999"),
    (b"a", "d24ec4f1a98c6e5b"),
    (b"abc", "44bc2cf5ad770999"),
    (b"x" * 7, "57abb58a45ee501a"),
    (b"x" * 8, "f3844dcda4eeabbf"),
    (b"x" * 15, "093f6c0ea54b9e89"),
    (b"x" * 16, "0572ac5bed1c5a11"),
    (b"x" * 31, "60dd0d01083b99f0"),
    (b"x" * 32, "e2df261fc2ec30eb"),
    (b"x" * 33, "b3fa465f554208a6"),
    (b"x" * 64, "a97161d5402f516e"),
    (bytes(range(256)), "1facbe8406cd904b"),
    (b"stario asset fingerprint test vector" * 40, "1fdc2f140678d4c7"),
]


@pytest.mark.parametrize(("data", "want"), VECTORS)
def test_xxh64_vectors(data: bytes, want: str) -> None:
    assert xxh64(data).hexdigest() == want
    assert xxh64(data).intdigest() == int(want, 16)
    assert xxh64(data).digest() == int(want, 16).to_bytes(8, "big")


def test_xxh64_update_splits() -> None:
    data = bytes(range(256)) * 9
    want = xxh64(data).hexdigest()
    for step in (1, 3, 7, 31, 32, 33, 63, 1024):
        hasher = xxh64()
        for i in range(0, len(data), step):
            assert hasher.update(data[i : i + step]) is hasher
        assert hasher.hexdigest() == want


def test_xxh64_accepts_buffer_types() -> None:
    want = xxh64(b"abcd").hexdigest()
    assert xxh64(bytearray(b"abcd")).hexdigest() == want
    assert xxh64(memoryview(b"abcd")).hexdigest() == want


def test_xxh64_update_needs_contiguous_bytes() -> None:
    """A strided view is refused rather than hashed as if it were contiguous."""
    data = bytes(range(64))
    assert xxh64(memoryview(data)).hexdigest() == xxh64(data).hexdigest()
    assert xxh64(bytearray(data)).hexdigest() == xxh64(data).hexdigest()
    with pytest.raises((ValueError, BufferError)):
        xxh64(memoryview(data)[::2])
    with pytest.raises((ValueError, BufferError)):
        xxh64(memoryview(data)[::-1])
