"""Run full garbage collections between tests, never inside one.

The suite runs serially in one process and leaves a large heap: about a
million tracked objects by the catalogd tests, which also shed 100k+ cyclic
objects each. Python's automatic full (gen2) collection then lands wherever
allocation crosses its threshold -- deterministically in the same catalogd
tests every run -- and stops every thread for 250-320 ms here and 1.3 s on
the contended CI runner, longer than catalogd's 1 s RPC deadline. Those tests
failed on most pushes for a day with "catalogd deadline exceeded".

So: freeze the import-time heap once, keep the cheap young-generation passes
automatic, and run the full pass ourselves in teardown. Loaded for every run
through pytest.ini, so local and CI behave the same.

Each full pass then freezes what survived it. The survivors are modules
imported lazily by tests, pytest's own reports and items, and module caches:
the serial heap grew from 0.1M to 1.4M such objects, and rescanning them made
every pass slower than the last (5 ms early, 600 ms by the end; 34 s of a
265 s serial run). Freezing only stops the cyclic collector from revisiting
them; reference counting still frees anything they release. Objects frozen
while alive that later become *cyclic* garbage are never reclaimed, so the
pass runs after the test's own fixtures are torn down.
"""

import gc

import pytest

FULL_COLLECT_EVERY_TESTS = 50

_finished = 0


def pytest_collection_finish(session):
    # Modules, models and collected items live for the whole run; a frozen
    # object is never rescanned by later full passes.
    gc.collect()
    gc.freeze()
    young, middle, _ = gc.get_threshold()
    # threshold2 gates automatic full passes; above any reachable count it
    # leaves them to the teardown hook below.
    gc.set_threshold(young, middle, 1_000_000_000)


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item, nextitem):
    global _finished
    _finished += 1
    if _finished % FULL_COLLECT_EVERY_TESTS == 0:
        gc.collect()
        gc.freeze()
