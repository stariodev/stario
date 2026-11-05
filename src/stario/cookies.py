"""Cookie parsing, `Set-Cookie` emission, and signed cookies.

Read inbound cookies via `req.cookies` (Cython ``ParsedCookies``, on demand).
Write outbound cookies with `set_cookie` / `delete_cookie` on a `Writer`.
`SignedCookie` signs a JSON payload that the client can read but not change.
"""

import base64
import hashlib
import hmac
import http.cookies
import re
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any, Literal

from stario.exceptions import StarioError
from stario.http.request import Request
from stario.http.writer import Writer
from stario.json import dumps_bytes, loads

_SAMESITE = frozenset(("lax", "strict", "none"))


def morsels_from_lines(
    header_values: Iterable[str],
) -> Iterator[tuple[str, http.cookies.Morsel[str]]]:
    """Yield `(name, morsel)` from header field-values; skip malformed lines."""
    for header_value in header_values:
        if not header_value:
            continue
        parsed = http.cookies.SimpleCookie()
        try:
            parsed.load(header_value)
        except http.cookies.CookieError:
            continue
        yield from parsed.items()


def parse_cookie_headers(cookie_strings: Iterable[str]) -> dict[str, str]:
    """Parse `Cookie` header value(s) into a name→value mapping.

      Uses `http.cookies.SimpleCookie` (RFC 6265 cookie-value quoting and
      unquoting). Later header lines win; within one line, later pairs override
    earlier. Malformed lines are skipped (no cookies extracted from that line).
    """
    cookies: dict[str, str] = {}
    for name, morsel in morsels_from_lines(cookie_strings):
        cookies[name] = morsel.value
    return cookies


def set_cookie(
    w: Writer,
    name: str,
    value: str,
    *,
    max_age: int | None = None,
    expires: datetime | str | int | None = None,
    path: str = "/",
    domain: str | None = None,
    secure: bool = False,
    httponly: bool = True,
    samesite: Literal["lax", "strict", "none"] | None = "lax",
) -> Writer:
    """Append `Set-Cookie` (returns `w` for chaining).

    `httponly` defaults to `True` so `document.cookie` cannot read the value;
    pass `httponly=False` only for cookies client-side JavaScript must read.

    `expires` may be:

    * A `datetime` — formatted as an HTTP-date (GMT).
    * A `str` — sent as the raw `Expires` attribute (must already be a valid HTTP-date if you rely on browser parsing).
    * An `int` — treated as a **Unix timestamp in seconds** (UTC); converted to an HTTP-date.

    For relative expiry, prefer `max_age` (seconds from now).
    """
    if path and (";" in path or "," in path):
        raise StarioError(
            "Invalid cookie path",
            context={"path": path},
            help_text="Cookie Path must not contain ';' or ','.",
        )
    if domain and (";" in domain or "," in domain):
        raise StarioError(
            "Invalid cookie domain",
            context={"domain": domain},
            help_text="Cookie Domain must not contain ';' or ','.",
        )
    if samesite is not None and samesite.lower() not in _SAMESITE:
        raise StarioError(
            "Invalid cookie samesite",
            context={"samesite": samesite},
            help_text="SameSite must be 'lax', 'strict', 'none', or None.",
        )
    if isinstance(expires, str) and (";" in expires or "," in expires):
        raise StarioError(
            "Invalid cookie expires",
            context={"expires": expires},
            help_text="Cookie Expires must not contain ';' or ','.",
        )

    cookie: http.cookies.BaseCookie[str] = http.cookies.SimpleCookie()
    try:
        cookie[name] = value
        if max_age is not None:
            cookie[name]["max-age"] = str(max_age)
        if expires is not None:
            if isinstance(expires, datetime):
                cookie[name]["expires"] = format_datetime(expires, usegmt=True)
            elif isinstance(expires, int):
                cookie[name]["expires"] = format_datetime(
                    datetime.fromtimestamp(expires, tz=UTC),
                    usegmt=True,
                )
            else:
                cookie[name]["expires"] = expires
        if path:
            cookie[name]["path"] = path
        if domain:
            cookie[name]["domain"] = domain
        if httponly:
            cookie[name]["httponly"] = True
        if samesite:
            cookie[name]["samesite"] = samesite
        if secure or (samesite is not None and samesite.lower() == "none"):
            cookie[name]["secure"] = True
        w.headers.add("set-cookie", cookie.output(header="").strip())
    except (http.cookies.CookieError, ValueError) as exc:
        raise StarioError(
            "Invalid cookie name, value, or attribute",
            context={"name": name},
            help_text="Cookie names, values, and attributes must follow RFC 6265 rules.",
        ) from exc
    return w


def delete_cookie(
    w: Writer,
    name: str,
    *,
    path: str = "/",
    domain: str | None = None,
    secure: bool = False,
    httponly: bool = True,
    samesite: Literal["lax", "strict", "none"] | None = "lax",
) -> Writer:
    """Expire a cookie by sending a clearing `Set-Cookie` that matches how it was set.

    Browsers match on name, path, domain, and often `Secure` / `SameSite`; pass the same
    values you used with `set_cookie` so deletion succeeds:

    ```python
    cookies.set_cookie(w, "sid", token, secure=True, httponly=True, path="/app")
    cookies.delete_cookie(w, "sid", secure=True, httponly=True, path="/app")
    ```
    """
    return set_cookie(
        w,
        name,
        "",
        max_age=0,
        path=path,
        domain=domain,
        secure=secure,
        httponly=httponly,
        samesite=samesite,
    )


