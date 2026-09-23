"""Calculator capability: arithmetic the Admiral says out loud, evaluated safely and shown on screen.

The voice model writes an ordinary arithmetic expression from what it heard ("15% of 80" -> "0.15*80")
and calls calculate(). Nothing here ever uses eval/exec: the expression is parsed with `ast` and walked
by a whitelist evaluator that only knows numbers, arithmetic operators and five functions.

Plain functions hold the logic (tested in test_calculator.py); the MCP tool is a thin wrapper.
"""
from __future__ import annotations

import ast
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from mcp.server.mcpserver import MCPServer

server = MCPServer("calculator")

MAX_LENGTH = 200          # characters of expression we will even parse
MAX_EXPONENT = 1000       # |exponent| of a power
MAX_DIGITS = 1000         # decimal digits any intermediate value may reach
MAX_BITS = int(MAX_DIGITS * 3.33) + 1
SIGNIFICANT = 10          # significant digits in a formatted result
EXACT_INT_DIGITS = 15     # integers up to this many digits print exactly, longer ones go scientific

CONSTANTS = {"pi": math.pi, "e": math.e}

# Unicode the Admiral's model (or a paste) might use for the four operations.
LENIENT = {"×": "*", "·": "*", "÷": "/", "−": "-", "–": "-", "—": "-", "^": "**"}


class CalcError(ValueError):
    """Something the Admiral should hear about: bad input, or a result we refuse to compute."""


# --- pretty printing -------------------------------------------------------------------------

# op -> (symbol, precedence, associativity)
BINARY = {
    ast.Add: ("+", 1, "L"),
    ast.Sub: ("−", 1, "L"),
    ast.Mult: ("×", 2, "L"),
    ast.Div: ("÷", 2, "L"),
    ast.FloorDiv: ("//", 2, "L"),
    ast.Mod: ("%", 2, "L"),
    ast.Pow: ("^", 4, "R"),
}
UNARY_PRECEDENCE = 3


def number_text(value: int | float) -> str:
    """A number as it should read back inside an expression (no thousands separators: commas separate args)."""
    if isinstance(value, int):
        return str(value)
    if value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def pretty(node: ast.AST, min_precedence: int = 0) -> str:
    """Render a validated expression tree with ×, ÷, −, ^ and only the parentheses it needs."""
    if isinstance(node, ast.Expression):
        return pretty(node.body)
    if isinstance(node, ast.Constant):
        return number_text(node.value)
    if isinstance(node, ast.Name):
        return "π" if node.id == "pi" else node.id
    if isinstance(node, ast.Call):
        args = ", ".join(pretty(a) for a in node.args)
        return f"{node.func.id}({args})"
    if isinstance(node, ast.UnaryOp):
        inner = pretty(node.operand, UNARY_PRECEDENCE)
        if isinstance(node.operand, ast.UnaryOp):  # −−3 reads badly; −(−3) doesn't
            inner = f"({inner})"
        text = ("−" if isinstance(node.op, ast.USub) else "+") + inner
        return f"({text})" if UNARY_PRECEDENCE < min_precedence else text
    if isinstance(node, ast.BinOp):
        symbol, precedence, assoc = BINARY[type(node.op)]
        left_min = precedence if assoc == "L" else precedence + 1
        right_min = precedence + 1 if assoc == "L" else precedence
        text = f"{pretty(node.left, left_min)} {symbol} {pretty(node.right, right_min)}"
        return f"({text})" if precedence < min_precedence else text
    raise CalcError("That isn't arithmetic I can calculate.")


