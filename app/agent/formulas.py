from __future__ import annotations

import ast
from typing import Any


class FormulaError(ValueError):
    """Raised when a metric formula is invalid or cannot be evaluated."""


_ALLOWED_BINARY_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_UNARY_OPS = (ast.UAdd, ast.USub)


def _parse_formula(formula: str) -> ast.Expression:
    try:
        parsed = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Invalid metric formula: {formula}") from exc
    _validate_node(parsed)
    return parsed


def _validate_node(node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _validate_node(node.body)
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINARY_OPS):
            raise FormulaError("Only +, -, *, and / are allowed in metric formulas.")
        _validate_node(node.left)
        _validate_node(node.right)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARY_OPS):
            raise FormulaError("Only unary +/- are allowed in metric formulas.")
        _validate_node(node.operand)
        return
    if isinstance(node, ast.Name):
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return
    raise FormulaError("Metric formulas may only contain stat names and numbers.")


def extract_formula_variables(formula: str) -> set[str]:
    parsed = _parse_formula(formula)
    return {node.id for node in ast.walk(parsed) if isinstance(node, ast.Name)}


def evaluate_formula(formula: str, values: dict[str, Any]) -> float | None:
    parsed = _parse_formula(formula)
    return _eval_node(parsed.body, values)


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _eval_node(node: ast.AST, values: dict[str, Any]) -> float | None:
    if isinstance(node, ast.Constant):
        # _validate_node guarantees constants are numeric.
        assert isinstance(node.value, (int, float))
        return float(node.value)
    if isinstance(node, ast.Name):
        return _coerce_float(values.get(node.id))
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, values)
        if value is None:
            return None
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, values)
        right = _eval_node(node.right, values)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                return None
            return left / right
    raise FormulaError("Unsupported metric formula node.")


def compile_formula_sql(formula: str, column_map: dict[str, str]) -> str:
    parsed = _parse_formula(formula)
    return _compile_node(parsed.body, column_map)


def _compile_node(node: ast.AST, column_map: dict[str, str]) -> str:
    if isinstance(node, ast.Constant):
        # _validate_node guarantees constants are numeric.
        assert isinstance(node.value, (int, float))
        return str(float(node.value))
    if isinstance(node, ast.Name):
        column = column_map.get(node.id)
        if column is None:
            raise FormulaError(f"Unsupported formula stat: {node.id}")
        return column
    if isinstance(node, ast.UnaryOp):
        operand = _compile_node(node.operand, column_map)
        operator = "+" if isinstance(node.op, ast.UAdd) else "-"
        return f"({operator}{operand})"
    if isinstance(node, ast.BinOp):
        left = _compile_node(node.left, column_map)
        right = _compile_node(node.right, column_map)
        if isinstance(node.op, ast.Div):
            return f"SAFE_DIVIDE(({left}), NULLIF(({right}), 0))"
        operator = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
        }[type(node.op)]
        return f"(({left}) {operator} ({right}))"
    raise FormulaError("Unsupported metric formula node.")
