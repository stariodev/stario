"""Tests for live `Files` and fingerprinted `Assets` URLs."""

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from stario import App, Assets, Files
from stario.exceptions import StarioError
from stario.http.compression import CompressionConfig
from stario.testing import TestClient


def _app(files: Assets | Files, **kwargs: Any) -> App:
    app = App()
    files.register(app, **kwargs)
    return app


async def _loaded(files: Assets | Files, **kwargs: Any) -> App:
    app = _app(files)
    await files.load(**kwargs)
    return app


class TestFiles:
    def test_does_not_require_root_at_construction(self, tmp_path: Path) -> None:
        root = tmp_path / "uploads"
        files = Files(root, "/media")
        root.mkdir()
        (root / "new file.txt").write_text("new")

        assert files.href("new file.txt") == "/media/new%20file.txt"

    def test_href_tracks_live_state(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        files = Files(tmp_path)

        with pytest.raises(StarioError, match="not found"):
            files.href("file.txt")
        target.write_text("ready")
        assert files.href("file.txt") == "/data/file.txt"
        target.unlink()
        with pytest.raises(StarioError, match="not found"):
            files.href("file.txt")

    def test_href_appends_query_and_fragment(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("ready")
        files = Files(tmp_path, "/media")

        assert (
            files.href("file.txt", query={"download": 1}, fragment="top")
            == "/media/file.txt?download=1#top"
        )

    @pytest.mark.parametrize(
        "path",
        ["", "/file.txt", "a\\file.txt", "a//file.txt", "./file.txt", "../file.txt"],
    )
    def test_rejects_invalid_api_paths(self, tmp_path: Path, path: str) -> None:
        files = Files(tmp_path)
        with pytest.raises(StarioError):
            files.href(path)

    def test_rejects_directory_hidden_and_symlink(self, tmp_path: Path) -> None:
        (tmp_path / "folder").mkdir()
        (tmp_path / ".secret").write_text("secret")
        (tmp_path / "target.txt").write_text("target")
        (tmp_path / "link.txt").symlink_to(tmp_path / "target.txt")
        files = Files(tmp_path)

        for path in ("folder", ".secret", "link.txt"):
            with pytest.raises(StarioError, match="not found"):
                files.href(path)

    def test_symlinks_must_remain_contained(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / "target.txt").write_text("target")
        (root / "inside.txt").symlink_to(root / "target.txt")
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (root / "outside.txt").symlink_to(outside)
        files = Files(root, follow_symlinks=True)

        assert files.href("inside.txt") == "/data/inside.txt"
        with pytest.raises(StarioError, match="not found"):
            files.href("outside.txt")

    def test_host_prefix_can_generate_urls(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("ready")
        files = Files(tmp_path, "//cdn.example.com/media")

        assert files.href("file.txt") == "//cdn.example.com/media/file.txt"

    def test_directory_symlink_cannot_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("leak")
        (root / "via").symlink_to(outside)
        files = Files(root, follow_symlinks=True)

        with pytest.raises(StarioError, match="not found"):
            files.href("via/secret.txt")


@pytest.mark.asyncio
class TestServing:
    async def test_direct_get_head_and_new_upload(self, tmp_path: Path) -> None:
        (tmp_path / "first.txt").write_text("first")
        files = Files(tmp_path)
        (tmp_path / "later.txt").write_text("later")

        async with TestClient(_app(files)) as client:
            get = await client.get("/data/later.txt")
            head = await client.head("/data/later.txt")

        assert get.status_code == 200
        assert get.content == b"later"
        assert get.headers.get("cache-control") == "private, no-cache"
        assert get.headers.get("accept-ranges") == "bytes"
        assert get.headers.get("x-content-type-options") == "nosniff"
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers.get("content-length") == "5"

    async def test_href_with_space_retrieves_the_file(self, tmp_path: Path) -> None:
        (tmp_path / "new file.txt").write_text("new")
        files = Files(tmp_path)

        async with TestClient(_app(files)) as client:
            response = await client.get(files.href("new file.txt"))

        assert files.href("new file.txt") == "/data/new%20file.txt"
        assert response.status_code == 200
        assert response.content == b"new"

    async def test_attach_registers_and_loads(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("ok")
        files = Files(tmp_path)
        app = App()
        stats = await files.attach(app)

        async with TestClient(app) as client:
            response = await client.get("/data/file.txt")

        assert stats["cached_files"] == 1
        assert response.content == b"ok"
        assert response.headers.get("x-content-type-options") == "nosniff"

    async def test_missing_directory_hidden_and_traversal_return_404(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "folder").mkdir()
        (tmp_path / ".secret").write_text("secret")
        files = Files(tmp_path)

        async with TestClient(_app(files)) as client:
            missing = await client.get("/data/missing.txt")
            folder = await client.get("/data/folder")
            hidden = await client.get("/data/.secret")
            traversal = await client.get("/data/%2e%2e/secret")

        assert {missing.status_code, folder.status_code, hidden.status_code} == {404}
        assert traversal.status_code == 404
        assert missing.headers.get("x-content-type-options") == "nosniff"

    async def test_content_type_override(self, tmp_path: Path) -> None:
        (tmp_path / "site.custom").write_text("data")
        (tmp_path / "IMAGE.PNG").write_bytes(b"\x89PNG\r\n\x1a\n")
        files = Files(tmp_path, content_types={".custom": "application/x-custom"})

        async with TestClient(_app(files)) as client:
            custom = await client.get("/data/site.custom")
            png = await client.get("/data/IMAGE.PNG")

        assert custom.headers.get("content-type") == "application/x-custom"
        assert png.headers.get("content-type") == "image/png"

    async def test_load_change_and_deletion_do_not_serve_stale_bytes(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "file.txt"
        target.write_text("old")
        files = Files(tmp_path)
        app = await _loaded(files)
        target.write_text("new-content")

        async with TestClient(app) as client:
            changed = await client.get("/data/file.txt")
            target.unlink()
            deleted = await client.get("/data/file.txt")

        assert changed.content == b"new-content"
        assert deleted.status_code == 404

    async def test_atomic_replacement_after_resolution_uses_opened_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "file.txt"
        replacement = tmp_path / "replacement.txt"
        target.write_bytes(b"old")
        replacement.write_bytes(b"new-content")
        files = Files(tmp_path)
        original = Files.resolve
        replaced = False

        def replace_after_resolve(bound: Files, path: str) -> object:
            nonlocal replaced
            resolved = original(bound, path)
            if not replaced:
                os.replace(replacement, target)
                replaced = True
            return resolved

        monkeypatch.setattr(Files, "resolve", replace_after_resolve)

        async with TestClient(await _loaded(files)) as client:
            response = await client.get("/data/file.txt")

        assert response.status_code == 200
        assert response.content == b"new-content"
        assert response.headers.get("content-length") == str(len(b"new-content"))

    async def test_deletion_before_open_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "file.txt"
        target.write_bytes(b"content")
        files = Files(tmp_path)
        original = Files.resolve

        def delete_after_resolve(bound: Files, path: str) -> object:
            resolved = original(bound, path)
            target.unlink()
            return resolved

        monkeypatch.setattr(Files, "resolve", delete_after_resolve)

        async with TestClient(_app(files)) as client:
            response = await client.get("/data/file.txt")

        assert response.status_code == 404
        assert response.content == b"Not Found"

    async def test_request_file_descriptors_are_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "file.txt"
        target.write_bytes(b"content")
        files = Files(tmp_path)
        original_open = os.open
        descriptors: list[int] = []

        def tracked_open(path: os.PathLike[str] | str, flags: int) -> int:
            fd = original_open(path, flags)
            if Path(path) == target:
                descriptors.append(fd)
            return fd

        monkeypatch.setattr(os, "open", tracked_open)

        async with TestClient(_app(files)) as client:
            get = await client.get("/data/file.txt")
            head = await client.head("/data/file.txt")
            not_modified = await client.get(
                "/data/file.txt", headers={"If-None-Match": "*"}
            )
            unsatisfied = await client.get(
                "/data/file.txt", headers={"Range": "bytes=99-100"}
            )

        assert [get.status_code, head.status_code, not_modified.status_code] == [
            200,
            200,
            304,
        ]
        assert unsatisfied.status_code == 416
        assert len(descriptors) == 4
        for fd in descriptors:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)

    async def test_range_on_loaded_file_is_uncompressed(self, tmp_path: Path) -> None:
        payload = ("body { color: red; }\n" * 100).encode()
        (tmp_path / "app.css").write_bytes(payload)
        files = Files(tmp_path)

        async with TestClient(
            await _loaded(
                files, precompress=("br",), compression=CompressionConfig(min_size=1)
            )
        ) as client:
            partial = await client.get(
                "/data/app.css",
                headers={"Range": "bytes=0-9", "Accept-Encoding": "br"},
            )

        assert partial.status_code == 206
        assert partial.content == payload[:10]
        assert partial.headers.get("content-encoding") is None

    async def test_new_file_after_load_streams_uncompressed(
        self, tmp_path: Path
    ) -> None:
        files = Files(tmp_path)

        async with TestClient(await _loaded(files, precompress=("br",))) as client:
            (tmp_path / "new.txt").write_text("new" * 1000)
            response = await client.get(
                "/data/new.txt", headers={"Accept-Encoding": "br"}
            )

        assert response.status_code == 200
        assert response.headers.get("content-encoding") is None

    async def test_validators_and_304(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("content")
        files = Files(tmp_path)

        async with TestClient(_app(files)) as client:
            initial = await client.get("/data/file.txt")
            etag = initial.headers.get("etag")
            modified = initial.headers.get("last-modified")
            assert etag is not None
            assert modified is not None
            wildcard = await client.get(
                "/data/file.txt", headers={"If-None-Match": "*"}
            )
            listed = await client.get(
                "/data/file.txt",
                headers={"If-None-Match": f'"other", {etag}'},
            )
            weak = await client.get(
                "/data/file.txt",
                headers={"If-None-Match": f"W/{etag}"},
            )
            ims = await client.get(
                "/data/file.txt", headers={"If-Modified-Since": modified}
            )

        assert etag.startswith('"')
        assert modified.endswith("GMT")
        for response in (wildcard, listed, weak, ims):
            assert response.status_code == 304
            assert response.content == b""
            assert response.headers.get("etag") == etag

    async def test_if_none_match_takes_precedence_over_ims(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "file.txt").write_text("content")
        files = Files(tmp_path)

        async with TestClient(_app(files)) as client:
            initial = await client.get("/data/file.txt")
            response = await client.get(
                "/data/file.txt",
                headers={
                    "If-None-Match": '"different"',
                    "If-Modified-Since": initial.headers.get("last-modified") or "",
                },
            )

        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("range_value", "body", "content_range"),
        [
            ("bytes=2-5", b"2345", "bytes 2-5/10"),
            ("bytes=7-", b"789", "bytes 7-9/10"),
            ("bytes=-3", b"789", "bytes 7-9/10"),
        ],
    )
    async def test_ranges(
        self,
        tmp_path: Path,
        range_value: str,
        body: bytes,
        content_range: str,
    ) -> None:
        (tmp_path / "file.bin").write_bytes(b"0123456789")
        files = Files(tmp_path)

        async with TestClient(_app(files)) as client:
            response = await client.get(
                "/data/file.bin", headers={"Range": range_value}
            )
            head = await client.head("/data/file.bin", headers={"Range": range_value})

        assert response.status_code == 206
        assert response.content == body
        assert response.headers.get("content-range") == content_range
        assert head.status_code == 206
        assert head.content == b""
        assert head.headers.get("content-length") == str(len(body))

    async def test_cached_range_and_416(self, tmp_path: Path) -> None:
        (tmp_path / "file.bin").write_bytes(b"0123456789")
        files = Files(tmp_path)

        async with TestClient(await _loaded(files)) as client:
            partial = await client.get("/data/file.bin", headers={"Range": "bytes=1-2"})
            invalid = await client.get(
                "/data/file.bin", headers={"Range": "bytes=99-100"}
            )

        assert partial.status_code == 206
        assert partial.content == b"12"
        assert invalid.status_code == 416
        assert invalid.content == b""
        assert invalid.headers.get("content-range") == "bytes */10"

    async def test_if_range_uses_strong_etag_and_date(self, tmp_path: Path) -> None:
        (tmp_path / "file.bin").write_bytes(b"0123456789")
        files = Files(tmp_path)

        async with TestClient(_app(files)) as client:
            initial = await client.get("/data/file.bin")
            tagged = await client.get(
                "/data/file.bin",
                headers={
                    "Range": "bytes=0-1",
                    "If-Range": initial.headers.get("etag") or "",
                },
            )
            weak = await client.get(
                "/data/file.bin",
                headers={
                    "Range": "bytes=0-1",
                    "If-Range": f"W/{initial.headers.get('etag') or ''}",
                },
            )
            dated = await client.get(
                "/data/file.bin",
                headers={
                    "Range": "bytes=0-1",
                    "If-Range": initial.headers.get("last-modified") or "",
                },
            )

        assert tagged.status_code == 206
        assert tagged.content == b"01"
        assert weak.status_code == 200
        assert weak.content == b"0123456789"
        assert dated.status_code == 206
        assert dated.content == b"01"

    async def test_load_precompression_negotiates(self, tmp_path: Path) -> None:
        payload = ("body { color: red; }\n" * 100).encode()
        (tmp_path / "app.css").write_bytes(payload)
        files = Files(tmp_path)

        async with TestClient(
            await _loaded(
                files,
                precompress=("br",),
                compression=CompressionConfig(min_size=1),
            )
        ) as client:
            response = await client.get(
                "/data/app.css", headers={"Accept-Encoding": "br"}
            )

        assert response.content == payload
        assert response.headers.get("content-encoding") == "br"
        assert "accept-encoding" in (response.headers.get("vary") or "").lower()
        assert files.stats["cached_files"] == 1
        assert files.stats["compressed_files"] == 1

    async def test_precompressed_files_prefer_br_over_zstd(
        self, tmp_path: Path
    ) -> None:
        payload = ("body { color: red; }\n" * 100).encode()
        (tmp_path / "app.css").write_bytes(payload)
        files = Files(tmp_path)

        async with TestClient(
            await _loaded(
                files,
                precompress=("br", "zstd"),
                compression=CompressionConfig(min_size=1),
            )
        ) as client:
            response = await client.get(
                "/data/app.css", headers={"Accept-Encoding": "br, zstd"}
            )

        assert response.headers.get("content-encoding") == "br"

    async def test_svg_is_precompressed(self, tmp_path: Path) -> None:
        payload = b'<svg xmlns="http://www.w3.org/2000/svg">' + b"<g/>" * 80 + b"</svg>"
        (tmp_path / "icon.svg").write_bytes(payload)
        files = Files(tmp_path)

        async with TestClient(
            await _loaded(
                files,
                precompress=("br",),
                compression=CompressionConfig(min_size=1),
            )
        ) as client:
            response = await client.get(
                "/data/icon.svg", headers={"Accept-Encoding": "br"}
            )

        assert response.content == payload
        assert response.headers.get("content-encoding") == "br"
        assert files.stats["compressed_files"] == 1

    async def test_compressed_304_varies_on_accept_encoding(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "app.css").write_text("body { color: red; }\n" * 100)
        files = Files(tmp_path)

        async with TestClient(
            await _loaded(
                files,
                precompress=("br",),
                compression=CompressionConfig(min_size=1),
            )
        ) as client:
            response = await client.get(
                "/data/app.css",
                headers={"Accept-Encoding": "br", "If-None-Match": "*"},
            )

        assert response.status_code == 304
        assert "accept-encoding" in (response.headers.get("vary") or "").lower()

    async def test_etag_is_distinct_per_encoding(self, tmp_path: Path) -> None:
        payload = ("body { color: red; }\n" * 100).encode()
        (tmp_path / "app.css").write_bytes(payload)
        files = Files(tmp_path)

        async with TestClient(
            await _loaded(
                files,
                precompress=("br", "gzip"),
                compression=CompressionConfig(min_size=1),
            )
        ) as client:
            identity = await client.get("/data/app.css")
            br = await client.get("/data/app.css", headers={"Accept-Encoding": "br"})
            gzip_resp = await client.get(
                "/data/app.css", headers={"Accept-Encoding": "gzip"}
            )

        identity_etag = identity.headers.get("etag")
        br_etag = br.headers.get("etag")
        gzip_etag = gzip_resp.headers.get("etag")
        assert identity.headers.get("content-encoding") is None
        assert br.headers.get("content-encoding") == "br"
        assert gzip_resp.headers.get("content-encoding") == "gzip"
        assert len({identity_etag, br_etag, gzip_etag}) == 3
        for etag in (identity_etag, br_etag, gzip_etag):
            assert etag is not None
            assert etag.startswith('"')

    async def test_stale_etag_for_wrong_encoding_is_not_fresh(
        self, tmp_path: Path
    ) -> None:
        payload = ("body { color: red; }\n" * 100).encode()
        (tmp_path / "app.css").write_bytes(payload)
        files = Files(tmp_path)

        async with TestClient(
            await _loaded(
                files, precompress=("br",), compression=CompressionConfig(min_size=1)
            )
        ) as client:
            br = await client.get("/data/app.css", headers={"Accept-Encoding": "br"})
            br_etag = br.headers.get("etag") or ""
            # A validator minted for the br representation must not satisfy a
            # request for the identity representation.
            revalidate_identity = await client.get(
                "/data/app.css", headers={"If-None-Match": br_etag}
            )
            revalidate_br = await client.get(
                "/data/app.css",
                headers={"Accept-Encoding": "br", "If-None-Match": br_etag},
            )

        assert revalidate_identity.status_code == 200
        assert revalidate_br.status_code == 304

    async def test_range_etag_matches_identity_not_negotiated_encoding(
        self, tmp_path: Path
    ) -> None:
        payload = ("body { color: red; }\n" * 100).encode()
        (tmp_path / "app.css").write_bytes(payload)
        files = Files(tmp_path)

        async with TestClient(
            await _loaded(
                files, precompress=("br",), compression=CompressionConfig(min_size=1)
            )
        ) as client:
            identity = await client.get("/data/app.css")
            ranged = await client.get(
                "/data/app.css",
                headers={"Range": "bytes=0-3", "Accept-Encoding": "br"},
            )

        assert ranged.status_code == 206
        assert ranged.headers.get("etag") == identity.headers.get("etag")

    async def test_load_budget_is_deterministic(self, tmp_path: Path) -> None:
        (tmp_path / "c.txt").write_bytes(b"c")
        (tmp_path / "b.txt").write_bytes(b"bbbb")
        (tmp_path / "a.txt").write_bytes(b"aaaaa")
        files = Files(tmp_path)
        files.register(App())
        await files.load(max_bytes=4)

        assert list(files._cache) == ["b.txt"]
        assert files.stats["files"] == 3
        assert files.stats["retained_bytes"] == 4
        assert files.stats["cached_files"] == 1
        assert files.stats["streamed_files"] == 2
        assert files.stats["budget_skipped_files"] == 2
        assert files.stats["compressed_files"] == 0

    async def test_load_budget_counts_compressed_variants(self, tmp_path: Path) -> None:
        payload = b"a" * 2000
        (tmp_path / "file.txt").write_bytes(payload)
        config = CompressionConfig(min_size=1)
        probe = Files(tmp_path)
        probe.register(App())
        await probe.load(precompress=("br",), compression=config)
        footprint = probe.stats["retained_bytes"]
        assert footprint > len(payload)

        limited = Files(tmp_path)
        app = App()
        limited.register(app)
        await limited.load(
            max_bytes=footprint - 1,
            precompress=("br",),
            compression=config,
        )

        assert limited.stats["cached_files"] == 1
        assert limited.stats["streamed_files"] == 0
        assert limited.stats["retained_bytes"] == len(payload)
        assert limited.stats["compressed_files"] == 0
        assert limited.stats["budget_skipped_files"] == 0

        async with TestClient(app) as client:
            response = await client.get(
                "/data/file.txt", headers={"Accept-Encoding": "br"}
            )

        assert response.content == payload
        assert response.headers.get("content-encoding") is None

    async def test_changed_load_streams_without_compression(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "app.css"
        target.write_text("a" * 1000)
        files = Files(tmp_path)
        app = await _loaded(
            files,
            precompress=("br",),
            compression=CompressionConfig(min_size=1),
        )
        target.write_text("changed" * 200)
        os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 1))

        async with TestClient(app) as client:
            response = await client.get(
                "/data/app.css", headers={"Accept-Encoding": "br"}
            )

        assert response.status_code == 200
        assert response.headers.get("content-encoding") is None


@pytest.mark.asyncio
class TestConfiguration:
    async def test_rejects_host_prefix(self, tmp_path: Path) -> None:
        files = Files(tmp_path, "//cdn.example.com/data")
        with pytest.raises(StarioError, match="app-relative"):
            files.register(App())

    async def test_requires_existing_directory(self, tmp_path: Path) -> None:
        files = Files(tmp_path / "missing")
        with pytest.raises(StarioError, match="not found"):
            files.register(App())

    async def test_rejects_invalid_register(self, tmp_path: Path) -> None:
        files = Files(tmp_path)
        with pytest.raises(StarioError):
            files.register(App(), cache_control="bad\nvalue")

    @pytest.mark.parametrize(
        "content_types",
        [
            {".txt": "bad\nvalue"},
            {"txt": "text/plain"},
        ],
    )
    async def test_rejects_invalid_content_types(
        self, tmp_path: Path, content_types: dict[str, str]
    ) -> None:
        with pytest.raises(StarioError):
            Files(tmp_path, content_types=content_types)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_bytes": 0},
            {"max_file_size": 0},
            {"precompress": "br"},
            {"precompress": ("deflate",)},
        ],
    )
    async def test_rejects_invalid_load(
        self, tmp_path: Path, kwargs: dict[str, object]
    ) -> None:
        files = Files(tmp_path)
        files.register(App())
        with pytest.raises(StarioError):
            await files.load(**kwargs)  # type: ignore[arg-type]

    async def test_load_without_register(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("ok")
        files = Files(tmp_path)
        stats = await files.load()
        assert stats["cached_files"] == 1

        async with TestClient(_app(files)) as client:
            response = await client.get("/data/file.txt")

        assert response.content == b"ok"

    async def test_load_once(self, tmp_path: Path) -> None:
        files = Files(tmp_path)
        await files.load()
        with pytest.raises(StarioError, match="already loaded"):
            await files.load()

    async def test_attach_reuses_tree_on_a_new_app(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("ok")
        files = Files(tmp_path)
        first = App()
        await files.attach(first)
        second = App()
        await files.attach(second)
        async with TestClient(second) as client:
            response = await client.get("/data/file.txt")
        assert response.content == b"ok"


class TestAssets:
    def test_requires_directory(self, tmp_path: Path) -> None:
        with pytest.raises(StarioError, match="not found"):
            Assets(tmp_path / "missing")

    def test_href_missing_file_raises(self, tmp_path: Path) -> None:
        files = Assets(tmp_path)
        with pytest.raises(StarioError, match="not found"):
            files.href("missing.js")

    @pytest.mark.asyncio
    async def test_hashes_and_redirects(self, tmp_path: Path) -> None:
        (tmp_path / "app.js").write_text("console.log('hi');")
        files = Assets(tmp_path, "/static")
        href = files.href("app.js")
        assert href.startswith("/static/app.")
        assert href.endswith(".js")

        async with TestClient(_app(files)) as client:
            redirect = await client.get("/static/app.js", follow_redirects=False)
            body = await client.get(href)

        assert redirect.status_code == 307
        assert redirect.headers.get("location") == href
        assert body.status_code == 200
        assert body.content == b"console.log('hi');"
        assert (
            body.headers.get("cache-control") == "public, max-age=31536000, immutable"
        )
        assert body.headers.get("x-content-type-options") == "nosniff"
        digest = href.rsplit("/", 1)[-1].split(".")[1]
        assert body.headers.get("etag") == f'"{digest}"'

    @pytest.mark.asyncio
    async def test_load_hashes_files_not_passed_to_href(self, tmp_path: Path) -> None:
        (tmp_path / "app.js").write_text("console.log('hi');")
        files = Assets(tmp_path, "/static")
        files.register(App())
        assert files._catalog == {}
        await files.load()

        href = files.href("app.js")
        assert href.startswith("/static/app.")
        assert files.stats["cached_files"] == 1

    @pytest.mark.asyncio
    async def test_load_walk_skips_hidden_and_escaped_symlinks(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.css").write_text("a{}")
        (root / "nested").mkdir()
        (root / "nested" / "b.css").write_text("b{}")
        hidden = root / ".skip"
        hidden.mkdir()
        (hidden / "x.css").write_text("x{}")
        (root / "link.css").symlink_to(root / "a.css")
        outside = tmp_path / "out.css"
        outside.write_text("o{}")
        (root / "escape.css").symlink_to(outside)

        files = Assets(root)
        files.register(App())
        await files.load()
        assert set(files._catalog) == {"a.css", "nested/b.css"}

        followed = Assets(root, follow_symlinks=True)
        followed.register(App())
        await followed.load()
        assert set(followed._catalog) == {"a.css", "nested/b.css", "link.css"}

    @pytest.mark.asyncio
    async def test_streamed_file_404_is_not_cached_immutably(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "big.bin"
        target.write_bytes(b"0123456789")
        app = App()
        files = Assets(tmp_path, "/static")
        files.register(app)
        await files.load(max_file_size=1)
        href = files.href("big.bin")
        assert files.stats["streamed_files"] == 1

        target.write_bytes(b"replaced by a longer body")

        async with TestClient(app) as client:
            stale = await client.get(href)

        assert stale.status_code == 404
        assert stale.headers.get("cache-control") != (
            "public, max-age=31536000, immutable"
        )
        assert stale.headers.get("x-content-type-options") == "nosniff"

    @pytest.mark.asyncio
    async def test_streamed_file_descriptors_are_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "big.bin"
        target.write_bytes(b"0123456789")
        files = Assets(tmp_path, "/static")
        app = App()
        files.register(app)
        await files.load(max_file_size=1)
        href = files.href("big.bin")

        original_open = os.open
        descriptors: list[int] = []

        def tracked_open(path: os.PathLike[str] | str, flags: int) -> int:
            fd = original_open(path, flags)
            if Path(path) == target:
                descriptors.append(fd)
            return fd

        monkeypatch.setattr(os, "open", tracked_open)

        async with TestClient(app) as client:
            full = await client.get(href)
            partial = await client.get(href, headers={"Range": "bytes=2-4"})
            unsatisfied = await client.get(href, headers={"Range": "bytes=99-100"})

        assert full.status_code == 200
        assert full.content == b"0123456789"
        assert partial.status_code == 206
        assert partial.content == b"234"
        assert unsatisfied.status_code == 416
        assert len(descriptors) == 3
        for fd in descriptors:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)

    @pytest.mark.asyncio
    async def test_load_max_bytes_applies(self, tmp_path: Path) -> None:
        (tmp_path / "a.css").write_bytes(b"a" * 400)
        (tmp_path / "b.css").write_bytes(b"b" * 400)
        files = Assets(tmp_path, "/static")
        files.register(App())
        await files.load(max_bytes=500, precompress=())

        assert files.stats["cached_files"] == 1
        assert files.stats["streamed_files"] == 1
        assert files.stats["budget_skipped_files"] == 1
        assert files.stats["retained_bytes"] <= 500

    @pytest.mark.asyncio
    async def test_changed_file_after_href_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "app.js"
        target.write_text("one")
        files = Assets(tmp_path)
        files.href("app.js")
        target.write_text("two")
        files.register(App())
        with pytest.raises(StarioError, match="changed"):
            await files.load()

    @pytest.mark.asyncio
    async def test_attach_serves_hashed_url(self, tmp_path: Path) -> None:
        (tmp_path / "app.js").write_text("console.log(1)")
        files = Assets(tmp_path)
        app = App()
        stats = await files.attach(app)
        href = files.href("app.js")

        async with TestClient(app) as client:
            response = await client.get(href)

        assert stats["cached_files"] == 1
        assert response.status_code == 200
        assert response.content == b"console.log(1)"
        assert response.headers.get("x-content-type-options") == "nosniff"

    @pytest.mark.asyncio
    async def test_cached_asset_honors_etag_304(self, tmp_path: Path) -> None:
        (tmp_path / "app.js").write_text("console.log(1)")
        files = Assets(tmp_path)
        href = files.href("app.js")
        await files.load(precompress=())

        async with TestClient(_app(files)) as client:
            initial = await client.get(href)
            etag = initial.headers.get("etag")
            assert etag is not None
            cached = await client.get(href, headers={"If-None-Match": etag})

        assert files.stats["cached_files"] == 1
        assert initial.status_code == 200
        assert cached.status_code == 304
        assert cached.content == b""
        assert cached.headers.get("etag") == etag

    @pytest.mark.asyncio
    async def test_head_on_cached_asset_keeps_get_content_length(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "app.js").write_text("console.log(1)")
        files = Assets(tmp_path)
        href = files.href("app.js")
        await files.load(precompress=())

        async with TestClient(_app(files)) as client:
            get = await client.get(href)
            head = await client.head(href)

        assert get.status_code == 200
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers.get("content-length") == get.headers.get("content-length")

    @pytest.mark.asyncio
    async def test_streamed_etag_and_304(self, tmp_path: Path) -> None:
        (tmp_path / "big.bin").write_bytes(b"0123456789")
        files = Assets(tmp_path)
        href = files.href("big.bin")
        await files.load(max_file_size=1)

        async with TestClient(_app(files)) as client:
            initial = await client.get(href)
            etag = initial.headers.get("etag")
            assert etag is not None
            digest = href.rsplit("/", 1)[-1].rsplit(".", 1)[0].split(".", 1)[1]
            assert etag == f'"{digest}"'
            cached = await client.get(href, headers={"If-None-Match": etag})

        assert initial.status_code == 200
        assert initial.headers.get("x-content-type-options") == "nosniff"
        assert cached.status_code == 304


class TestUntrustedFiles:
    def test_untrusted_must_be_bool(self, tmp_path: Path) -> None:
        with pytest.raises(StarioError, match="untrusted must be bool"):
            Files(tmp_path, untrusted=1)  # type: ignore[arg-type]

    async def test_untrusted_marks_active_types_for_download(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "page.html").write_text("<p>hi</p>")
        (tmp_path / "note.txt").write_text("plain")
        files = Files(tmp_path, untrusted=True)

        async with TestClient(_app(files)) as client:
            page = await client.get("/data/page.html")
            note = await client.get("/data/note.txt")
            missing = await client.get("/data/missing.txt")

        assert page.status_code == 200
        assert page.headers.get("content-security-policy") == "sandbox"
        assert page.headers.get("content-disposition") == "attachment"
        assert note.status_code == 200
        assert note.headers.get("content-security-policy") == "sandbox"
        assert note.headers.get("content-disposition") is None
        # The 404 stays a plain not-found.
        assert missing.status_code == 404
        assert missing.headers.get("content-security-policy") is None
        assert missing.headers.get("content-disposition") is None

    async def test_untrusted_headers_on_304_206_and_416(self, tmp_path: Path) -> None:
        (tmp_path / "page.html").write_text("<p>hi</p>")
        files = Files(tmp_path, untrusted=True)

        async with TestClient(_app(files)) as client:
            stale = await client.get("/data/page.html", headers={"If-None-Match": "*"})
            partial = await client.get(
                "/data/page.html", headers={"Range": "bytes=0-2"}
            )
            unsatisfied = await client.get(
                "/data/page.html", headers={"Range": "bytes=99-100"}
            )

        for response in (stale, partial, unsatisfied):
            assert response.headers.get("content-security-policy") == "sandbox"
            assert response.headers.get("content-disposition") == "attachment"
        assert [stale.status_code, partial.status_code, unsatisfied.status_code] == [
            304,
            206,
            416,
        ]

    async def test_trusted_mount_has_no_sandbox_headers(self, tmp_path: Path) -> None:
        (tmp_path / "page.html").write_text("<p>hi</p>")
        files = Files(tmp_path)

        async with TestClient(_app(files)) as client:
            response = await client.get("/data/page.html")

        assert response.status_code == 200
        assert response.headers.get("content-security-policy") is None
        assert response.headers.get("content-disposition") is None


@pytest.mark.asyncio
class TestOpenOffLoop:
    async def test_cancelled_open_does_not_leak_fd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A handler cancelled while the worker thread opens the file still
        closes the fd once the thread returns."""
        target = tmp_path / "file.txt"
        target.write_bytes(b"content")
        files = Files(tmp_path)
        app = _app(files)
        gate = threading.Event()
        descriptors: list[int] = []
        real = Files._resolve_open

        def gated(self: Files, path: str):
            assert gate.wait(5)
            opened = real(self, path)
            if opened is not None:
                descriptors.append(opened[0])
            return opened

        monkeypatch.setattr(Files, "_resolve_open", gated)

        async with TestClient(app) as client:
            request = asyncio.ensure_future(client.get("/data/file.txt"))
            for _ in range(100):
                if app.tasks:
                    break
                await asyncio.sleep(0.01)
            handler_task = next(iter(app.tasks))
            handler_task.cancel()
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await handler_task
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await asyncio.sleep(0.1)  # let the thread finish + callback run

        assert descriptors, "the gated open never ran"
        for fd in descriptors:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
