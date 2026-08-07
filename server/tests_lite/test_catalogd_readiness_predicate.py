"""Every catalogd readiness probe must decide readiness the same way.

`/api/health` and `/api/health/db` used to accept any ping that returned, while
`/api/readyz` and the supervisor also required a matching schema version and
generation. An incompatible peer owning the socket therefore reported healthy on
the two endpoints that deploy gates and QA scripts read, and 503 on the one they
do not. The probe that lied was the one being trusted.
"""

from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import catalogd_ping_is_compatible
from zerg.services.catalogd_supervisor import CatalogdSupervisor


def _compatible_ping() -> dict:
    return {
        "ready": True,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "schema_generation": CATALOG_SCHEMA_GENERATION,
        "commit_seq": "1",
    }


def test_compatible_ping_is_accepted():
    assert catalogd_ping_is_compatible(_compatible_ping()) is True


def test_ready_alone_is_not_readiness():
    """catalogd sets `ready` true on every successful ping, so it proves only
    that the socket answered."""

    incompatible_version = _compatible_ping() | {"schema_version": CATALOG_SCHEMA_VERSION + 1}
    incompatible_generation = _compatible_ping() | {"schema_generation": -1}

    assert incompatible_version["ready"] is True
    assert catalogd_ping_is_compatible(incompatible_version) is False
    assert catalogd_ping_is_compatible(incompatible_generation) is False


def test_missing_fields_are_not_readiness():
    assert catalogd_ping_is_compatible({}) is False
    assert catalogd_ping_is_compatible({"ready": True}) is False


def test_supervisor_and_probes_share_one_predicate():
    """The supervisor decides whether to adopt a peer's catalogd. If a probe
    disagrees with it, one of them is reporting a state the other refuses to
    work with."""

    incompatible = _compatible_ping() | {"schema_generation": -1}

    assert CatalogdSupervisor._is_compatible(_compatible_ping()) is catalogd_ping_is_compatible(_compatible_ping())
    assert CatalogdSupervisor._is_compatible(incompatible) is catalogd_ping_is_compatible(incompatible)
