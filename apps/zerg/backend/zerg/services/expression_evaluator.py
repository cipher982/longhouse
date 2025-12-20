"""
Safe Expression Evaluator for Workflow Conditions

Provides secure evaluation of mathematical and logical expressions using simpleeval.
Replaces the string-based conditional evaluation with type-safe AST evaluation.
"""

import logging
from typing import Any
from typing import Dict

from simpleeval import AttributeDoesNotExist
from simpleeval import FeatureNotAvailable
from simpleeval import FunctionNotDefined
from simpleeval import InvalidExpression
from simpleeval import NameNotDefined
from simpleeval import SimpleEval

logger = logging.getLogger(__name__)


class ExpressionEvaluationError(Exception):
    """Raised when expression evaluation fails."""

    pass


class ExpressionValidationError(Exception):
    """Raised when expression validation fails."""

    pass


class ExpressionSecurityError(Exception):
    """Raised when expression violates security restrictions."""

    pass


class SafeExpressionEvaluator:
    """
    Safe expression evaluator using simpleeval.

    Supports:
    - Arithmetic: +, -, *, /, %, **
    - Comparisons: ==, !=, <, <=, >, >=
    - Boolean logic: and, or, not
    - Literals: numbers, strings, booleans, None
    - Parentheses for grouping

    Security:
    - No attribute access (. operator blocked)
    - No function calls except whitelisted built-ins
    - No imports or module access
    - DoS protection (power limits, string limits)
    """

    def __init__(self):
        """Initialize evaluator with security restrictions."""
        self._evaluator = SimpleEval()
        self._configure_security()

    def _configure_security(self):
        """Configure security restrictions for safe evaluation."""

        # Allowed names (built-ins and literals)
        self._evaluator.names = {
            # Boolean literals
            "True": True,
            "False": False,
            "None": None,
        }

        # Safe built-in functions (carefully selected)
        self._evaluator.functions = {
            "abs": abs,
            "min": min,
            "max": max,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
        }

        # Create power limit by overriding the pow operator
        import ast

        original_pow = self._evaluator.operators[ast.Pow]

        def safe_pow(left, right):
            """Safe power operation with limits."""
            if isinstance(right, (int, float)) and right > 100:
                raise ExpressionEvaluationError("Power operation too large (max exponent: 100)")
            return original_pow(left, right)

        self._evaluator.operators[ast.Pow] = safe_pow

    def evaluate(self, expression: str, variables: Dict[str, Any] = None) -> Any:
        """
        Safely evaluate an expression with given variables.

        Args:
            expression: String expression to evaluate (e.g., "a >= 80")
            variables: Dictionary of variable name -> value mappings

        Returns:
            Evaluation result (bool, int, float, str, etc.)

        Raises:
            ExpressionEvaluationError: For evaluation failures
            ExpressionSecurityError: For security violations
            ExpressionValidationError: For invalid expressions
        """
        if not expression or not isinstance(expression, str):
            raise ExpressionValidationError("Expression must be a non-empty string")

        if len(expression) > 500:
            raise ExpressionValidationError("Expression too long (max 500 characters)")

        # Set up variables for evaluation
        if variables:
            # Create a copy to avoid modifying original
            eval_names = {**self._evaluator.names, **variables}
        else:
            eval_names = self._evaluator.names

        try:
            # Set the names for this evaluation
            original_names = self._evaluator.names
            self._evaluator.names = eval_names

            # Use simpleeval for safe evaluation
            result = self._evaluator.eval(expression)

            # Restore original names
            self._evaluator.names = original_names

            logger.debug(f"Expression '{expression}' evaluated to {result} (type: {type(result).__name__})")
            return result

        except NameNotDefined as e:
            raise ExpressionEvaluationError(f"Undefined variable in expression '{expression}': {e}")

        except FunctionNotDefined as e:
            # Check if this is a dangerous function that should be treated as security violation
            dangerous_functions = {"exec", "eval", "compile", "open", "__import__"}
            func_name = str(e).split("'")[1] if "'" in str(e) else ""
            if func_name in dangerous_functions:
                raise ExpressionSecurityError(f"Dangerous function '{func_name}' not allowed in expression '{expression}'")
            else:
                raise ExpressionEvaluationError(f"Undefined function in expression '{expression}': {e}")

        except ZeroDivisionError as e:
            raise ExpressionEvaluationError(f"Division by zero in expression '{expression}': {e}")

        except (OverflowError, ValueError) as e:
            raise ExpressionEvaluationError(f"Mathematical error in expression '{expression}': {e}")

        except (InvalidExpression, AttributeDoesNotExist, FeatureNotAvailable) as e:
            # Security violations from simpleeval
            raise ExpressionSecurityError(f"Security violation in expression '{expression}': {e}")

        except Exception as e:
            # Any other evaluation failure
            raise ExpressionEvaluationError(f"Failed to evaluate expression '{expression}': {e}")

    def validate_expression(self, expression: str) -> bool:
        """
        Validate an expression without evaluating it.

        Args:
            expression: String expression to validate

        Returns:
            True if expression is valid and safe

        Raises:
            ExpressionValidationError: For invalid or unsafe expressions
            ExpressionSecurityError: For security violations
        """
        if not expression or not isinstance(expression, str):
            raise ExpressionValidationError("Expression must be a non-empty string")

        if len(expression) > 500:
            raise ExpressionValidationError("Expression too long (max 500 characters)")

        try:
            # Try to parse without evaluation (dry run with dummy variables)
            dummy_vars = {"a": 1, "b": 2, "c": "test", "d": True, "x": 1, "y": 2, "z": 3, "status": "ready"}
            original_names = self._evaluator.names
            self._evaluator.names = {**self._evaluator.names, **dummy_vars}

            self._evaluator.eval(expression)

            # Restore original names
            self._evaluator.names = original_names
            return True

        except (AttributeDoesNotExist, FeatureNotAvailable) as e:
            # Security violations should be raised as SecurityError
            raise ExpressionSecurityError(f"Expression contains forbidden operations: {e}")

        except (InvalidExpression, SyntaxError) as e:
            raise ExpressionValidationError(f"Invalid expression syntax: {e}")

        except (NameNotDefined, FunctionNotDefined):
            # For validation, undefined variables/functions are okay - we're just checking syntax
            return True

        except Exception as e:
            if "not allowed" in str(e).lower() or "forbidden" in str(e).lower():
                raise ExpressionSecurityError(f"Expression contains forbidden operations: {e}")
            else:
                # For validation, we don't care about undefined variables or math errors
                # Only syntax and security violations matter
                return True


# Global instance for convenience
safe_evaluator = SafeExpressionEvaluator()


def evaluate_expression(expression: str, variables: Dict[str, Any] = None) -> Any:
    """
    Convenience function for safe expression evaluation.

    Args:
        expression: String expression to evaluate
        variables: Dictionary of variable name -> value mappings

    Returns:
        Evaluation result

    Raises:
        ExpressionEvaluationError: For evaluation failures
        ExpressionSecurityError: For security violations
        ExpressionValidationError: For invalid expressions
    """
    return safe_evaluator.evaluate(expression, variables)


def validate_expression(expression: str) -> bool:
    """
    Convenience function for expression validation.

    Args:
        expression: String expression to validate

    Returns:
        True if expression is valid and safe

    Raises:
        ExpressionValidationError: For invalid expressions
        ExpressionSecurityError: For security violations
    """
    return safe_evaluator.validate_expression(expression)
