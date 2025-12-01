-- Initialize PostgreSQL database for Zerg AI Agent Platform
-- This runs only on first container startup

-- Create extensions if needed
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Set timezone
SET timezone = 'UTC';

-- Database is already created by POSTGRES_DB env var
-- Tables will be created automatically by SQLAlchemy on first run