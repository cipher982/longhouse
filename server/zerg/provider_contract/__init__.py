"""Provider proof and contract vocabulary shared by the product and the factory verifier.

Modules here import nothing from the rest of `zerg` (scripts/ci/verifier_boundary.py enforces it), so the
verifier can read them without importing the code it judges. Changing a file here moves the verdict like an
oracle does: separation of duties (scripts/ci/separation_of_duties.py) classes this package with the verifier.
"""
