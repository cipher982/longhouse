import sys

from sqlalchemy import inspect

# Add /app to sys.path to allow imports from zerg
sys.path.append("/app")

from zerg.database import configure_database
from zerg.database import get_default_engine
from zerg.database import initialize_database


def init_db():
    print("🔍 Checking database schema...")
    configure_database()
    engine = get_default_engine()
    if engine is None:
        print("❌ DATABASE_URL is not configured.")
        sys.exit(1)
    inspector = inspect(engine)

    # Check for 'users' table as a proxy for schema existence
    if not inspector.has_table("users"):
        print("🏗️ Creating initial database schema...")
        try:
            initialize_database(engine)
            print("✅ Database schema created successfully.")
        except Exception as e:
            print(f"❌ Failed to create database schema: {e}")
            sys.exit(1)
    else:
        print("✅ Database schema already exists.")


if __name__ == "__main__":
    init_db()
