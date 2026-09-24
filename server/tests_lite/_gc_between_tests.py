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
"""

import gc

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


def pytest_runtest_teardown(item, nextitem):
    global _finished
    _finished += 1
    if _finished % FULL_COLLECT_EVERY_TESTS == 0:
        gc.collect()
