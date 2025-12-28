#!/usr/bin/env python3
"""
Manual verification script for e2e_schema_manager.py functions.

Tests:
1. get_schema_name() generates correct names
2. recreate_worker_schema() creates fresh schema with tables
3. drop_schema() removes a specific schema
4. drop_all_e2e_schemas() cleans up all test schemas
"""

import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent.parent
sys.path.insert(0, str(backend_path))

from sqlalchemy import text, create_engine
from zerg.e2e_schema_manager import (
    get_schema_name,
    recreate_worker_schema,
    drop_schema,
    drop_all_e2e_schemas,
)
from zerg.config import get_settings

def main():
    print("🧪 E2E Schema Manager Verification")
    print("=" * 50)

    # Get database connection
    settings = get_settings()
    engine = create_engine(settings.database_url)

    # Test 1: Schema name generation
    print("\n1️⃣ Testing get_schema_name()")
    assert get_schema_name("0") == "e2e_worker_0"
    assert get_schema_name("42") == "e2e_worker_42"
    assert get_schema_name("test_123") == "e2e_worker_test_123"  # Underscores allowed
    assert get_schema_name("test-123") == "e2e_worker_test123"  # Sanitized (dash removed)
    print("✅ Schema names generated correctly")

    # Test 2: Create worker schema
    print("\n2️⃣ Testing recreate_worker_schema()")
    schema_name = recreate_worker_schema(engine, "test_verify")
    assert schema_name == "e2e_worker_test_verify"

    # Verify schema exists
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT schema_name FROM information_schema.schemata
            WHERE schema_name = 'e2e_worker_test_verify'
        """))
        assert result.fetchone() is not None

        # Verify tables exist in schema
        result = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'e2e_worker_test_verify'
        """))
        tables = [row[0] for row in result.fetchall()]
        print(f"   Tables created: {len(tables)} ({', '.join(tables[:5])}...)")
        assert len(tables) > 0, "No tables created in schema"

    print("✅ Schema created with tables")

    # Test 3: Drop specific schema
    print("\n3️⃣ Testing drop_schema()")
    drop_schema(engine, "test_verify")

    # Verify schema is gone
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT schema_name FROM information_schema.schemata
            WHERE schema_name = 'e2e_worker_test_verify'
        """))
        assert result.fetchone() is None

    print("✅ Schema dropped successfully")

    # Test 4: Create multiple schemas and drop all
    print("\n4️⃣ Testing drop_all_e2e_schemas()")
    for i in range(3):
        recreate_worker_schema(engine, f"bulk_test_{i}")

    # Verify schemas exist
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT schema_name FROM information_schema.schemata
            WHERE schema_name LIKE 'e2e_worker_bulk_test_%'
        """))
        schemas = [row[0] for row in result.fetchall()]
        print(f"   Created {len(schemas)} test schemas")
        assert len(schemas) == 3

    # Drop all E2E schemas
    dropped_count = drop_all_e2e_schemas(engine)
    print(f"   Dropped {dropped_count} schemas")
    assert dropped_count >= 3

    # Verify cleanup
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT schema_name FROM information_schema.schemata
            WHERE schema_name LIKE 'e2e_worker_%'
        """))
        remaining = [row[0] for row in result.fetchall()]
        assert len(remaining) == 0, f"Schemas still exist: {remaining}"

    print("✅ All E2E schemas dropped")

    print("\n" + "=" * 50)
    print("✅ All verification tests passed!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
