"""Mathematical computation tools."""

import ast
import logging
import operator
from typing import List
from typing import Union

from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

# Safe operators for math evaluation
SAFE_OPERATORS = {
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

# Safe functions that can be used in expressions
SAFE_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
}


class SafeMathEvaluator(ast.NodeVisitor):
    """Safe mathematical expression evaluator using AST."""

    def visit_BinOp(self, node):
        """Evaluate binary operations."""
        left = self.visit(node.left)
        right = self.visit(node.right)
        op_func = SAFE_OPERATORS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported operation: {type(node.op).__name__}")
        return op_func(left, right)

    def visit_UnaryOp(self, node):
        """Evaluate unary operations."""
        operand = self.visit(node.operand)
        op_func = SAFE_OPERATORS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported operation: {type(node.op).__name__}")
        return op_func(operand)

    def visit_Num(self, node):  # For Python < 3.8 compatibility
        """Visit number node."""
        return node.n

    def visit_Constant(self, node):  # For Python >= 3.8
        """Visit constant node."""
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant type: {type(node.value)}")

    def visit_Name(self, node):
        """Visit name node (for constants like pi, e)."""
        raise ValueError(f"Variables not supported: {node.id}")

    def visit_Call(self, node):
        """Visit function call node."""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in SAFE_FUNCTIONS:
                args = [self.visit(arg) for arg in node.args]
                return SAFE_FUNCTIONS[func_name](*args)
        raise ValueError(f"Unsupported function: {node.func}")

    def visit_List(self, node):
        """Visit list node (for functions like sum, min, max)."""
        return [self.visit(elem) for elem in node.elts]

    def generic_visit(self, node):
        """Fallback for unsupported nodes."""
        raise ValueError(f"Unsupported expression type: {type(node).__name__}")


def math_eval(expression: str) -> Union[int, float]:
    """Safely evaluate a mathematical expression.

    This tool can evaluate basic arithmetic expressions including:
    - Basic operations: +, -, *, /, //, %, **
    - Functions: abs(), round(), min(), max(), sum()
    - Parentheses for grouping

    Args:
        expression: Mathematical expression to evaluate

    Returns:
        The result of the calculation as int or float

    Raises:
        ValueError: If the expression contains unsafe operations

    Examples:
        >>> math_eval("2 + 2")
        4
        >>> math_eval("(10 + 5) * 2")
        30
        >>> math_eval("2 ** 8")
        256
        >>> math_eval("round(10.7)")
        11
        >>> math_eval("max([1, 5, 3, 9, 2])")
        9
    """
    try:
        # Parse the expression into an AST
        tree = ast.parse(expression, mode="eval")

        # Evaluate using our safe evaluator
        evaluator = SafeMathEvaluator()
        result = evaluator.visit(tree.body)

        # Ensure result is numeric
        if not isinstance(result, (int, float)):
            raise ValueError(f"Expression did not evaluate to a number: {result}")

        return result

    except SyntaxError as e:
        logger.error(f"Syntax error in expression '{expression}': {e}")
        raise ValueError(f"Invalid mathematical expression: {e}")
    except ZeroDivisionError:
        logger.error(f"Division by zero in expression: {expression}")
        raise ValueError("Division by zero")
    except Exception as e:
        logger.error(f"Error evaluating expression '{expression}': {e}")
        raise ValueError(f"Error evaluating expression: {e}")


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(func=math_eval, name="math_eval", description="Safely evaluate mathematical expressions"),
]
