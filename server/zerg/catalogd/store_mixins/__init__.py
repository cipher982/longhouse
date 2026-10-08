"""CatalogStore, split by responsibility.

Each module holds one mixin of CatalogStore's methods; `zerg.catalogd.store`
composes them and remains the only import path. Module-level helpers and
constants stay in `zerg.catalogd.store`, which these modules import from, so
import the class from there, never a mixin directly.
"""
