"""The one request-scoped "no database session" dependency.

Routes that read and write exclusively through catalogd never open a
SQLAlchemy session on the request path, but FastAPI still needs a callable to
resolve their ``db`` parameter against. Every such route resolves it here.

Legacy/test processes that still own an archive engine keep ``get_db`` as the
exact dependency callable so ``dependency_overrides[get_db]`` binds; those
modules select between the two with :func:`catalog_db_dependency`.
"""

from __future__ import annotations

from collections.abc import Iterator


def no_request_db() -> Iterator[None]:
    """Yield no request-scoped database session."""

    yield None


__all__ = ["no_request_db"]
