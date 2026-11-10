"""Tests for HTTP server configuration validation."""

import pytest

from stario.exceptions import StarioError
from stario.http.config import RequestPolicy, ServerConfig


def test_request_policy_rejects_low_pipeline_cap() -> None:
    with pytest.raises(StarioError, match="max_pipelined_requests"):
        RequestPolicy(max_pipelined_requests=0)


def test_request_policy_rejects_oversized_header_budget() -> None:
    with pytest.raises(StarioError, match="2\\^30"):
        RequestPolicy(max_header_bytes=1_073_741_825)
    RequestPolicy(max_header_bytes=1_073_741_824)


def test_server_config_rejects_blank_unix_socket() -> None:
    with pytest.raises(StarioError, match="unix_socket must be a non-empty path"):
        ServerConfig(unix_socket="")


def test_server_config_rejects_blank_tcp_host() -> None:
    with pytest.raises(StarioError, match="host must be non-empty"):
        ServerConfig(host="   ")


def test_server_config_rejects_zero_threads() -> None:
    with pytest.raises(StarioError, match="threads must be at least 1"):
        ServerConfig(threads=0)


def test_server_config_rejects_too_many_threads() -> None:
    with pytest.raises(StarioError, match="threads must be at most 256"):
        ServerConfig(threads=257)


def test_server_config_ssl_and_certfile_are_exclusive() -> None:
    import ssl

    with pytest.raises(StarioError, match="either ssl= or ssl_certfile"):
        ServerConfig(ssl=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), ssl_certfile="x.pem")


def test_server_config_ssl_keyfile_requires_certfile() -> None:
    with pytest.raises(StarioError, match="ssl_keyfile requires ssl_certfile"):
        ServerConfig(ssl_keyfile="k.pem")


def test_server_config_missing_certfile_raises() -> None:
    with pytest.raises(StarioError, match="TLS certificate not found"):
        ServerConfig(ssl_certfile="/no/such/cert.pem")


def test_server_config_rejects_zero_max_connections() -> None:
    with pytest.raises(StarioError, match="max_connections must be at least 1"):
        ServerConfig(max_connections=0)


def test_server_config_accepts_max_connections() -> None:
    assert ServerConfig(max_connections=100).max_connections == 100


def test_server_config_reads_max_connections_from_env(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_MAX_CONNECTIONS", "42")
    assert ServerConfig.from_env().max_connections == 42


def test_server_config_rejects_bad_max_connections_env(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_MAX_CONNECTIONS", "-3")
    with pytest.raises(StarioError, match="max_connections must be at least 1"):
        ServerConfig.from_env()
