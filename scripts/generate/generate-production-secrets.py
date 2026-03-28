#!/usr/bin/env python3
"""Generate secure secrets for production deployment."""

import secrets
from cryptography.fernet import Fernet

def generate_secrets():
    """Generate all required production secrets."""
    print("🔐 Generating Production Secrets")
    print("=" * 50)

    print("\n# Database")
    print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}")

    print("\n# Security & Auth")
    print(f"JWT_SECRET={secrets.token_urlsafe(32)}")
    print(f"FERNET_SECRET={Fernet.generate_key().decode()}")
    print(f"TRIGGER_SIGNING_SECRET={secrets.token_hex(32)}")

    print("\n# Copy these values to your Coolify environment variables")
    print("# or update .env.production file")

if __name__ == "__main__":
    generate_secrets()
