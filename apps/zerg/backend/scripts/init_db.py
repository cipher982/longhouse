import sys

from sqlalchemy import inspect

# Add /app to sys.path to allow imports from zerg
sys.path.append("/app")

from zerg.database import Base
from zerg.database import default_engine


def init_db():
    print("🔍 Checking database schema...")
    inspector = inspect(default_engine)

    # Check for 'users' table as a proxy for schema existence
    if not inspector.has_table("users"):
        print("🏗️ Creating initial database schema...")
        try:
            # Import models to ensure they are registered
            from zerg.models.models import Fiche  # noqa: F401
            from zerg.models.models import Run  # noqa: F401
            from zerg.models.models import Thread  # noqa: F401
            from zerg.models.models import ThreadMessage  # noqa: F401
            from zerg.models.models import User  # noqa: F401
            from zerg.models.models import Workflow  # noqa: F401

            Base.metadata.create_all(bind=default_engine)
            print("✅ Database schema created successfully.")
        except Exception as e:
            print(f"❌ Failed to create database schema: {e}")
            sys.exit(1)
    else:
        print("✅ Database schema already exists.")


if __name__ == "__main__":
    init_db()
