"""What an IP-XACT expression is worth: a number, or text another scope or a compiler can read.

IP-XACT writes its expressions in SystemVerilog's syntax, and XactFlow's evaluator works out the ones made of
numbers and parameters. This is what the elaboration and the generator need around that evaluator: telling the
parameters in an expression from everything else in it, a number checked to be one, and a value as text,
worked out as far as it can be here and left to the compiler where it cannot.
"""

from __future__ import annotations

import math
import re
from typing import Callable, List, Optional, Tuple, Union

import ipxact
from xactflow import ExpressionError, evaluate

from .keywords import SYSTEMVERILOG_KEYWORDS

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# What an expression is made of, as far as telling its parameters from everything else takes.
TOKEN = re.compile(
    r"""
      (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<system>\$\w+)
    | (?P<escaped>\\\S+)
    | (?P<package>\w+(?:::\w+)+)
    | (?P<based>(?:\d[\d_]*\s*)?'[sS]?(?:[bB]\s*[01_xXzZ?]+|[oO]\s*[0-7_xXzZ?]+|[dD]\s*[0-9_xXzZ?]+|[hH]\s*[0-9a-fA-F_xXzZ?]+))
    | (?P<fill>'[01xXzZ])
    | (?P<real>\d[\d_]*\.\d[\d_]*(?:[eE][+-]?\d+)?|\d[\d_]*[eE][+-]?\d+)
    | (?P<decimal>\d[\d_]*)
    | (?P<name>[A-Za-z_][\w$]*)
    """,
    re.X,
)
_ALONE = re.compile(r"""\d+|"(?:[^"\\]|\\.)*"|\w+(?:::\w+)+""")  # safe next to anything without parentheses
BOOLEANS = {"true": "1'b1", "false": "1'b0"}  # IP-XACT's spelling, which evaluate() does not read
_BIT_VECTOR = re.compile(r"(?:logic|bit|reg)(?:\s+unsigned)?\s*(?:\[[^\[\]]*\]\s*)*")
DIMENSION = re.compile(r"\[([^\[\]]*)\]")

Resolver = Callable[[str], object]


def text_of(value: object) -> str:
    """The text of an Expression or TextValue, or of a plain string."""
    return str(getattr(value, "value", value))


def two_bounds(dimension: str) -> Optional[Tuple[str, str]]:
    """The left and right of a dimension written `left:right`. The colon between them is the one outside any
    brackets that belongs to no `pkg::name` and to no `condition ? a : b`."""
    depth = pending = 0
    found = []
    for position, character in enumerate(dimension):
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif depth == 0 and character == "?":
            pending += 1
        elif depth == 0 and character == ":" and dimension[position - 1 : position] != ":" and dimension[position + 1 : position + 2] != ":":
            if pending:
                pending -= 1
            else:
                found.append(position)
    if len(found) != 1:
        return None
    return dimension[: found[0]].strip(), dimension[found[0] + 1 :].strip()


def is_typed(parameter: ipxact.Parameter) -> bool:
    """Whether a parameter's type changes its value: the evaluator casts to it, as a compiler would."""
    return parameter.type not in (None, "string")


def parameter_of(name: str, resolve: Optional[Resolver]) -> Optional[ipxact.Parameter]:
    if resolve is None:
        return None
    try:
        return resolve(name)
    except ExpressionError:
        return None


def _all_ones(text: str, resolve: Optional[Resolver], seen: Tuple[str, ...] = ()) -> bool:
    """Whether this is '1, directly or through parameters with no type to give it a width of its own."""
    text = text.strip()
    if text == "'1":
        return True
    if text in seen or not IDENTIFIER.fullmatch(text):
        return False
    parameter = parameter_of(text, resolve)
    if parameter is None or is_typed(parameter):
        return False
    return _all_ones(text_of(parameter.value), resolve, (*seen, text))


