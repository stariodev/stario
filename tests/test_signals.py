"""Tests for Datastar signal parsing via `stario.datastar.read_signals`."""

import pytest

from stario.datastar import read_signals
from stario.exceptions import RequestBodyError
from tests.helpers import make_request


class TestReadSignals:
    async def test_returns_dict_for_valid_post_body(self):
        req = make_request(method="POST", body=b'{"name":"test","count":42}')

        result = await read_signals(req)

        assert result == {"name": "test", "count": 42}

    @pytest.mark.parametrize("method", ["GET", "HEAD"])
    async def test_reads_get_query_datastar_payload(self, method: str):
        # A GET route also answers HEAD, so HEAD reads the same signals.
        req = make_request(
            method=method, query={"datastar": '{"name":"test","count":42}'}
        )

        result = await read_signals(req)

        assert result == {"name": "test", "count": 42}

    async def test_delete_ignores_body_uses_query(self):
        req = make_request(
            method="DELETE",
            query={"datastar": '{"from":"query"}'},
            body=b'{"from":"body"}',
        )

        result = await read_signals(req)

        assert result == {"from": "query"}

    async def test_missing_payload_defaults_to_empty_dict(self):
        req = make_request(method="GET")

        result = await read_signals(req)

        assert result == {}

    async def test_invalid_json_raises_body_error_400(self):
        req = make_request(method="POST", body=b"{invalid")

        with pytest.raises(RequestBodyError, match="Invalid Datastar signals") as info:
            await read_signals(req)
        assert info.value.status_code == 400
        assert isinstance(info.value.__cause__, ValueError)

    @pytest.mark.parametrize("payload", [b"[1,2,3]", b"null", b'"text"', b"42"])
    async def test_non_object_json_raises_body_error_400(self, payload: bytes):
        req = make_request(method="POST", body=payload)

        with pytest.raises(RequestBodyError, match="Invalid Datastar signals") as info:
            await read_signals(req)
        assert info.value.status_code == 400

    async def test_max_size_limits_the_body(self):
        req = make_request(method="POST", body=b'{"n":1}')

        with pytest.raises(RequestBodyError) as info:
            await read_signals(req, max_size=3)
        assert info.value.status_code == 413
        assert await read_signals(req, max_size=7) == {"n": 1}

    async def test_deep_nesting_raises_body_error_400(self):
        # Past the decoder's depth cap stdlib json raises RecursionError, not
        # ValueError; it must still map to a 400, not a 500.
        req = make_request(method="POST", body=b"[" * 100_000 + b"]" * 100_000)

        with pytest.raises(RequestBodyError, match="Invalid Datastar signals"):
            await read_signals(req)

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "QUERY"])
    async def test_write_methods_use_body_not_query(self, method: str):
        req = make_request(
            method=method,
            query={"datastar": '{"from":"query"}'},
            body=b'{"from":"body"}',
        )

        result = await read_signals(req)

        assert result == {"from": "body"}
