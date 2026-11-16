"""Smoke tests for public import surface."""

import importlib

import pytest


def test_from_stario_import_app_and_serve() -> None:
    """Fresh interpreter: package root must bind App and serve without a cycle."""
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "from stario import App, serve\n"
            "import stario\n"
            "assert 'serve' in vars(stario)\n"
            "assert stario.serve is serve\n"
            "assert stario.App is App\n",
        ],
        check=True,
    )


def test_core_modules_import() -> None:
    import stario
    import stario.datastar
    import stario.filesystem
    import stario.http
    import stario.json
    import stario.markup

    assert isinstance(stario.__version__, str)
    assert stario.__version__
    assert hasattr(stario, "App")
    assert hasattr(stario.http, "Router")
    assert hasattr(stario.datastar, "data")
    assert hasattr(stario.markup, "render")


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        (
            "stario",
            [
                "App",
                "Assets",
                "Context",
                "Files",
                "Match",
                "Route",
                "Writer",
                "serve",
            ],
        ),
        (
            "stario.filesystem",
            ["Assets", "Files"],
        ),
        (
            "stario.http",
            [
                "App",
                "Match",
                "Route",
                "Router",
                "Request",
                "Writer",
                "normalized_location",
                "default_not_found",
            ],
        ),
        (
            "stario.json",
            [
                "JsonCodec",
                "JsonDecode",
                "JsonEncode",
                "StdlibJsonCodec",
                "dumps",
                "dumps_bytes",
                "loads",
                "set_codec",
            ],
        ),
    ],
)
def test_public_exports(module_name: str, names: list[str]) -> None:
    module = importlib.import_module(module_name)
    for name in names:
        assert hasattr(module, name), f"{module_name} missing {name!r}"


def test_filesystem_all_is_exact() -> None:
    import stario.filesystem

    assert stario.filesystem.__all__ == ["Assets", "Files"]
    assert stario.Assets is stario.filesystem.Assets
    assert stario.Files is stario.filesystem.Files


@pytest.mark.parametrize(
    ("module_name", "name"),
    [
        ("stario", "AssetManifest"),
        ("stario", "StaticAssets"),
        ("stario", "UrlPath"),
        ("stario.http", "UrlPath"),
        ("stario.http.route", "UrlPath"),
        ("stario.http.route", "as_target"),
    ],
)
def test_removed_names(module_name: str, name: str) -> None:
    module = importlib.import_module(module_name)
    assert not hasattr(module, name)


def test_removed_verb_helpers() -> None:
    from stario.datastar import at
    from stario.http import Route, Router

    for verb in ("get", "query", "post", "put", "delete", "patch", "head", "options"):
        assert not hasattr(Route, verb)
        assert not hasattr(Router, verb)
    assert not hasattr(Router, "handle")
    assert not hasattr(at, "fetch")


@pytest.mark.parametrize(
    "removed_module",
    [
        "stario.urls",
        "stario.files",
        "stario.filesystem.assets",
        "stario.filesystem.files",
        "stario.filesystem.live",
        "stario.http.router",
        "stario.http.staticassets",
        "stario.routing",
        "stario.staticassets",
        "stario.routing.trie",
        "stario.routing.pattern",
        "stario.http.protocol",
        "stario.html",
    ],
)
def test_removed_shim_modules(removed_module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(removed_module)


def test_root_reexports_handler_names() -> None:
    """The names a handler file imports most come from `stario` too."""
    import stario
    import stario.datastar
    import stario.markup

    for name in ("at", "data", "SSE", "read_signals"):
        assert getattr(stario, name) is getattr(stario.datastar, name)
    for name in ("HtmlElement", "SafeString", "baked", "render", "classes"):
        assert getattr(stario, name) is getattr(stario.markup, name)
    assert set(stario.__all__) >= {"at", "data", "SSE", "read_signals", "HtmlElement"}


def test_server_config_from_stario_http() -> None:
    import stario
    import stario.http

    assert stario.http.ServerConfig is stario.ServerConfig


def test_root_import_in_a_fresh_interpreter() -> None:
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "from stario import SSE, at, data, h, read_signals\n"
            "from stario.http import ServerConfig\n"
            "assert 'data-on:click' in data.on('click', at.post('/x')).rendered\n",
        ],
        check=True,
    )
