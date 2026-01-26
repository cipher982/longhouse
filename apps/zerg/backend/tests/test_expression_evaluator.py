"""
Tests for SafeExpressionEvaluator

Comprehensive test suite covering functionality, security, and performance.
"""

import time

import pytest

from zerg.services.expression_evaluator import ExpressionEvaluationError
from zerg.services.expression_evaluator import ExpressionSecurityError
from zerg.services.expression_evaluator import ExpressionValidationError
from zerg.services.expression_evaluator import SafeExpressionEvaluator
from zerg.services.expression_evaluator import evaluate_expression
from zerg.services.expression_evaluator import validate_expression


class TestSafeExpressionEvaluator:
    """Test SafeExpressionEvaluator functionality."""

    def setup_method(self):
        """Set up test evaluator."""
        self.evaluator = SafeExpressionEvaluator()

    # Basic Arithmetic Tests
    def test_basic_arithmetic(self):
        """Test basic arithmetic operations."""
        assert self.evaluator.evaluate("2 + 3") == 5
        assert self.evaluator.evaluate("10 - 4") == 6
        assert self.evaluator.evaluate("3 * 4") == 12
        assert self.evaluator.evaluate("15 / 3") == 5.0
        assert self.evaluator.evaluate("17 % 5") == 2
        assert self.evaluator.evaluate("2 ** 3") == 8

    def test_arithmetic_with_variables(self):
        """Test arithmetic with variables."""
        variables = {"a": 10, "b": 5}

        assert self.evaluator.evaluate("a + b", variables) == 15
        assert self.evaluator.evaluate("a - b", variables) == 5
        assert self.evaluator.evaluate("a * b", variables) == 50
        assert self.evaluator.evaluate("a / b", variables) == 2.0
        assert self.evaluator.evaluate("a % b", variables) == 0
        assert self.evaluator.evaluate("a ** 2", variables) == 100

    # Comparison Tests
    def test_basic_comparisons(self):
        """Test comparison operations."""
        assert self.evaluator.evaluate("5 > 3") is True
        assert self.evaluator.evaluate("5 < 3") is False
        assert self.evaluator.evaluate("5 >= 5") is True
        assert self.evaluator.evaluate("5 <= 4") is False
        assert self.evaluator.evaluate("5 == 5") is True
        assert self.evaluator.evaluate("5 != 3") is True

    def test_comparisons_with_variables(self):
        """Test comparisons with variables."""
        variables = {"score": 85, "threshold": 80}

        assert self.evaluator.evaluate("score >= threshold", variables) is True
        assert self.evaluator.evaluate("score > threshold", variables) is True
        assert self.evaluator.evaluate("score == threshold", variables) is False
        assert self.evaluator.evaluate("score != threshold", variables) is True

    def test_string_comparisons(self):
        """Test string comparison operations."""
        variables = {"status": "completed", "error": ""}

        assert self.evaluator.evaluate("status == 'completed'", variables) is True
        assert self.evaluator.evaluate("status != 'failed'", variables) is True
        assert self.evaluator.evaluate("error == ''", variables) is True
        assert self.evaluator.evaluate("error != 'some error'", variables) is True

    # Boolean Logic Tests
    def test_boolean_logic(self):
        """Test boolean logical operations."""
        variables = {"a": True, "b": False, "c": True}

        assert self.evaluator.evaluate("a and c", variables) is True
        assert self.evaluator.evaluate("a and b", variables) is False
        assert self.evaluator.evaluate("a or b", variables) is True
        assert self.evaluator.evaluate("b or False", variables) is False
        assert self.evaluator.evaluate("not b", variables) is True
        assert self.evaluator.evaluate("not a", variables) is False

    def test_complex_boolean_expressions(self):
        """Test complex boolean expressions with parentheses."""
        variables = {"count": 15, "status": "ready", "urgent": True}

        # Our target use case from the PRD
        assert self.evaluator.evaluate("(count > 10) and (status == 'ready')", variables) is True

        assert self.evaluator.evaluate("(count > 20) or (urgent == True)", variables) is True

        assert self.evaluator.evaluate("not (count < 10)", variables) is True

        assert self.evaluator.evaluate("(count > 10) and (status == 'ready') and urgent", variables) is True

    # Type Preservation Tests
    def test_type_preservation(self):
        """Test that evaluation preserves original types."""
        variables = {"int_val": 85, "float_val": 3.14, "bool_val": True, "str_val": "completed", "none_val": None}

        # Test that types are preserved
        result = self.evaluator.evaluate("int_val", variables)
        assert result == 85 and isinstance(result, int)

        result = self.evaluator.evaluate("float_val", variables)
        assert result == 3.14 and isinstance(result, float)

        result = self.evaluator.evaluate("bool_val", variables)
        assert result is True and isinstance(result, bool)

        result = self.evaluator.evaluate("str_val", variables)
        assert result == "completed" and isinstance(result, str)

        result = self.evaluator.evaluate("none_val", variables)
        assert result is None

    # Built-in Function Tests
    def test_builtin_functions(self):
        """Test allowed built-in functions."""
        variables = {"values": [1, 5, 3], "negative": -10}

        assert self.evaluator.evaluate("abs(-5)") == 5
        assert self.evaluator.evaluate("abs(negative)", variables) == 10
        assert self.evaluator.evaluate("min(1, 5, 3)") == 1
        assert self.evaluator.evaluate("max(1, 5, 3)") == 5

        # String functions
        assert self.evaluator.evaluate("len('hello')") == 5
        assert self.evaluator.evaluate("str(123)") == "123"
        assert self.evaluator.evaluate("int('42')") == 42
        assert self.evaluator.evaluate("float('3.14')") == 3.14
        assert self.evaluator.evaluate("bool(1)") is True

    # Error Handling Tests
    def test_invalid_expressions(self):
        """Test invalid expression handling."""
        with pytest.raises(ExpressionValidationError):
            self.evaluator.evaluate("")

        with pytest.raises(ExpressionValidationError):
            self.evaluator.evaluate(None)

        with pytest.raises(ExpressionValidationError):
            self.evaluator.evaluate("x" * 501)  # Too long

        with pytest.raises(ExpressionEvaluationError):
            self.evaluator.evaluate("invalid syntax +++")

    def test_undefined_variables(self):
        """Test undefined variable handling."""
        with pytest.raises(ExpressionEvaluationError):
            self.evaluator.evaluate("undefined_var > 5")

        with pytest.raises(ExpressionEvaluationError):
            self.evaluator.evaluate("a + undefined_var", {"a": 5})

    def test_division_by_zero(self):
        """Test division by zero handling."""
        with pytest.raises(ExpressionEvaluationError):
            self.evaluator.evaluate("5 / 0")

        with pytest.raises(ExpressionEvaluationError):
            self.evaluator.evaluate("a / b", {"a": 10, "b": 0})

    def test_mathematical_errors(self):
        """Test mathematical error handling."""
        # Overflow protection is handled by simpleeval's MAX_POWER
        with pytest.raises(ExpressionEvaluationError):
            self.evaluator.evaluate("2 ** 200")  # Should hit MAX_POWER limit

    # Security Tests
    def test_security_restrictions(self):
        """Test that dangerous operations are blocked."""

        # Attribute access should be blocked
        with pytest.raises(ExpressionSecurityError):
            self.evaluator.evaluate("''.__class__")

        with pytest.raises(ExpressionSecurityError):
            self.evaluator.evaluate("().__class__.__bases__[0]")

        # Import attempts should be blocked (though they won't parse anyway)
        with pytest.raises((ExpressionValidationError, ExpressionSecurityError)):
            self.evaluator.evaluate("__import__('os')")

    def test_no_dangerous_builtins(self):
        """Test that dangerous built-ins are not available."""
        dangerous_names = ["exec", "eval", "compile", "open", "__import__"]

        for name in dangerous_names:
            with pytest.raises(ExpressionSecurityError):
                self.evaluator.evaluate(f"{name}('test')")

    # Expression Validation Tests
    def test_expression_validation(self):
        """Test expression validation without evaluation."""
        # Valid expressions
        assert self.evaluator.validate_expression("a > b") is True
        assert self.evaluator.validate_expression("(x + y) * z") is True
        assert self.evaluator.validate_expression("status == 'completed'") is True

        # Invalid expressions should raise ValidationError
        with pytest.raises(ExpressionValidationError):
            self.evaluator.validate_expression("")

        with pytest.raises(ExpressionValidationError):
            self.evaluator.validate_expression("invalid syntax +++")

        # Security violations should raise SecurityError
        with pytest.raises(ExpressionSecurityError):
            self.evaluator.validate_expression("a.__class__")

    # Performance Tests
    def test_performance(self):
        """Test expression evaluation performance."""
        variables = {"a": 15, "b": 10, "status": "ready"}
        expression = "(a > b) and (status == 'ready')"

        # Warm up
        for _ in range(10):
            self.evaluator.evaluate(expression, variables)

        # Time 1000 evaluations
        start_time = time.time()
        for _ in range(1000):
            result = self.evaluator.evaluate(expression, variables)
            assert result is True
        duration = time.time() - start_time

        # Should complete 1000 evaluations in less than 100ms
        assert duration < 0.1, f"Performance test failed: {duration:.3f}s for 1000 evaluations"

        # Average should be less than 0.5ms per evaluation
        avg_time = duration / 1000
        assert avg_time < 0.0005, f"Average evaluation time too high: {avg_time:.3f}ms"


