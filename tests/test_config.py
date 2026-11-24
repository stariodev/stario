"""Tests for HTTP server configuration validation."""

import warnings

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


def test_port_comes_from_stario_port(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_PORT", "8123")
    assert ServerConfig.from_env().port == 8123


def test_plain_port_is_not_read(monkeypatch) -> None:
    # Stario reads STARIO_* only: a platform's PORT goes in as STARIO_PORT=$PORT.
    monkeypatch.delenv("STARIO_PORT", raising=False)
    monkeypatch.setenv("PORT", "9555")
    with pytest.warns(RuntimeWarning, match=r"PORT=9555 is set.*STARIO_PORT=\$PORT"):
        assert ServerConfig.from_env().port == 8000


@pytest.mark.parametrize(
    ("port", "stario_port"), [("8000", None), ("", None), ("9555", "9555")]
)
def test_port_that_changes_nothing_does_not_warn(
    monkeypatch, port: str, stario_port: str | None
) -> None:
    monkeypatch.setenv("PORT", port)
    if stario_port is None:
        monkeypatch.delenv("STARIO_PORT", raising=False)
    else:
        monkeypatch.setenv("STARIO_PORT", stario_port)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ServerConfig.from_env()


def test_bad_port_names_the_variable(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_PORT", "web")
    with pytest.raises(StarioError, match="STARIO_PORT must be an integer"):
        ServerConfig.from_env()


def test_host_comes_from_stario_host(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_HOST", "10.0.0.5")
    monkeypatch.setenv("HOST", "0.0.0.0")
    assert ServerConfig.from_env().host == "10.0.0.5"


def test_plain_host_is_not_read(monkeypatch) -> None:
    # tcsh and SuSE export HOST as the machine name; conda as a target triple.
    monkeypatch.delenv("STARIO_HOST", raising=False)
    monkeypatch.setenv("HOST", "x86_64-apple-darwin13.4.0")
    assert ServerConfig.from_env().host == "127.0.0.1"


def test_blank_stario_host_is_loopback(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_HOST", " ")
    monkeypatch.setenv("HOST", "0.0.0.0")
    assert ServerConfig.from_env().host == "127.0.0.1"


def test_host_defaults_to_loopback(monkeypatch) -> None:
    monkeypatch.delenv("STARIO_HOST", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    assert ServerConfig.from_env().host == "127.0.0.1"


def test_hostname_is_not_read(monkeypatch) -> None:
    # Docker sets HOSTNAME to the container ID; it is not a bind address.
    monkeypatch.delenv("STARIO_HOST", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.setenv("HOSTNAME", "3f2a9c1b7d4e")
    assert ServerConfig.from_env().host == "127.0.0.1"
