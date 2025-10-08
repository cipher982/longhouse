# Database Architecture

This document describes the database architecture and access patterns used in our application.

## Overview

We use SQLAlchemy as our ORM with a factory pattern for creating engines and session factories. This approach offers several benefits:

- **Testability**: Easily swap database connections for testing
- **Dependency Injection**: Explicitly pass database sessions to functions
- **Isolation**: Prevent leaking sessions across requests
- **Centralized Configuration**: Single source of truth for DB connection settings

## Key Components

### Database Factory Functions

Located in `zerg/app/database.py`:

- `make_engine(db_url, **kwargs)`: Creates a SQLAlchemy engine with proper configuration
- `make_sessionmaker(engine)`: Creates a sessionmaker bound to an engine
- `get_session_factory()`: Returns the default sessionmaker for the application
- `initialize_database(engine)`: Creates all tables using the given engine (or default)

### Default Instances

For convenience, we provide default instances:

- `default_engine`: The application's default engine
- `default_session_factory`: The application's default sessionmaker

### FastAPI Dependency

The `get_db()` function is a FastAPI dependency that:

1. Creates a session from a factory (defaulting to `default_session_factory`)
2. Yields the session for request handling
3. Ensures proper cleanup after the request

```python
def get_db(session_factory=None):
    factory = session_factory or default_session_factory
    db = factory()
    try:
        yield db
    finally:
        db.close()
```

## Usage Patterns

### In FastAPI Route Handlers

Use the dependency injection pattern:

```python
from fastapi import Depends
from zerg.app.database import get_db

@router.get("/items")
async def get_items(db: Session = Depends(get_db)):
    items = crud.get_items(db)
    return items
```

### In Services

Inject the session or session factory:

```python
class MyService:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory or default_session_factory
        
    def do_something(self):
        # Create a session when needed
        db = self.session_factory()
        try:
            # Use the session
            result = db.query(MyModel).filter(...).all()
            return result
        finally:
            # Always close the session
            db.close()
```

### In WebSocket Handlers

For WebSocket connections, we use a dedicated function to get fresh sessions:

```python
from zerg.app.websocket.websocket import get_websocket_session

db = get_websocket_session()
try:
    # Handle the WebSocket message
    await process_message(data, db)
finally:
    db.close()
```

## Table Management

Tables are automatically created during application startup via the `initialize_database()` function.

## Agent Scheduling Model

The `Agent` table includes a `schedule` column (nullable string) that stores a CRON expression. If `schedule` is set (not NULL), the agent is considered scheduled and will be picked up by the SchedulerService. If `schedule` is NULL, the agent is not scheduled.

**Note:** The previous `run_on_schedule` boolean flag has been removed. Scheduling is now determined solely by the presence of a non-null `schedule` string.

## Database Reset

For development purposes only, we provide an admin endpoint for resetting the database:

```
POST /admin/reset-database
```

This endpoint:
1. Drops all tables
2. Recreates them with empty data

**IMPORTANT:** This endpoint is only accessible when the `ENVIRONMENT=development` environment variable is set. It will return a 403 Forbidden error in other environments.

## Testing

Tests use an in-memory SQLite database with dependency overrides:

```python
@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=test_engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=test_engine)
```

## Best Practices

1. **Always use dependency injection** for database access
2. **Never import and use global session instances** directly
3. **Create short-lived sessions** and close them properly
4. **Only create tables in application startup** or test setup

By following these patterns, we ensure that our database access is clean, testable, and free from common pitfalls like session leaks. 