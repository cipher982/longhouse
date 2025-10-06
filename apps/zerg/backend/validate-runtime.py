#!/usr/bin/env python3
"""
Runtime Environment Validation - Master startup check.

Validates all critical environment variables and runtime dependencies
before the application starts. Fails fast with clear error messages.

Usage: python validate-runtime.py
Exit codes:
  0: All validations passed
  1: Critical validation failure - container should not start
"""

import os
import sys
from pathlib import Path


def check_environment():
    """Check all required environment variables."""
    print("🔍 Validating environment variables...")

    errors = []

    # Core application
    if not os.getenv("OPENAI_API_KEY"):
        errors.append("OPENAI_API_KEY is missing")

    if not os.getenv("DATABASE_URL"):
        errors.append("DATABASE_URL is missing")

    # Security/Auth
    jwt_secret = os.getenv("JWT_SECRET", "")
    if not jwt_secret or jwt_secret == "dev-secret" or len(jwt_secret) < 16:
        errors.append("JWT_SECRET is missing, too weak, or using default")

    if not os.getenv("FERNET_SECRET"):
        errors.append("FERNET_SECRET is missing")

    # Auth requirements (if enabled)
    auth_disabled = os.getenv("AUTH_DISABLED", "").lower() in ("1", "true", "yes")
    if not auth_disabled:
        if not os.getenv("GOOGLE_CLIENT_ID"):
            errors.append("GOOGLE_CLIENT_ID is missing (required when auth enabled)")
        if not os.getenv("GOOGLE_CLIENT_SECRET"):
            errors.append("GOOGLE_CLIENT_SECRET is missing (required when auth enabled)")

    if not os.getenv("TRIGGER_SIGNING_SECRET"):
        errors.append("TRIGGER_SIGNING_SECRET is missing")

    return errors


def check_database():
    """Test database connectivity."""
    print("🔍 Testing database connectivity...")

    try:
        from sqlalchemy import text

        from zerg.config import get_settings
        from zerg.database import make_engine

        settings = get_settings()
        engine = make_engine(settings.database_url)

        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            row = result.fetchone()
            if not (row and row[0] == 1):
                return ["Database query test failed"]

        print("✅ Database connection OK")
        return []

    except Exception as e:
        return [f"Database connection failed: {e}"]


def check_critical_paths():
    """Check that critical directories and files exist."""
    print("🔍 Checking critical paths...")

    errors = []

    # Required directories
    static_dir = Path("/app/static")
    if not static_dir.exists():
        try:
            static_dir.mkdir(parents=True)
            print(f"✅ Created static directory: {static_dir}")
        except Exception as e:
            errors.append(f"Cannot create static directory: {e}")

    # Alembic config
    alembic_ini = Path("/app/alembic.ini")
    if not alembic_ini.exists():
        errors.append("alembic.ini not found - migrations will fail")

    return errors


def check_python_dependencies():
    """Verify critical Python modules can be imported."""
    print("🔍 Checking Python dependencies...")

    critical_modules = ["uvicorn", "fastapi", "sqlalchemy", "alembic", "zerg.config", "zerg.main"]

    errors = []
    for module in critical_modules:
        try:
            __import__(module)
        except ImportError as e:
            errors.append(f"Cannot import {module}: {e}")

    return errors


def main():
    """Run all runtime validations."""
    print("🚀 Zerg Runtime Validation")
    print("=" * 40)

    all_errors = []

    # Run all checks
    all_errors.extend(check_environment())
    all_errors.extend(check_database())
    all_errors.extend(check_critical_paths())
    all_errors.extend(check_python_dependencies())

    # Summary
    print("\n" + "=" * 40)
    if all_errors:
        print(f"❌ VALIDATION FAILED ({len(all_errors)} errors):")
        for error in all_errors:
            print(f"   • {error}")
        print("\n💥 CONTAINER STARTUP BLOCKED")
        print("Fix these issues before deploying.")
        return 1
    else:
        print("✅ ALL RUNTIME VALIDATIONS PASSED")
        print("🚀 Safe to start application")
        return 0


if __name__ == "__main__":
    sys.exit(main())
