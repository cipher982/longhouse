# Safe Expression Evaluator Design Specification

## Overview

This document specifies the design for a safe expression evaluator that will replace the current string-based conditional evaluation system in the workflow engine.

## Library Selection: simpleeval

**Selected Library:** `simpleeval`

**Rationale:**

- Perfect security model for our use case
- Native support for our exact expression patterns
- Simple integration with existing codebase
- Good performance for workflow conditionals
- Comprehensive error handling

## Security Model

### Allowed Operations

**Arithmetic:**

- `+`, `-`, `*`, `/`, `%` (modulo)
- `**` (power, with restrictions)

**Comparisons:**

- `==`, `!=`, `<`, `<=`, `>`, `>=`

**Boolean Logic:**

- `and`, `or`, `not`
- Parentheses for grouping: `(condition1) and (condition2)`

**Literals:**

- Numbers: `85`, `3.14`, `-10`
- Strings: `"completed"`, `'ready'`
- Booleans: `True`, `False`
- None: `None`

### Forbidden Operations

**Security Restrictions:**

- No attribute access (`.` operator blocked)
- No function calls except whitelisted built-ins
- No imports or module access
- No variable assignment
- No list/dict comprehensions
- No lambda expressions

**DoS Protection:**

- Power operation limits (prevent `2**999999`)
- String length limits
- Expression complexity limits

## Expression Examples

### Basic Comparisons

```python
"85 >= 80"                    # True
"status == 'completed'"       # True (with status="completed")
"count != 0"                  # True (with count=5)
```

### Boolean Logic

```python
"(count > 10) and (status == 'ready')"           # Complex condition
"not (error_count > 0)"                          # Negation
"(priority == 'high') or (urgent == True)"       # Or condition
```

### Arithmetic in Conditions

```python
"(total_score / max_score) >= 0.8"               # Percentage check
"(end_time - start_time) > 300"                  # Duration check
```

## Implementation Interface

### Core Evaluator Class

```python
class SafeExpressionEvaluator:
    """Safe expression evaluator using simpleeval."""

    def __init__(self):
        """Initialize with security restrictions."""

    def evaluate(self, expression: str, variables: Dict[str, Any]) -> Any:
        """
        Safely evaluate an expression with given variables.

        Args:
            expression: String expression to evaluate
            variables: Dictionary of variable name -> value mappings

        Returns:
            Evaluation result (bool, int, float, str, etc.)

        Raises:
            ExpressionEvaluationError: For invalid expressions or security violations
        """

    def validate_expression(self, expression: str) -> bool:
        """
        Validate an expression without evaluating it.

        Args:
            expression: String expression to validate

        Returns:
            True if expression is valid and safe

        Raises:
            ExpressionValidationError: For invalid or unsafe expressions
        """
```

### Error Handling

```python
class ExpressionEvaluationError(Exception):
    """Raised when expression evaluation fails."""
    pass

class ExpressionValidationError(Exception):
    """Raised when expression validation fails."""
    pass

class ExpressionSecurityError(Exception):
    """Raised when expression violates security restrictions."""
    pass
```

## Security Configuration

### simpleeval Configuration

```python
ALLOWED_NAMES = {
    # Boolean literals
    'True': True,
    'False': False,
    'None': None,

    # Mathematical functions (if needed)
    'abs': abs,
    'min': min,
    'max': max,

    # String functions (if needed)
    'len': len,
    'str': str,
}

SECURITY_LIMITS = {
    'max_power': 100,           # Limit x**y to prevent DoS
    'max_string_length': 1000,  # Limit string operations
    'max_expression_length': 500,  # Limit expression complexity
}
```

## Integration Points

### Variable Resolution Integration

The evaluator will integrate with the existing variable resolution system:

1. **Variable Resolution** → Resolves `${node.result}` to actual values
2. **Expression Evaluation** → Evaluates resolved expression with real objects

### Conditional Node Integration

```python
# In ConditionalNodeExecutor._evaluate_condition()
def _evaluate_condition(self, condition: str, condition_type: str, node_outputs: Dict[str, Any]) -> bool:
    if condition_type == "expression":
        # New: Use safe evaluator instead of string parsing
        evaluator = SafeExpressionEvaluator()

        # Variable resolution happens first (existing logic)
        resolved_condition = resolve_variables(condition, node_outputs)

        # Then safe evaluation on resolved expression
        result = evaluator.evaluate(resolved_condition, {})

        # Ensure boolean result
        return bool(result)
```

## Performance Considerations

### Optimization Strategies

1. **Parse Once, Reuse:** Cache parsed expressions for repeated evaluations
2. **Variable Pre-resolution:** Resolve variables once, evaluate multiple times
3. **Expression Validation:** Validate expressions at workflow save time, not execution time

### Expected Performance

- **Simple expressions** (`a > b`): < 0.1ms
- **Complex expressions** (`(a > b) and (c == d)`): < 0.5ms
- **Parse overhead**: Amortized across multiple evaluations

## Testing Strategy

### Security Tests

```python
def test_security_restrictions():
    """Test that forbidden operations are blocked."""
    evaluator = SafeExpressionEvaluator()

    # Should raise security errors
    with pytest.raises(ExpressionSecurityError):
        evaluator.evaluate("__import__('os').system('ls')", {})

    with pytest.raises(ExpressionSecurityError):
        evaluator.evaluate("().__class__.__bases__[0].__subclasses__()", {})
```

### Functionality Tests

```python
def test_expression_evaluation():
    """Test correct expression evaluation."""
    evaluator = SafeExpressionEvaluator()

    # Basic comparisons
    assert evaluator.evaluate("85 >= 80", {}) == True
    assert evaluator.evaluate("status == 'completed'", {"status": "completed"}) == True

    # Boolean logic
    assert evaluator.evaluate("(a > 10) and (b == 'ready')", {"a": 15, "b": "ready"}) == True
```

### Performance Tests

```python
def test_expression_performance():
    """Test expression evaluation performance."""
    evaluator = SafeExpressionEvaluator()

    start = time.time()
    for _ in range(1000):
        evaluator.evaluate("(count > 10) and (status == 'ready')",
                          {"count": 15, "status": "ready"})
    duration = time.time() - start

    assert duration < 0.1  # Should complete 1000 evaluations in < 100ms
```

## Migration Strategy

### Backward Compatibility

During transition period, support both evaluation methods:

```python
def _evaluate_condition_with_fallback(self, condition: str, ...):
    try:
        # Try new safe evaluator first
        return self._evaluate_with_safe_evaluator(condition, ...)
    except Exception as e:
        # Fall back to old string-based method with warning
        logger.warning(f"Expression evaluation failed, falling back to legacy method: {e}")
        return self._evaluate_with_legacy_method(condition, ...)
```

### Deprecation Warnings

```python
def _evaluate_with_legacy_method(self, condition: str, ...):
    logger.warning(
        "Legacy expression evaluation is deprecated. "
        f"Please update expression '{condition}' to use new syntax. "
        "See documentation for migration guide."
    )
    # ... legacy implementation
```

## Next Steps

1. Install `simpleeval` dependency
2. Implement `SafeExpressionEvaluator` class
3. Create comprehensive test suite
4. Integrate with existing conditional evaluation
5. Add migration support and warnings

---

**Status:** Design Complete ✅
**Next Task:** Implement SafeExpressionEvaluator class