def format_number(value: int | float) -> str:
    """A number the way a calculator shows it: no trailing .0, thousands separators, 10 significant digits."""
    if isinstance(value, float):
        if math.isnan(value):
            raise CalcError("That isn't a number I can show.")
        if math.isinf(value):
            raise CalcError("That result is too large to calculate.")
        exact = Decimal(repr(value))
    else:
        exact = Decimal(value)

    if isinstance(value, int) and len(str(abs(value))) <= EXACT_INT_DIGITS:
        return f"{value:,}"  # small whole numbers stay exact, however many digits they need

    with localcontext() as ctx:
        ctx.prec = SIGNIFICANT
        rounded = +exact

    if rounded == 0:
        return "0"
    if -5 <= rounded.adjusted() < EXACT_INT_DIGITS:
        sign = "-" if rounded < 0 else ""
        plain = format(abs(rounded), "f")
        whole, _, fraction = plain.partition(".")
        fraction = fraction.rstrip("0")
        return f"{sign}{int(whole):,}" + (f".{fraction}" if fraction else "")
    mantissa, _, exponent = format(rounded, f".{SIGNIFICANT - 1}E").partition("E")
    if "." in mantissa:
        mantissa = mantissa.rstrip("0").rstrip(".")
    power = int(exponent)
    return f"{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


# --- safe evaluation -------------------------------------------------------------------------

def check_magnitude(value: int | float) -> int | float:
    """Keep intermediate values inside calculator territory instead of letting them eat the machine."""
    if isinstance(value, int):
        if value.bit_length() > MAX_BITS:
            raise CalcError(f"That result is too large to calculate (over {MAX_DIGITS} digits).")
    elif isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            raise CalcError("That isn't a number I can calculate.")
        raise CalcError("That result is too large to calculate.")
    return value


def power(base: int | float, exponent: int | float) -> int | float:
    """base ** exponent, refusing the ones that would hang or go complex."""
    if abs(exponent) > MAX_EXPONENT:
        raise CalcError(f"That exponent is too large; keep it under {MAX_EXPONENT}.")
    if base < 0 and isinstance(exponent, float) and not exponent.is_integer():
        raise CalcError("Cannot raise a negative number to a fractional power.")
    if isinstance(base, int) and isinstance(exponent, int) and exponent >= 0:
        digits = max(base.bit_length(), 1) * exponent * 0.302
        if digits > MAX_DIGITS:
            raise CalcError(f"That result is too large to calculate (over {MAX_DIGITS} digits).")
    try:
        return base ** exponent
    except ZeroDivisionError:
        raise CalcError("Cannot divide by zero.") from None
    except OverflowError:
        raise CalcError("That result is too large to calculate.") from None


def _half_up(value: int | float, places: int) -> Decimal:
    exact = Decimal(value) if isinstance(value, int) else Decimal(repr(value))
    try:
        return exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise CalcError("That result is too large to round.") from None


def rounded(value: int | float, places: int | float = 0) -> int | float:
    """round(), but half away from zero the way a person expects: round(2.5) is 3, not 2."""
    if isinstance(places, float):
        if not places.is_integer():
            raise CalcError("round() needs a whole number of decimal places.")
        places = int(places)
    if abs(places) > MAX_DIGITS:
        raise CalcError("round() needs a sensible number of decimal places.")
    if isinstance(value, int) and places >= 0:
        return value
    result = _half_up(value, places)
    return int(result) if places <= 0 else float(result)


def smallest(*values: int | float) -> int | float:
    """min(), but min(4) is 4 rather than a TypeError: every argument here is already a number."""
    return min(values)


def largest(*values: int | float) -> int | float:
    """max() over plain numbers, single argument included."""
    return max(values)


def square_root(value: int | float) -> float:
    if value < 0:
        raise CalcError("Cannot take the square root of a negative number.")
    try:
        return math.sqrt(value)
    except OverflowError:
        raise CalcError("That number is too large to take the square root of.") from None


# name -> (function, min args, max args or None)
FUNCTIONS = {
    "sqrt": (square_root, 1, 1),
    "abs": (abs, 1, 1),
    "round": (rounded, 1, 2),
    "min": (smallest, 1, None),
    "max": (largest, 1, None),
}

BINARY_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: power,
}


def normalize(expression: str) -> str:
    """Accept the symbols a person (or a speech model) might write, and reject an expression we won't parse."""
    if not isinstance(expression, str):
        raise CalcError("Give me an expression to calculate, like 3 + 3.")
    text = expression.strip()
    if not text:
        raise CalcError("Give me an expression to calculate, like 3 + 3.")
    if len(text) > MAX_LENGTH:
        raise CalcError(f"That expression is too long (over {MAX_LENGTH} characters).")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise CalcError("Write the calculation on one line.")
    for symbol, replacement in LENIENT.items():
        text = text.replace(symbol, replacement)
    return text


