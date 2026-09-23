"""Tests for the calculator capability's logic (the MCP tool is a thin wrapper over these).

Two things matter here: the arithmetic and formatting the Admiral hears, and that nothing outside
arithmetic can ever be evaluated.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load():
    """Import this capability's server.py under a unique name (every capability has a server.py)."""
    spec = importlib.util.spec_from_file_location("guppy_capability_calculator", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calc = _load()
CalcError = calc.CalcError
compute = calc.compute
evaluate = calc.evaluate
format_number = calc.format_number
normalize = calc.normalize
parse = calc.parse
pretty = calc.pretty


def value(expression: str):
    """The number an expression works out to."""
    return compute(expression)["result"]


def error(expression: str) -> str:
    """The message an invalid expression produces."""
    with pytest.raises(CalcError) as caught:
        compute(expression)
    return str(caught.value)


# --- the manifest ----------------------------------------------------------------------------

def test_manifest_declares_every_exposed_tool():
    manifest = json.loads((HERE / "capability.json").read_text())
    assert manifest["name"] == "calculator"
    assert manifest["enabled"] is True
    assert manifest["taints"] == []
    exposed = {t.name for t in asyncio.run(calc.server.list_tools())}
    assert exposed == {"calculate"}
    assert manifest["effects"] == {"calculate": "read"}


def test_reflex_tools_are_declared_and_only_read_or_draft():
    manifest = json.loads((HERE / "capability.json").read_text())
    assert manifest["reflex"] == ["calculate"]
    for tool in manifest["reflex"]:  # the kernel drops reflex tools that aren't read/draft
        assert manifest["effects"][tool] in {"read", "draft"}


def test_every_tool_has_a_description_for_the_mind():
    for tool in asyncio.run(calc.server.list_tools()):
        assert (tool.description or "").strip()


def test_the_tool_takes_an_expression_string():
    tool = next(t for t in asyncio.run(calc.server.list_tools()) if t.name == "calculate")
    assert tool.input_schema["properties"]["expression"]["type"] == "string"
    assert tool.input_schema.get("required") == ["expression"]


# --- arithmetic ------------------------------------------------------------------------------

def test_the_four_operations():
    assert value("3+3") == 6
    assert value("10-4") == 6
    assert value("6*7") == 42
    assert value("9/3") == 3


def test_percentages_as_the_voice_model_writes_them():
    assert value("0.15*80") == 12
    assert value("(12.5*4)/3") == pytest.approx(16.666666666666668)


def test_precedence_and_parentheses():
    assert value("2+3*4") == 14
    assert value("(2+3)*4") == 20
    assert value("2+3*4-6/3") == 12
    assert value("10-2-3") == 5  # left associative, not 11
    assert value("100/10/2") == 5


def test_floor_division_and_modulo():
    assert value("7//2") == 3
    assert value("7%2") == 1
    assert value("-7//2") == -4
    assert value("17 % 5") == 2


def test_powers_both_ways():
    assert value("2^10") == 1024
    assert value("2**10") == 1024
    assert value("2^3^2") == 512  # right associative
    assert value("(2^3)^2") == 64
    assert value("2^-1") == 0.5
    assert value("2^0.5") == pytest.approx(math.sqrt(2))


def test_unary_minus_and_decimals():
    assert value("-5") == -5
    assert value("-5+8") == 3
    assert value("-2^2") == -4  # the power binds tighter than the minus
    assert value("(-2)^2") == 4
    assert value("--3") == 3
    assert value("0.1+0.2") == pytest.approx(0.3)
    assert value(".5*4") == 2


def test_whitespace_and_unicode_operators_are_accepted():
    assert value("  3   +   3  ") == 6
    assert value("6 × 7") == 42
    assert value("84 ÷ 2") == 42
    assert value("10 − 4") == 6


def test_normalize_rewrites_the_symbols_a_speaker_might_use():
    assert normalize("2 ^ 3") == "2 ** 3"
    assert normalize(" 6×7 ") == "6*7"
    assert normalize("8÷2") == "8/2"


# --- functions and constants ------------------------------------------------------------------

def test_sqrt_abs_min_max():
    assert value("sqrt(144)") == 12
    assert value("sqrt(2)") == pytest.approx(math.sqrt(2))
    assert value("abs(-7)") == 7
    assert value("abs(3-10)") == 7
    assert value("min(3,7)") == 3
    assert value("max(3,7,11)") == 11
    assert value("min(4)") == 4


def test_round_goes_half_away_from_zero_like_a_person_expects():
    assert value("round(2.5)") == 3  # not Python's banker's rounding
    assert value("round(-2.5)") == -3
    assert value("round(2.4)") == 2
    assert value("round(3.14159, 2)") == pytest.approx(3.14)
    assert value("round(2.675, 2)") == pytest.approx(2.68)
    assert value("round(1234, -2)") == 1200


def test_constants():
    assert value("pi") == pytest.approx(math.pi)
    assert value("e") == pytest.approx(math.e)
    assert value("2*pi") == pytest.approx(2 * math.pi)
    assert value("round(pi, 4)") == pytest.approx(3.1416)


def test_functions_nest():
    assert value("sqrt(abs(-16))") == 4
    assert value("max(sqrt(81), min(10, 8))") == 9


# --- formatting ------------------------------------------------------------------------------

def test_whole_numbers_lose_the_trailing_zero():
    assert format_number(6) == "6"
    assert format_number(6.0) == "6"
    assert compute("9/3")["result"] == 3
    assert compute("9/3")["display"]["lines"][-1] == "= 3"


def test_thousands_separators():
    assert format_number(1234567) == "1,234,567"
    assert format_number(-1234567) == "-1,234,567"
    assert format_number(1234.5) == "1,234.5"
    assert format_number(999) == "999"


def test_ten_significant_digits():
    assert format_number(1 / 3) == "0.3333333333"
    assert format_number(2 / 3) == "0.6666666667"
    assert format_number(math.pi) == "3.141592654"
    assert format_number(12.5 * 4 / 3) == "16.66666667"


def test_very_large_and_very_small_numbers_go_scientific():
    assert format_number(2 ** 1000).startswith("1.071508607e+30")
    assert format_number(1.5e20) == "1.5e+20"
    assert format_number(0.00000001234) == "1.234e-8"
    assert format_number(0) == "0"
    assert format_number(-0.0) == "0"


def test_exact_integers_stay_exact_up_to_fifteen_digits():
    assert format_number(2 ** 40) == "1,099,511,627,776"
    assert format_number(10 ** 15) == "1e+15"


def test_nan_and_infinity_are_refused():
    with pytest.raises(CalcError):
        format_number(float("nan"))
    with pytest.raises(CalcError, match="too large"):
        format_number(float("inf"))


# --- the pretty expression --------------------------------------------------------------------

def test_pretty_uses_the_calculator_symbols():
    assert compute("3+3")["expression"] == "3 + 3"
    assert compute("0.15*80")["expression"] == "0.15 × 80"
    assert compute("84/2")["expression"] == "84 ÷ 2"
    assert compute("10-4")["expression"] == "10 − 4"
    assert compute("2**10")["expression"] == "2 ^ 10"
    assert compute("2^10")["expression"] == "2 ^ 10"


def test_pretty_keeps_only_the_parentheses_it_needs():
    assert compute("(12.5*4)/3")["expression"] == "12.5 × 4 ÷ 3"
    assert compute("(2+3)*4")["expression"] == "(2 + 3) × 4"
    assert compute("2+3*4")["expression"] == "2 + 3 × 4"
    assert compute("10-(2-3)")["expression"] == "10 − (2 − 3)"
    assert compute("100/(10*2)")["expression"] == "100 ÷ (10 × 2)"
    assert compute("(-2)^2")["expression"] == "(−2) ^ 2"
    assert compute("-2^2")["expression"] == "−2 ^ 2"
    assert compute("--3")["expression"] == "−(−3)"


def test_pretty_renders_functions_and_constants():
    assert compute("sqrt(144)")["expression"] == "sqrt(144)"
    assert compute("min(3, 7)")["expression"] == "min(3, 7)"
    assert compute("round(pi, 2)")["expression"] == "round(π, 2)"


# --- the display card -------------------------------------------------------------------------

def test_the_result_shape_is_what_the_kernel_expects():
    out = compute("0.15*80")
    assert out == {
        "result": 12,
        "expression": "0.15 × 80",
        "display": {"title": "Calculator", "lines": ["0.15 × 80", "= 12"], "seconds": 12},
    }


def test_the_card_shows_the_expression_then_the_result():
    card = compute("(12.5*4)/3")["display"]
    assert card["title"] == "Calculator"
    assert card["lines"] == ["12.5 × 4 ÷ 3", "= 16.66666667"]
    assert card["seconds"] == 12


def test_the_result_is_json_able():
    out = compute("2^10")
    assert json.loads(json.dumps(out))["result"] == 1024


# --- bad input --------------------------------------------------------------------------------

def test_empty_input():
    assert "expression" in error("")
    assert "expression" in error("   ")
    assert "expression" in error(None)


def test_division_by_zero():
    assert error("1/0") == "Cannot divide by zero."
    assert error("1.0/0") == "Cannot divide by zero."
    assert error("5//0") == "Cannot divide by zero."
    assert error("5%0") == "Cannot divide by zero."
    assert error("0^-1") == "Cannot divide by zero."


def test_nonsense_is_reported_not_raised_as_a_crash():
    assert "couldn't read" in error("3 +")
    assert "couldn't read" in error("((2+3)")
    assert "couldn't read" in error("@@@")


def test_a_list_of_numbers_asks_for_one_calculation():
    assert "one calculation" in error("1,2")


def test_square_root_of_a_negative_number():
    assert error("sqrt(-4)") == "Cannot take the square root of a negative number."


def test_negative_base_to_a_fractional_power():
    assert "fractional power" in error("(-8)^0.5")


def test_wrong_argument_counts():
    assert "takes 1 numbers" in error("sqrt(1,2)")
    assert "takes 1 to 2 numbers" in error("round(1,2,3)")
    assert "takes 1 or more numbers" in error("min()")


def test_long_expressions_are_refused():
    assert "too long" in error("1+" * 200 + "1")


def test_multiline_input_is_refused():
    assert "one line" in error("1+1\n2+2")


# --- anything that is not arithmetic ------------------------------------------------------------

@pytest.mark.parametrize("expression", [
    "__import__('os').system('ls')",
    "open('/etc/passwd')",
    "exec('1')",
    "eval('1')",
    "print(1)",
    "os.system('ls')",
    "(1).__class__",
    "().__class__.__bases__",
    "[1,2][0]",
    "{'a':1}['a']",
    "lambda: 1",
    "1 if 1 else 2",
    "1 and 2",
    "not 1",
    "1 < 2",
    "'abc'*3",
    "f'{1}'",
    "x",
    "x+1",
    "pi.real",
    "sqrt.__call__(4)",
    "globals()",
    "1 | 2",
    "1 & 2",
    "~1",
    "1 << 2",
    "True",
    "None",
    "[x for x in (1,2)]",
    "(y := 2)",
])
def test_dangerous_or_non_arithmetic_input_is_rejected(expression):
    with pytest.raises(CalcError):
        compute(expression)


def test_rejection_happens_without_evaluating_anything(tmp_path):
    """The evaluator is a whitelist walk, so a call to something unknown never reaches Python."""
    victim = tmp_path / "victim.txt"
    victim.write_text("still here")
    with pytest.raises(CalcError, match="don't know the function"):
        compute(f"open({str(victim)!r})")
    assert victim.read_text() == "still here"


def test_unknown_names_name_themselves():
    assert "'foo'" in error("foo")
    assert "'sin'" in error("sin(1)")


def test_no_keyword_arguments_or_star_args():
    with pytest.raises(CalcError):
        compute("round(1, ndigits=2)")
    with pytest.raises(CalcError):
        compute("max(*[1,2])")


# --- limits -------------------------------------------------------------------------------------

def test_huge_exponents_are_refused_before_they_are_computed():
    assert "exponent is too large" in error("2^100000")
    assert "exponent is too large" in error("9^9^9")
    assert "too large" in error("2^999 ^ 999")


def test_a_big_but_reasonable_power_still_works():
    assert value("2^1000") == 2 ** 1000
    assert compute("2^1000")["display"]["lines"][-1].startswith("= 1.071508607e+30")


def test_huge_products_are_refused():
    assert "too large" in error("2^900 * 2^900 * 2^900 * 2^900")


def test_float_overflow_is_reported():
    assert "too large" in error("1e308*10")
    assert "too large" in error("1e400")


def test_round_with_an_absurd_number_of_places():
    assert "sensible" in error("round(1.5, 100000)")
    assert "whole number" in error("round(1.5, 2.5)")


def test_deeply_nested_input_is_handled_not_crashed():
    assert value("(" * 90 + "1" + ")" * 90) == 1  # inside the length limit: still just 1
    assert "too long" in error("(" * 300 + "1" + ")" * 300)  # beyond it: refused before parsing


# --- the MCP tool wrapper ------------------------------------------------------------------------

def test_calculate_tool_returns_the_answer():
    assert calc.calculate("3+3") == {
        "result": 6,
        "expression": "3 + 3",
        "display": {"title": "Calculator", "lines": ["3 + 3", "= 6"], "seconds": 12},
    }


def test_calculate_tool_reports_errors_instead_of_raising():
    out = calc.calculate("1/0")
    assert out["error"] == "Cannot divide by zero."
    assert out["expression"] == "1/0"
    assert out["display"]["lines"] == ["1/0", "Cannot divide by zero."]
    assert "result" not in out


def test_calculate_tool_survives_hostile_input():
    for hostile in ["__import__('os')", "", "))", "x.y.z"]:
        out = calc.calculate(hostile)
        assert "error" in out and out["error"]