def _fill_inside_an_expression(text: str, resolve: Optional[Resolver], seen: Tuple[str, ...] = (), alone: bool = True) -> bool:
    """A fill literal takes its width from what is around it, which the evaluator does not know. Alone it
    is fine, and through a typed parameter the type gives it a width."""
    tokens = list(TOKEN.finditer(text))
    alone = alone and len(tokens) == 1 and tokens[0].group(0) == text.strip()
    for token in tokens:
        if token.lastgroup == "fill" and not alone:
            return True
        if token.lastgroup == "name" and token.group(0) not in seen:
            parameter = parameter_of(token.group(0), resolve)
            if parameter is not None and not is_typed(parameter):
                if _fill_inside_an_expression(text_of(parameter.value), resolve, (*seen, token.group(0)), alone):
                    return True
    return False


def number_of(expression: object, resolve: Optional[Resolver], where: str, constant: bool = False) -> int:
    """A number this generator needs itself. `constant` is for a value to drive onto a net, where '1 means
    all ones at whatever width the net has: that comes back as -1, which is all ones at any width."""
    text = text_of(expression)
    if constant and _all_ones(text, resolve):
        return -1
    if _all_ones(text, resolve) or _fill_inside_an_expression(text, resolve):
        raise ValueError(f"{where}: {text!r} has no width of its own here, so it cannot be worked out")
    try:
        value = evaluate(text, resolve)
        whole = int(value)
    except ExpressionError as exc:
        raise ValueError(f"{where}: cannot evaluate {text!r}: {exc}") from exc
    except (OverflowError, ValueError, MemoryError, RecursionError) as exc:  # infinity, NaN, or absurdly large
        raise ValueError(f"{where}: {text!r} is not a usable number: {exc}") from exc
    if value != whole:
        raise ValueError(f"{where}: {text!r} is not an integer, it evaluates to {value}")
    return whole


def sv_number(value: Union[int, float]) -> str:
    """A number as SystemVerilog. Past what an unsized literal is sure to hold it needs a size, and is
    signed, as an unsized decimal is, so that a minus sign in front of it still means a negative number."""
    if isinstance(value, float) or abs(value) < 1 << 31:
        return str(value)
    return f"{'-' if value < 0 else ''}{abs(value).bit_length() + 1}'sd{abs(value)}"


def _sized(number: int, width: Optional[int], signed: bool) -> str:
    """A number as a literal of the evaluator's width and sign, so that an untyped HDL parameter, which takes
    its width from what it is given, gets the right one. A negative is written as its bits, as -8'sd128 would
    be +128 once extended. A number its width cannot hold, which the evaluator does not wrap, is left unsized
    rather than cut."""
    if width is None and not signed and -(1 << 32) <= number < 1 << 32:
        width = 32  # unsized but unsigned, as an unsized based literal is: a plain decimal would be signed
    if width is None:
        return sv_number(number)
    mask = (1 << width) - 1
    if signed and -(1 << (width - 1)) <= number < 1 << (width - 1):
        return f"{width}'sd{number}" if number >= 0 else f"{width}'sh{number & mask:x}"
    if not signed and -(1 << width) <= number <= mask:
        return f"{width}'d{number & mask}"  # an unsigned negative is its bits, as SystemVerilog has it
    return sv_number(number)


def worked_out(expression: Union[str, ipxact.Parameter], resolve: Resolver) -> str:
    """A value worked out by the evaluator, as SystemVerilog, with the width and sign it gives the value.
    A Parameter is cast to its own type. The evaluator only offers width and sign through its private
    _evaluate."""
    try:
        from xactflow.expressions import _evaluate
    except ImportError:  # pragma: no cover - a later XactFlow without it: the plain number is still right
        return sv_number(evaluate(expression, resolve))
    value = _evaluate(expression, resolve, None, set())
    number = value.number
    if isinstance(number, float):
        if not math.isfinite(number):
            raise ValueError(f"{text_of(getattr(expression, 'value', expression))!r} is not a usable number, it evaluates to {number}")
        return str(number)
    if value.fill and not number >> 1:
        return "'1" if number else "'0"
    return _sized(number, value.width, value.signed)