def parse(expression: str) -> ast.Expression:
    """Parse a normalized expression, turning every syntax problem into one clear message."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise CalcError(f"I couldn't read {expression!r} as a calculation.") from None
    except (ValueError, MemoryError, RecursionError):
        raise CalcError("That expression is too complicated to calculate.") from None
    if isinstance(tree.body, ast.Tuple):
        raise CalcError("That looks like several values. Give me one calculation at a time.")
    return tree


def evaluate(node: ast.AST) -> int | float:
    """Walk a parsed expression, allowing numbers, arithmetic and the five known functions. Nothing else."""
    if isinstance(node, ast.Expression):
        return evaluate(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError("Only numbers can be calculated with.")
        return check_magnitude(node.value)

    if isinstance(node, ast.Name):
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        raise CalcError(f"I don't know what {node.id!r} is. Use numbers, pi or e.")

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            raise CalcError("That isn't arithmetic I can calculate.")
        value = evaluate(node.operand)
        return check_magnitude(-value if isinstance(node.op, ast.USub) else +value)

    if isinstance(node, ast.BinOp):
        operation = BINARY_OPS.get(type(node.op))
        if operation is None:
            raise CalcError("That isn't arithmetic I can calculate.")
        left, right = evaluate(node.left), evaluate(node.right)
        try:
            return check_magnitude(operation(left, right))
        except ZeroDivisionError:
            raise CalcError("Cannot divide by zero.") from None
        except OverflowError:
            raise CalcError("That result is too large to calculate.") from None

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalcError("I can only call sqrt, abs, round, min and max.")
        entry = FUNCTIONS.get(node.func.id)
        if entry is None:
            raise CalcError(f"I don't know the function {node.func.id!r}. I know sqrt, abs, round, min and max.")
        if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
            raise CalcError(f"{node.func.id}() takes plain numbers.")
        function, low, high = entry
        if len(node.args) < low or (high is not None and len(node.args) > high):
            wanted = f"{low}" if high == low else (f"{low} or more" if high is None else f"{low} to {high}")
            raise CalcError(f"{node.func.id}() takes {wanted} numbers, not {len(node.args)}.")
        args = [evaluate(a) for a in node.args]
        try:
            return check_magnitude(function(*args))
        except CalcError:
            raise
        except (ValueError, TypeError):
            raise CalcError(f"{node.func.id}() can't handle those numbers.") from None
        except OverflowError:
            raise CalcError("That result is too large to calculate.") from None

    raise CalcError("That isn't arithmetic I can calculate.")


def tidy(value: int | float) -> int | float:
    """Hand back 12 rather than 12.0 when the answer is a whole number."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return int(value)
    return value


def compute(expression: str) -> dict:
    """The whole job: normalize, parse, check, evaluate, and dress the answer for the screen."""
    text = normalize(expression)
    tree = parse(text)
    try:
        value = tidy(evaluate(tree))
        shown = pretty(tree)
    except RecursionError:
        raise CalcError("That expression is too complicated to calculate.") from None
    formatted = format_number(value)
    return {
        "result": value,
        "expression": shown,
        "display": {"title": "Calculator", "lines": [shown, f"= {formatted}"], "seconds": 12},
    }


@server.tool()
def calculate(expression: str) -> dict:
    """Work out an arithmetic expression and show it on screen.

    Write what the Admiral said as ordinary arithmetic: "15% of 80" as "0.15*80", "twelve point five times four
    over three" as "(12.5*4)/3". Supports + - * / // % ^ (power), parentheses, decimals, negative numbers,
    sqrt, abs, round, min, max, pi and e.
    """
    try:
        return compute(expression)
    except CalcError as e:
        return {
            "error": str(e),
            "expression": (expression or "").strip(),
            "display": {"title": "Calculator", "lines": [(expression or "").strip(), str(e)], "seconds": 12},
        }


if __name__ == "__main__":
    server.run()
