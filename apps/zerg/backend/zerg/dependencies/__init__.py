"""Dependency helpers (package marker).

Real implementations are placed in dedicated sub-modules so that the package
root remains intentionally empty – this keeps automatic `import *` pollution
to an absolute minimum and avoids hiding complex logic in ``__init__`` files.
"""

# Only keep what *must* be here.
