#!/usr/bin/env python3
"""
Deployment validation script - run before deploying to catch config issues.

Usage:
    python scripts/validate-deployment.py

Exit codes:
    0: All validations pass
    1: Critical validation failure - deployment should be blocked
"""

import os
import sys
from pathlib import Path

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

def validate_environment():
    """Validate all required environment variables are present and valid."""
    print("🔍 Validating environment configuration...")

    required_vars = [
        ("OPENAI_API_KEY", "OpenAI API key for LLM operations"),
        ("DATABASE_URL", "Database connection string"),
        ("JWT_SECRET", "JWT signing secret (>=16 chars)"),
        ("FERNET_SECRET", "Encryption key for data security"),
    ]

    conditional_vars = [
        ("GOOGLE_CLIENT_ID", "AUTH_DISABLED", "Google OAuth client ID (required if auth enabled)"),
        ("GOOGLE_CLIENT_SECRET", "AUTH_DISABLED", "Google OAuth secret (required if auth enabled)"),
    ]

    errors = []
    warnings = []

    # Check required variables
    for var, description in required_vars:
        value = os.getenv(var)
        if not value:
            errors.append(f"❌ {var}: {description} - MISSING")
        elif var == "JWT_SECRET" and len(value) < 16:
            errors.append(f"❌ {var}: {description} - TOO SHORT ({len(value)} chars)")
        elif var == "DATABASE_URL" and not any(db in value for db in ["postgresql://", "sqlite:///"]):
            warnings.append(f"⚠️  {var}: Unusual database URL format: {value[:50]}...")
        else:
            print(f"✅ {var}: OK")

    # Check conditional variables
    auth_disabled = os.getenv("AUTH_DISABLED", "").lower() in ("1", "true", "yes")
    if not auth_disabled:
        for var, condition, description in conditional_vars:
            if not os.getenv(var):
                errors.append(f"❌ {var}: {description} - MISSING (AUTH_DISABLED={auth_disabled})")
            else:
                print(f"✅ {var}: OK")
    else:
        print("ℹ️  Auth disabled, skipping OAuth validation")

    return errors, warnings

def validate_database_connection():
    """Test database connectivity."""
    print("\n🔍 Validating database connectivity...")

    try:
        from zerg.config import get_settings
        from zerg.database import make_engine
        from sqlalchemy import text

        settings = get_settings()
        if not settings.database_url:
            return ["❌ DATABASE_URL not configured"]

        engine = make_engine(settings.database_url)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            row = result.fetchone()
            if row and row[0] == 1:
                print(f"✅ Database connection: OK ({settings.database_url.split('@')[0]}@***)")
                return []
            else:
                return ["❌ Database query failed"]

    except Exception as e:
        return [f"❌ Database connection failed: {e}"]

def validate_secrets_strength():
    """Check that secrets meet security requirements."""
    print("\n🔍 Validating secret strength...")

    issues = []

    # Check JWT secret
    jwt_secret = os.getenv("JWT_SECRET", "")
    if jwt_secret:
        if jwt_secret == "dev-secret":
            issues.append("❌ JWT_SECRET: Using default dev secret in production")
        elif len(jwt_secret) < 32:
            issues.append(f"⚠️  JWT_SECRET: Consider longer secret (current: {len(jwt_secret)} chars)")
        else:
            print("✅ JWT_SECRET: Strong")

    # Check Fernet secret format
    fernet_secret = os.getenv("FERNET_SECRET", "")
    if fernet_secret:
        if len(fernet_secret) != 44 or not fernet_secret.endswith("="):
            issues.append("❌ FERNET_SECRET: Invalid format (should be 44 chars, base64)")
        else:
            print("✅ FERNET_SECRET: Valid format")

    return issues

def main():
    """Run all deployment validations."""
    print("🚀 Zerg Deployment Validation")
    print("=" * 50)

    all_errors = []
    all_warnings = []

    # Load environment from .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
        print("📁 Loaded environment from .env file")
    except ImportError:
        print("📁 No .env file loading (install python-dotenv)")

    # Run validations
    errors, warnings = validate_environment()
    all_errors.extend(errors)
    all_warnings.extend(warnings)

    db_errors = validate_database_connection()
    all_errors.extend(db_errors)

    secret_issues = validate_secrets_strength()
    all_warnings.extend(secret_issues)

    # Summary
    print("\n" + "=" * 50)
    print("📊 VALIDATION SUMMARY")

    if all_errors:
        print(f"\n🚨 CRITICAL ERRORS ({len(all_errors)}):")
        for error in all_errors:
            print(f"  {error}")
        print("\n❌ DEPLOYMENT BLOCKED - Fix errors above before deploying")
        return 1

    if all_warnings:
        print(f"\n⚠️  WARNINGS ({len(all_warnings)}):")
        for warning in all_warnings:
            print(f"  {warning}")

    print(f"\n✅ ALL VALIDATIONS PASSED")
    print("🚀 Safe to deploy!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