def _literal_as_written(token: str, kind: str) -> str:
    """A literal, sized if it needs more than the 32 bits an unsized one is sure to have."""
    if kind == "decimal":
        value = int(token.replace("_", ""))
        return sv_number(value) if value >> 31 else token
    if kind == "based" and token.startswith("'"):
        digits = re.sub(r"[_\s]", "", token[1:]).lstrip("sS")
        if not re.search(r"[xXzZ?]", digits):
            value = int(digits[1:], {"b": 2, "o": 8, "d": 10, "h": 16}[digits[0].lower()])
            if value >> 32:
                return f"{value.bit_length()}{token}"
    return token


def names_in(text: str) -> List[str]:
    """The names an expression uses, leaving out strings and what is in a package."""
    return [token.group(0) for token in TOKEN.finditer(text) if token.lastgroup == "name" and token.group(0) not in BOOLEANS]


def says_more_than_numbers(text: str, resolve: Resolver, seen: Tuple[str, ...] = ()) -> bool:
    """Whether the expression, or a parameter it uses, holds a string or something in a package."""
    for token in TOKEN.finditer(text):
        if token.lastgroup in ("string", "package"):
            return True
        if token.lastgroup == "name" and token.group(0) not in seen:
            parameter = parameter_of(token.group(0), resolve)
            if parameter is not None and says_more_than_numbers(text_of(parameter.value), resolve, (*seen, token.group(0))):
                return True
    return False


def _check_is_one_value(text: str, where: str) -> None:
    """A value is written between the brackets of `.NAME(...)`, so it has to stay there: its own brackets
    closed, and no comma or semicolon outside them."""
    depth = 0
    for token in re.finditer(r'"(?:[^"\\]|\\.)*"|[()\[\]{},;]', text):
        character = token.group(0)
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        if depth < 0 or (depth == 0 and character in ",;"):
            break
    else:
        if depth == 0:
            return
    raise ValueError(f"{where}: {text!r} is not one value")


def single_token(text: str) -> bool:
    tokens = list(TOKEN.finditer(text))
    return len(tokens) == 1 and tokens[0].group(0) == text.strip()


