"""Seeded test-order shuffle: `pytest --shuffle=SEED`.

The suite claims order independence (one SQLite DB per test, no shared conftest,
no import-time settings). This makes the claim checkable: a shuffled run fails on
the first test that leaned on a neighbour. The order depends only on the seed and
the collected ids, so every xdist worker computes the same one. Unset (the
default), nothing moves.
"""

import random

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--shuffle",
        metavar="SEED",
        default=None,
        help="shuffle collected tests with this integer seed (tests_lite/_shuffle.py)",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    seed = config.getoption("--shuffle")
    if seed is None:
        return
    # Sort first so the result does not depend on collection order, which the
    # shard plugin and file discovery both influence.
    items.sort(key=lambda item: item.nodeid)
    random.Random(int(seed)).shuffle(items)


def pytest_report_header(config):
    seed = config.getoption("--shuffle")
    if seed is not None:
        return f"shuffle seed {seed} (tests_lite/_shuffle.py)"
