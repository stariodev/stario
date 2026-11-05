"""`Form` (text fields and uploaded files) and `UploadFile`.

Field access matches `req.query` (`ParsedQuery`): `get` returns the first
value, `getlist` all of them, in the order the client sent them. A form that
moves from `method="get"` to `method="post"` keeps the same handler code.
"""

import asyncio
from collections.abc import Iterable
from tempfile import SpooledTemporaryFile
from types import TracebackType
from typing import Self, overload


class UploadFile:
    """One file part of a `multipart/form-data` body.

    `file` is a `SpooledTemporaryFile`: in memory up to the spool size of
    `read_form`, then on disk. `filename` is the client's name without a
    path part; it is untrusted, so never use it as a path as it is.
    """

    __slots__ = ("content_type", "file", "filename", "size")

    def __init__(
        self,
        filename: str,
        content_type: str | None,
        file: SpooledTemporaryFile[bytes],
        size: int,
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.file = file
        self.size = size

    async def read(self) -> bytes:
        """Return the whole content (from the start of the file).

        A file that moved to disk is read in a thread, so the event loop
        keeps serving other requests.
        """
        if getattr(self.file, "_rolled", False):
            return await asyncio.to_thread(self._read_all)
        return self._read_all()

    def _read_all(self) -> bytes:
        self.file.seek(0)
        return self.file.read()

    def close(self) -> None:
        self.file.close()

    def __repr__(self) -> str:
        return (
            f"UploadFile(filename={self.filename!r}, "
            f"content_type={self.content_type!r}, size={self.size})"
        )


class Form:
    """Text fields and uploaded files of one form submission.

    Close it (or use `with form:`) to delete spooled files at once; otherwise
    they go when the `Form` is garbage-collected.
    """

    __slots__ = ("_fields", "_files")

    def __init__(
        self,
        fields: Iterable[tuple[str, str]] = (),
        files: Iterable[tuple[str, UploadFile]] = (),
    ) -> None:
        self._fields = list(fields)
        self._files = list(files)

    @overload
    def get(self, name: str) -> str | None: ...
    @overload
    def get[T](self, name: str, default: T) -> str | T: ...
    def get(self, name: str, default: object = None) -> object:
        """First value of the text field `name`, else `default`."""
        for key, value in self._fields:
            if key == name:
                return value
        return default

    def getlist(self, name: str) -> list[str]:
        """Every value of the text field `name`, in order."""
        return [value for key, value in self._fields if key == name]

    def items(self) -> list[tuple[str, str]]:
        """Every text field as `(name, value)`, in order."""
        return list(self._fields)

    def as_dict(self, *, last: bool = False) -> dict[str, str]:
        """One value per text field: the first, or the last with `last=True`."""
        out: dict[str, str] = {}
        for key, value in self._fields:
            if last or key not in out:
                out[key] = value
        return out

    def file(self, name: str) -> UploadFile | None:
        """First file of the field `name`, else `None`."""
        for key, upload in self._files:
            if key == name:
                return upload
        return None

    def filelist(self, name: str) -> list[UploadFile]:
        """Every file of the field `name` (`<input type="file" multiple>`)."""
        return [upload for key, upload in self._files if key == name]

    def file_items(self) -> list[tuple[str, UploadFile]]:
        """Every file as `(name, UploadFile)`, in order."""
        return list(self._files)

    def close(self) -> None:
        """Close every uploaded file and delete its spool."""
        for _, upload in self._files:
            upload.close()

    def __contains__(self, name: object) -> bool:
        return any(key == name for key, _ in self._fields) or any(
            key == name for key, _ in self._files
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Form(fields={len(self._fields)}, files={len(self._files)})"
