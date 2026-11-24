"""Hand xdist single tests, slowest files first, and keep a shared fixture's users together.

The runner uses ``--dist=loadgroup``. Under ``--dist=loadfile`` a worker took a whole file, so a file with a few
long tests (the harness and qualification files hold tests of 50 to 115 s) ran them one after another on one
worker while the others went idle, and that file's sum was the job's tail. With ``loadgroup`` every test is its
own unit unless it is marked ``xdist_group``, and an idle worker takes the next unit in collection order. Two
things make that work here:

- A test that uses a fixture broader than one test (module, class, package or session scope, defined in this
  suite) is grouped with the other users of that fixture, so the fixture is still built once per run rather than
  once per worker that happens to draw one of its tests.
- Tests of the files that hold the longest single tests (``_LONGEST_TEST_SECONDS``) go first, longest first, then
  the rest of ``_shard._HEAVY_FILE_WEIGHTS`` by weight, so the long tests start in the first wave and the short
  ones fill the gaps after them. A 75 s test that starts last is a 75 s tail. The order is a pure function of
  the collected ids, so every worker computes the same one (xdist requires identical collections).

``--shuffle`` (``_shuffle.py``) runs after this and replaces the order when it is given.
"""

import pytest

from tests_lite._shard import _HEAVY_FILE_WEIGHTS

_SHARED_SCOPES = frozenset({"class", "module", "package", "session"})
_WEIGHTS = dict(_HEAVY_FILE_WEIGHTS)
# The longest single test of each file that has one over 10 s, measured with _HEAVY_FILE_WEIGHTS.
_LONGEST_TEST_SECONDS = {
    "tests_lite/test_provider_harness_qualification.py": 75.0,
    "tests_lite/test_universal_agent_harness.py": 49.0,
    "tests_lite/test_provider_observed_install_qualification.py": 49.0,
    "tests_lite/test_pi_family_turn_oracle.py": 18.0,
    "tests_lite/test_pi_family_helm_fake_lifecycle.py": 13.0,
}


def _start_order(item: pytest.Item) -> tuple[float, float]:
    path = item.nodeid.split("::", 1)[0]
    return (-_LONGEST_TEST_SECONDS.get(path, 0.0), -_WEIGHTS.get(path, 0.0))


def _shared_fixture_group(item: pytest.Item) -> str | None:
    info = getattr(item, "_fixtureinfo", None)
    if info is None:
        return None
    shared = []
    for name, definitions in info.name2fixturedefs.items():
        definition = definitions[-1]
        # Only fixtures this suite defines: pytest's own session fixtures (tmp_path_factory, ...) are cheap.
        if definition.scope in _SHARED_SCOPES and definition.baseid.startswith("tests_lite"):
            module = definition.baseid.rsplit("/", 1)[-1].removesuffix(".py")
            shared.append(f"{module}.{name}")
    return min(shared) if shared else None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # tryfirst: xdist turns the mark into the "@group" suffix of the node id in its own (untagged) hook.
    for item in items:
        if item.get_closest_marker("xdist_group") is None:
            group = _shared_fixture_group(item)
            if group is not None:
                item.add_marker(pytest.mark.xdist_group(group))
    items.sort(key=_start_order)