# RFC 6265 4.1.1: a cookie name is an RFC 9110 token.
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_SIGNED_VERSION = "v1"
_MIN_KEY_BYTES = 32
# Browsers keep at least 4096 bytes of name, value, and attributes (RFC 6265
# 6.1); past it they drop the cookie without telling anyone.
_MAX_COOKIE_BYTES = 4096


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True, slots=True)
class SignedValue:
    """A verified, unexpired payload from `SignedCookie.get`."""

    data: dict[str, Any]
    issued_at: int
    expires_at: int

    @property
    def age(self) -> float:
        """Seconds since `SignedCookie.set` issued it."""
        return time.time() - self.issued_at


class SignedCookie:
    """A cookie with a JSON payload that the client can read but not change.

    `set` signs `{data, issued, expires}` with HMAC-SHA256. `get` returns a
    `SignedValue` only when the signature is valid and the expiry, which the
    server checks, has not passed; otherwise `None`. The payload is not
    encrypted: store ids, never secrets.

    `keys`: the first key signs; every key verifies. To rotate, put the new key
    first and keep the old one for one `max_age`. Each key is 32 bytes or more.
    The signing key is derived from the key and the cookie name, so one secret
    serves several cookies and a value cannot move to another cookie name.

    ```python
    jar = SignedCookie("session", keys=[config.session_secret])
    jar.set(w, {"user": user_id})
    value = jar.get(c.req)            # SignedValue | None
    jar.delete(w)
    ```

    Sliding expiry: when `value.age` is past half of `max_age`, call `set`
    again with `value.data`. `secure=True` is the default; pass
    `secure=False` only for plain HTTP in development.
    """

    __slots__ = (
        "_keys",
        "domain",
        "httponly",
        "max_age",
        "name",
        "path",
        "samesite",
        "secure",
    )

    def __init__(
        self,
        name: str,
        *,
        keys: Sequence[str | bytes],
        max_age: int = 14 * 24 * 3600,
        path: str = "/",
        domain: str | None = None,
        secure: bool = True,
        httponly: bool = True,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> None:
        if not _TOKEN.fullmatch(name):
            raise StarioError(
                "Invalid signed cookie name",
                context={"name": name},
                help_text="A cookie name is letters, digits, and !#$%&'*+-.^_`|~.",
            )
        if not keys:
            raise StarioError(
                "SignedCookie needs at least one key",
                help_text="Pass keys=[secret], with the secret from the app env.",
            )
        raw_keys = [k.encode() if isinstance(k, str) else k for k in keys]
        if any(len(k) < _MIN_KEY_BYTES for k in raw_keys):
            raise StarioError(
                f"SignedCookie keys must be at least {_MIN_KEY_BYTES} bytes",
                context={"name": name},
                help_text="Generate one with: python -c 'import secrets; "
                "print(secrets.token_urlsafe(32))'",
            )
        if max_age <= 0:
            raise StarioError(
                "SignedCookie max_age must be greater than 0",
                context={"max_age": max_age},
            )
        label = f"stario.cookie.{name}".encode()
        self._keys = tuple(
            hmac.new(k, label, hashlib.sha256).digest() for k in raw_keys
        )
        self.name = name
        self.max_age = max_age
        self.path = path
        self.domain = domain
        self.secure = secure
        self.httponly = httponly
        self.samesite = samesite

    def _sign(self, key: bytes, body: str) -> str:
        return _b64encode(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())

    def set(self, w: Writer, data: Mapping[str, Any]) -> Writer:
        """Sign `data` (JSON) and add `Set-Cookie`; a new expiry starts now."""
        now = int(time.time())
        payload = dumps_bytes({"d": dict(data), "i": now, "e": now + self.max_age})
        body = f"{_SIGNED_VERSION}.{_b64encode(payload)}"
        value = f"{body}.{self._sign(self._keys[0], body)}"
        # Leave about 200 bytes of the browser limit for the attributes.
        if len(self.name) + len(value) > _MAX_COOKIE_BYTES - 200:
            raise StarioError(
                "Signed cookie too large",
                context={"name": self.name, "bytes": len(value)},
                help_text="Store an id in the cookie and the data on the server.",
            )
        return set_cookie(
            w,
            self.name,
            value,
            max_age=self.max_age,
            path=self.path,
            domain=self.domain,
            secure=self.secure,
            httponly=self.httponly,
            samesite=self.samesite,
        )

    def get(self, req: Request) -> SignedValue | None:
        """The verified, unexpired payload, or `None` (missing, changed, expired)."""
        raw = req.cookies.get(self.name)
        if not raw:
            return None
        body, _, signature = raw.rpartition(".")
        version, _, encoded = body.partition(".")
        # A client can send any bytes; only an ASCII value can be ours.
        if (
            version != _SIGNED_VERSION
            or not encoded
            or not signature
            or not raw.isascii()
        ):
            return None
        if not any(
            hmac.compare_digest(self._sign(key, body), signature) for key in self._keys
        ):
            return None
        try:
            payload = loads(_b64decode(encoded))
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        data, issued, expires = payload.get("d"), payload.get("i"), payload.get("e")
        if (
            not isinstance(data, dict)
            or not isinstance(issued, int)
            or not isinstance(expires, int)
            or expires <= time.time()
        ):
            return None
        return SignedValue(data, issued, expires)

    def delete(self, w: Writer) -> Writer:
        """Expire the cookie, with the same path, domain, and flags as `set`."""
        return delete_cookie(
            w,
            self.name,
            path=self.path,
            domain=self.domain,
            secure=self.secure,
            httponly=self.httponly,
            samesite=self.samesite,
        )
