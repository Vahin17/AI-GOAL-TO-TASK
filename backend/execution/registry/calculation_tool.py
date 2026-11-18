"""
calculation_tool.py — Explicit Deterministic Calculation Executor
================================================================
Implements an AST-safe mathematical calculation executor registered with ToolRegistry.
Executes mathematical expressions deterministically without eval() or arbitrary code execution.
"""

from __future__ import annotations

import ast
import math
import operator
import time
from typing import Any, Dict, Type
from pydantic import BaseModel, Field

from execution.registry.base_tool import BaseTool
from execution.registry.tool_models import ToolCapability, ToolCall, ToolResult
from orchestration.safety_schemas import SafetyContext, ActionType


# Supported safe operators
_SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# Supported safe functions
_SAFE_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "ceil": math.ceil,
    "floor": math.floor,
}


def _eval_ast_node(node: ast.AST) -> Any:
    """Recursively evaluate an AST node strictly within the safe operator set."""
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body)
    elif isinstance(node, ast.Constant):  # Python 3.8+ number/string
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant type: {type(node.value).__name__}")
    elif isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported binary operator: {op_type.__name__}")
        left = _eval_ast_node(node.left)
        right = _eval_ast_node(node.right)
        if op_type is ast.Pow and (right > 1000 or (isinstance(left, (int, float)) and left > 1e10)):
            raise ValueError("Exponentiation magnitude exceeds safe limits.")
        return _SAFE_OPERATORS[op_type](left, right)
    elif isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        operand = _eval_ast_node(node.operand)
        return _SAFE_OPERATORS[op_type](operand)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only direct named functions are permitted in calculation.")
        func_name = node.func.id
        if func_name not in _SAFE_FUNCTIONS:
            raise ValueError(f"Forbidden function call '{func_name}'.")
        args = [_eval_ast_node(arg) for arg in node.args]
        return _SAFE_FUNCTIONS[func_name](*args)
    else:
        raise ValueError(f"Unsupported AST node expression: {type(node).__name__}")


class CalculationInput(BaseModel):
    expression: str = Field(..., description="Mathematical expression to evaluate (e.g. '24 * 60' or 'sqrt(144)')")


class CalculationOutput(BaseModel):
    result: float = Field(..., description="Evaluated numeric result")
    expression: str = Field(..., description="Echo of evaluated expression")


class CalculationTool(BaseTool):
    """
    Deterministically evaluates safe mathematical expressions without eval().
    """
    name: str = "calculator"
    description: str = "Deterministic mathematical and arithmetic calculation engine."
    capability: ToolCapability = ToolCapability.CUSTOM
    input_schema: Type[BaseModel] = CalculationInput
    output_schema: Type[BaseModel] = CalculationOutput

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        raw_expr = str(tool_call.arguments.get("expression", "")).strip()
        if not raw_expr:
            dur = (time.perf_counter() - t0) * 1000.0
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=self.name,
                action_name=tool_call.action_name,
                success=False,
                error="Empty calculation expression.",
                duration_ms=dur,
            )

        try:
            parsed = ast.parse(raw_expr, mode="eval")
            numeric_res = _eval_ast_node(parsed)
            dur = (time.perf_counter() - t0) * 1000.0
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=self.name,
                action_name=tool_call.action_name,
                success=True,
                output_data={"result": float(numeric_res), "expression": raw_expr},
                extracted_text=str(numeric_res),
                duration_ms=dur,
                metadata={"evaluator": "ast_safe_eval", "type": type(numeric_res).__name__},
            )
        except Exception as err:
            dur = (time.perf_counter() - t0) * 1000.0
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=self.name,
                action_name=tool_call.action_name,
                success=False,
                error=f"CALCULATION_ERROR: {err}",
                duration_ms=dur,
            )

    def infer_safety_context(self, task_id: str, tool_call: ToolCall) -> SafetyContext:
        from orchestration.safety_schemas import (
            ActionType,
            AmbiguityLevel,
            DataSensitivity,
            DestructivePotential,
            Environment,
            Reversibility,
            Scope,
            TargetCategory,
        )
        return SafetyContext(
            task_id=task_id,
            action=ActionType.READ,
            target=self.name,
            target_category=TargetCategory.APPLICATION_DATA,
            scope=Scope.APPLICATION,
            environment=Environment.LOCAL,
            reversibility=Reversibility.HIGH,
            destructive_potential=DestructivePotential.NONE,
            privilege_required=False,
            system_critical=False,
            data_sensitivity=DataSensitivity.NONE,
            external_side_effect=False,
            ambiguity=AmbiguityLevel.CLEAR,
            semantic_confidence=1.0,
        )
