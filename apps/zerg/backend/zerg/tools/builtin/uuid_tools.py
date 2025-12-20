"""UUID generation tools."""

import uuid
from typing import List
from typing import Optional

from langchain_core.tools import StructuredTool


def generate_uuid(version: Optional[int] = 4, namespace: Optional[str] = None, name: Optional[str] = None) -> str:
    """Generate a UUID.

    Args:
        version: UUID version to generate (1, 3, 4, or 5). Default is 4 (random).
            - Version 1: Based on timestamp and MAC address
            - Version 3: Based on MD5 hash of namespace/name
            - Version 4: Random UUID (default)
            - Version 5: Based on SHA-1 hash of namespace/name
        namespace: For versions 3 and 5, the namespace UUID (as string).
            Can be "dns", "url", "oid", "x500", or a custom UUID string.
        name: For versions 3 and 5, the name to hash within the namespace.

    Returns:
        A UUID string in standard format (e.g., "550e8400-e29b-41d4-a716-446655440000")

    Raises:
        ValueError: If invalid version or missing required parameters

    Examples:
        >>> generate_uuid()  # Random UUID v4
        "550e8400-e29b-41d4-a716-446655440000"

        >>> generate_uuid(version=1)  # Time-based UUID
        "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

        >>> generate_uuid(version=5, namespace="dns", name="example.com")
        "cfbff0d1-9375-5685-968c-48ce8b15ae17"
    """
    if version == 1:
        # UUID based on timestamp and MAC address
        return str(uuid.uuid1())

    elif version == 3:
        # UUID based on MD5 hash
        if not namespace or not name:
            raise ValueError("Version 3 UUIDs require both namespace and name parameters")

        # Convert namespace string to UUID object
        ns_uuid = _get_namespace_uuid(namespace)
        return str(uuid.uuid3(ns_uuid, name))

    elif version == 4:
        # Random UUID (default)
        return str(uuid.uuid4())

    elif version == 5:
        # UUID based on SHA-1 hash
        if not namespace or not name:
            raise ValueError("Version 5 UUIDs require both namespace and name parameters")

        # Convert namespace string to UUID object
        ns_uuid = _get_namespace_uuid(namespace)
        return str(uuid.uuid5(ns_uuid, name))

    else:
        raise ValueError(f"Invalid UUID version: {version}. Must be 1, 3, 4, or 5")


def _get_namespace_uuid(namespace: str) -> uuid.UUID:
    """Convert namespace string to UUID object.

    Args:
        namespace: Either a predefined namespace name or a UUID string

    Returns:
        UUID object for the namespace

    Raises:
        ValueError: If namespace is invalid
    """
    # Predefined namespaces
    predefined = {
        "dns": uuid.NAMESPACE_DNS,
        "url": uuid.NAMESPACE_URL,
        "oid": uuid.NAMESPACE_OID,
        "x500": uuid.NAMESPACE_X500,
    }

    if namespace.lower() in predefined:
        return predefined[namespace.lower()]

    # Try to parse as UUID
    try:
        return uuid.UUID(namespace)
    except ValueError:
        raise ValueError(f"Invalid namespace '{namespace}'. Must be one of {list(predefined.keys())} or a valid UUID string")


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(func=generate_uuid, name="generate_uuid", description="Generate a universally unique identifier (UUID)"),
]