class TestConvenienceFunctions:
    """Test convenience functions."""

    def test_global_evaluate_function(self):
        """Test global evaluate_expression function."""
        assert evaluate_expression("5 > 3") is True
        assert evaluate_expression("a + b", {"a": 2, "b": 3}) == 5

        with pytest.raises(ExpressionEvaluationError):
            evaluate_expression("undefined_var")

    def test_global_validate_function(self):
        """Test global validate_expression function."""
        assert validate_expression("a > b") is True

        with pytest.raises(ExpressionValidationError):
            validate_expression("invalid syntax")


class TestRealWorldUseCases:
    """Test real-world use cases from the PRD."""

    def setup_method(self):
        """Set up test evaluator."""
        self.evaluator = SafeExpressionEvaluator()

    def test_tool_result_comparison(self):
        """Test tool result comparison (our failing test case)."""
        # This should work with our new system
        # Note: In practice, variable resolution will handle ${tool.result.score} → 85
        assert self.evaluator.evaluate("score >= 80", {"score": 85}) is True
        assert self.evaluator.evaluate("score < 100", {"score": 85}) is True

    def test_fiche_status_check(self):
        """Test fiche status checking."""
        variables = {"fiche_status": "completed", "message_count": 3, "success": True}

        assert self.evaluator.evaluate("fiche_status == 'completed'", variables) is True
        assert self.evaluator.evaluate("message_count > 0", variables) is True
        assert self.evaluator.evaluate("success and (message_count > 0)", variables) is True

    def test_conditional_workflow_logic(self):
        """Test complex conditional workflow logic."""
        variables = {"score": 85, "threshold": 80, "priority": "high", "error_count": 0, "processing_time": 1200}

        # High score and no errors
        assert self.evaluator.evaluate("(score >= threshold) and (error_count == 0)", variables) is True

        # Priority routing
        assert self.evaluator.evaluate("(priority == 'high') or (score >= 90)", variables) is True

        # Performance check
        assert self.evaluator.evaluate("processing_time < 2000", variables) is True

        # Complex business logic
        assert (
            self.evaluator.evaluate(
                "((score >= threshold) and (error_count == 0)) and (processing_time < 2000)", variables
            )
            is True
        )

    def test_data_validation_scenarios(self):
        """Test data validation scenarios."""
        # Email validation scenario
        variables = {"email_count": 15, "spam_count": 2, "processing_status": "completed"}

        assert self.evaluator.evaluate("(email_count > 10) and (spam_count < 5)", variables) is True

        # Quality check scenario
        variables = {"accuracy": 0.95, "completeness": 0.88, "data_quality_threshold": 0.85}

        assert (
            self.evaluator.evaluate(
                "(accuracy >= data_quality_threshold) and (completeness >= data_quality_threshold)", variables
            )
            is True
        )

    def test_percentage_calculations(self):
        """Test percentage-based conditions."""
        variables = {"correct_answers": 17, "total_questions": 20, "passing_score": 0.8}

        # Calculate percentage and compare
        assert self.evaluator.evaluate("(correct_answers / total_questions) >= passing_score", variables) is True

        # More complex percentage logic
        variables = {"processed": 850, "total": 1000, "min_completion": 0.8, "target_completion": 0.9}

        assert self.evaluator.evaluate("(processed / total) >= min_completion", variables) is True

        # Check if we're close to target
        assert self.evaluator.evaluate("(processed / total) >= target_completion", variables) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
