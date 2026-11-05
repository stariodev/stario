"""HTML form submissions: text fields and uploaded files.

```python
from stario.forms import Form, UploadFile, read_form
```

`read_form` follows the same rules as `stario.datastar.read_signals`: the
method decides where the data is, limits are keyword arguments, and errors are
`RequestBodyError`. Use `read_form` for an HTML form (or a Datastar form
submit) and `read_signals` for Datastar signals. A file in a signal is a
base64 `FileSignal`; a file in a form is a streamed `UploadFile`.
"""

from .form import Form, UploadFile
from .read import read_form

__all__ = ["Form", "UploadFile", "read_form"]
