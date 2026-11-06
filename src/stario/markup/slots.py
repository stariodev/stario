"""
`@baked` slot machinery shared by `tag`, `render`, and `baked`.

Everything here is internal to `stario.markup`. Users only ever see plain function
parameters; `BakeSlot` is what those parameters become while the builder runs once
at decoration time. The guard dunders make every misuse of a parameter at bake
time (formatting, concatenation, branching, iteration, attribute access) a
loud `StarioError` instead of silently baking a placeholder into the plan.

Equality and truthiness (`==`, `!=`, `if` / `and` / `or`) fail loudly: they
would use placeholder objects, not call-time values. Identity checks (`is`,
`is not`) do not — placeholders are real objects, so `param is not None` is
always true at bake time.

Cross-file coupling: tag.py, baked.py, and render.py import via identity checks.
"""

from dataclasses import dataclass

from stario.exceptions import StarioError

# Attribute name deliberately obscure: any plausible-looking attribute access on a
# slot (`user.name`, `item.id`, ...) must miss and hit `__getattr__`.
_SLOT_NAME_FIELD = "_stario_slot_parameter_name"


class BakeSlot:
    """Placeholder for one `@baked` parameter during the capture run."""

    __slots__ = (_SLOT_NAME_FIELD,)

    def __init__(self, name: str) -> None:
        object.__setattr__(self, _SLOT_NAME_FIELD, name)

    def _guard(self, operation: str) -> StarioError:
        name: str = getattr(self, _SLOT_NAME_FIELD)
        return StarioError(
            f"@baked: parameter {name!r} cannot be used with {operation} at bake time",
            context={"parameter": name},
            help_text=(
                "The builder runs once at decoration with placeholder parameters. "
                "Compute derived values per call and pass the result as the "
                "argument, or move this logic out of the @baked builder."
            ),
        )

    def __repr__(self) -> str:
        return f"BakeSlot({getattr(self, _SLOT_NAME_FIELD)!r})"

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __getattr__(self, name: str):
        # Keep protocol probing (copy, pickle, inspect) on dunders working via
        # the normal AttributeError contract; everything else is user misuse.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise self._guard(f"attribute access ('.{name}')")

    # --- misuse guards (each raises StarioError, never returns) -------------

    def __str__(self) -> str:
        raise self._guard("str() or f-string formatting")

    def __format__(self, format_spec: str) -> str:
        raise self._guard("string formatting")

    def __add__(self, other: object):
        raise self._guard("concatenation ('+')")

    def __radd__(self, other: object):
        raise self._guard("concatenation ('+')")

    def __mod__(self, other: object):
        raise self._guard("'%' formatting")

    def __rmod__(self, other: object):
        raise self._guard("'%' formatting")

    def __bool__(self) -> bool:
        raise self._guard("truthiness (if / and / or)")

    def __eq__(self, other: object) -> bool:
        raise self._guard("equality (==)")

    def __ne__(self, other: object) -> bool:
        raise self._guard("inequality (!=)")

    def __iter__(self):
        raise self._guard("iteration or unpacking")

    def __getitem__(self, item: object):
        raise self._guard("subscripting ('[...]')")

    def __len__(self) -> int:
        raise self._guard("len()")

    def __call__(self, *args: object, **kwargs: object) -> object:
        raise self._guard("calling ()")


def slot_name(slot: BakeSlot) -> str:
    """Internal accessor for a slot's parameter name."""
    return getattr(slot, _SLOT_NAME_FIELD)


def bake_slot_if_present(value: object) -> BakeSlot | None:
    """Return a bake slot when `value` is one (runtime @baked misuse guard)."""
    if type(value) is BakeSlot:
        return value
    return None


@dataclass(frozen=True, slots=True)
class AttrSlot:
    """A whole attribute value bound to one parameter (`{"href": url}`)."""

    key: str  # validated attribute name, ready for the wire
    name: str  # parameter name


@dataclass(frozen=True, slots=True)
class RawSlot:
    """A run of raw-text content with parameters in it (`h.Script(code)`).

    `parts` is the run in document order: `str` pieces are literal text and
    `BakeSlot` entries are parameters. The splice joins the run with the
    values in place, verbatim, and runs the element's breakout check once
    over the whole, so a value cannot complete a `</script` with the text
    around it.
    """

    tag: str  # element name, for the breakout check
    parts: tuple[str | BakeSlot, ...]