def resolved_text(
    text: str, resolve: Resolver, where: str, seen: Tuple[str, ...] = (), keep_unknown: bool = False, own: Optional[str] = None
) -> str:
    """A value as SystemVerilog, for the instance of a module.

    With no parameters in it, it is written as it stands, as only the compiler knows what 8'hA5 or '1
    means where it lands. So is `own`, the module parameter's own value, which is what the HDL declares
    for it, and a parameter whose value is a single literal. Anything else that uses parameters is worked
    out here, by the same evaluator as port widths, so the two cannot disagree. Written inline, another
    parameter's expression would lose its self-determined width, and any type it has.

    What the evaluator cannot do, a string or something in a package, is left to the compiler with the
    numbers filled in. `keep_unknown` leaves a name that is not a parameter here, for a narrower scope to
    resolve; otherwise it is refused."""
    text = TOKEN.sub(lambda t: BOOLEANS[t.group(0)] if t.group(0) in BOOLEANS else t.group(0), text.strip())
    if not text:
        raise ValueError(f"{where}: the value is empty")
    _check_is_one_value(text, where)
    if _BIT_VECTOR.fullmatch(text) and "[" in text:
        # A type, as a type parameter has for its value: each bound is a value of its own (D31).
        def bound(dimension: "re.Match[str]") -> str:
            sides = two_bounds(dimension.group(1))
            if sides is None:
                raise ValueError(f"{where}: cannot work out the dimension {dimension.group(0)} of {text!r}")
            return "[" + ":".join(resolved_text(side, resolve, where, seen, keep_unknown) for side in sides) + "]"

        return DIMENSION.sub(bound, text)
    names = names_in(text)
    parameters = [name for name in names if parameter_of(name, resolve) is not None]
    unknown = [name for name in names if name not in parameters and name not in SYSTEMVERILOG_KEYWORDS]
    if unknown and not keep_unknown:
        raise ValueError(f"{where}: {text!r} uses {', '.join(unknown)}, which no parameter here is called")
    if "$ipxact_" in text:  # IP-XACT's own functions mean nothing to a compiler, and they need a context here
        raise ValueError(f"{where}: {text!r} uses an IP-XACT function, which is not supported in a module parameter yet")

    if not parameters:
        if not unknown:
            _check_is_usable(text, resolve, where)
        return TOKEN.sub(lambda t: _literal_as_written(t.group(0), t.lastgroup), text)

    if text in seen:
        raise ValueError(f"{where}: {' -> '.join((*seen, text))} refers to itself")
    if IDENTIFIER.fullmatch(text):
        parameter = parameter_of(text, resolve)
        value = text_of(parameter.value)
        if not is_typed(parameter) and (text == own or parameter.name == own or parameter.parameter_id == own or single_token(value)):
            return resolved_text(value, resolve, where, (*seen, text), keep_unknown)

    if unknown:
        return _with_numbers_filled_in(text, resolve, where, seen, keep_unknown)
    if _fill_inside_an_expression(text, resolve):
        raise ValueError(f"{where}: {text!r} has a fill literal inside an expression, so it cannot be worked out here")
    try:
        return worked_out(text, resolve)
    except ExpressionError as exc:
        if says_more_than_numbers(text, resolve):
            if IDENTIFIER.fullmatch(text):
                # A parameter whose value only the compiler can finish, such as a call of a package's function:
                # its value stands in for it. Filling in the name itself would come straight back here.
                parameter = parameter_of(text, resolve)
                if is_typed(parameter):
                    raise ValueError(f"{where}: cannot work out {text!r}, a {parameter.type} whose value is {text_of(parameter.value)!r}") from exc
                return resolved_text(text_of(parameter.value), resolve, where, (*seen, text), keep_unknown)
            return _with_numbers_filled_in(text, resolve, where, seen, keep_unknown)
        raise ValueError(f"{where}: cannot evaluate {text!r}: {exc}") from exc
    except (OverflowError, ValueError, MemoryError, RecursionError) as exc:
        raise ValueError(f"{where}: {text!r} is not a usable number: {exc}") from exc


def _check_is_usable(text: str, resolve: Resolver, where: str) -> None:
    """A division by zero in a literal expression would quietly become x in the compiler."""
    try:
        value = evaluate(text, resolve)
    except ExpressionError as exc:
        if "by zero" in str(exc):
            raise ValueError(f"{where}: cannot evaluate {text!r}: {exc}") from exc
    except (OverflowError, ValueError, MemoryError, RecursionError) as exc:
        raise ValueError(f"{where}: {text!r} is not a usable number: {exc}") from exc
    else:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{where}: {text!r} is not a usable number, it evaluates to {value}")


def _with_numbers_filled_in(text: str, resolve: Resolver, where: str, seen: Tuple[str, ...], keep_unknown: bool) -> str:
    """An expression only the compiler can finish, with each parameter it uses replaced by its value."""

    def replace(token: "re.Match[str]") -> str:
        if token.lastgroup in ("based", "decimal"):
            return _literal_as_written(token.group(0), token.lastgroup)
        if token.lastgroup != "name" or parameter_of(token.group(0), resolve) is None:
            return token.group(0)
        if re.match(r"\s*\[", text[token.end() :]) or token.group(0) in seen:
            raise ValueError(f"{where}: cannot work out {text!r}")
        value = resolved_text(token.group(0), resolve, where, seen, keep_unknown)
        return value if _ALONE.fullmatch(value) else f"({value})"

    if "'{" in text:
        raise ValueError(f"{where}: cannot work out {text!r}")
    return TOKEN.sub(replace, text)
