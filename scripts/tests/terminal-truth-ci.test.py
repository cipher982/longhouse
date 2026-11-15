#!/usr/bin/env python3
"""The terminal-truth CI lane: pinned provider versions, the verdict script, and the workflow's shape."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINS = ROOT / "scripts" / "ci" / "terminal-truth-providers.toml"
SUMMARY = ROOT / "scripts" / "ci" / "terminal_truth_summary.py"
WORKFLOW = ROOT / ".github" / "workflows" / "terminal-truth.yml"
BUDGET = ROOT / "schemas" / "terminal-truth-budget.json"
PROVIDERS = {"codex", "claude", "opencode", "pi", "omp", "antigravity"}


def row(cell: str, outcome: str = "pass", median: float | None = 0.2, requests: int = 1, stage: str | None = None) -> dict:
    provider, mode = cell.split("/")
    return {
        "cell": cell, "provider": provider, "mode": mode, "status": "measured", "outcome": outcome, "first_failed_stage": stage,
        "reason": "", "stages": {"preflight": {"ok": True, "evidence": {"model_requests_served": requests}}},
        "lanes": [{"lane": "workspace stream (SSE)", "median_delay_s": median}],
    }


class PinsTests(unittest.TestCase):
    def test_every_provider_the_lane_installs_has_an_exact_pin(self):
        pins = tomllib.loads(PINS.read_text())
        self.assertEqual(set(pins), PROVIDERS)
        for name, pin in pins.items():
            self.assertRegex(pin["version"], r"^\d+\.\d+\.\d+$", name)
            self.assertIn(pin["version"], pin["reports"], name)
            self.assertIn(pin["kind"], {"npm", "omp", "agy"}, name)
            if pin["kind"] == "npm":
                self.assertTrue(pin["package"], name)
            if pin["kind"] == "agy":
                self.assertRegex(pin["sha512"], r"^[0-9a-f]{128}$")
                self.assertIn(pin["version"], pin["url"])

    def test_the_workflow_installs_and_verifies_through_the_pins_and_never_the_unpinned_installers(self):
        text = WORKFLOW.read_text()
        self.assertIn("terminal_truth_providers.py install", text)
        self.assertIn("terminal_truth_providers.py verify", text)
        for forbidden in ("npm install", "https://omp.sh/install", "install.sh"):
            self.assertNotIn(forbidden, text)

    def test_install_refuses_an_unpinned_provider(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/ci/terminal_truth_providers.py"), "verify", "--providers", "cursor"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no pinned version for: cursor", result.stderr)

    def test_a_cached_install_is_unpacked_instead_of_installed_and_new_pins_or_paths_miss(self):
        sys.path.insert(0, str(ROOT / "scripts" / "ci"))
        import terminal_truth_providers as providers

        pins = providers.load_pins()
        names = list(pins)
        installs: list[Path] = []

        def fake_install(_pins, _names, root: Path, home: Path) -> None:
            installs.append(root)
            (root / "node_modules" / ".bin").mkdir(parents=True)
            (root / "node_modules" / ".bin" / "codex").write_text("#!/bin/sh\n")
            (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
            (home / ".local" / "bin" / "omp").write_text("binary")
            (home / ".local" / "bin" / "agy").symlink_to("omp")

        real_install, providers.install = providers.install, fake_install
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                root, home, cache = base / "fixture" / "providers", base / "fixture" / "home", base / "cache"
                self.assertEqual(providers.install_cached(pins, names, root, home, cache), "miss")
                self.assertEqual(len(list(cache.glob("providers-*.tar"))), 1)
                import shutil

                shutil.rmtree(base / "fixture")
                self.assertEqual(providers.install_cached(pins, names, root, home, cache), "hit")
                self.assertEqual(len(installs), 1)
                self.assertEqual((home / ".local" / "bin" / "omp").read_text(), "binary")
                self.assertTrue((home / ".local" / "bin" / "agy").is_symlink())
                self.assertTrue((root / "node_modules" / ".bin" / "codex").is_file())
                # Another path is another install: installers may write their own absolute paths.
                self.assertNotEqual(providers.cache_key(names, root, home), providers.cache_key(names, base / "elsewhere", home))
                # A narrowed run misses and leaves the whole set's archive alone.
                self.assertEqual(providers.install_cached(pins, names[:1], base / "n" / "providers", base / "n" / "home", cache), "miss")
                self.assertEqual(len(list(cache.glob("providers-*.tar"))), 2)
                # An archive that cannot be unpacked is a miss, not a failed job.
                archive = cache / f"providers-{providers.cache_key(names, root, home)}.tar"
                archive.write_text("not a tar")
                shutil.rmtree(base / "fixture")
                self.assertEqual(providers.install_cached(pins, names, root, home, cache), "miss")
                self.assertEqual(len(installs), 3)
        finally:
            providers.install = real_install


class WorkflowTests(unittest.TestCase):
    def test_push_paths_are_the_one_list_and_cover_everything_the_result_is_made_from(self):
        text = WORKFLOW.read_text()
        block = text.split("    paths:\n", 1)[1].split("  schedule:", 1)[0]
        paths = re.findall(r'^\s+- "([^"]+)"$', block, re.M)
        for needed in (
            # The engine and what it embeds (the engine cache key hashes exactly these three), the host and harness.
            "engine/**", "config/**", "schemas/**", "server/zerg/**", "server/uv.lock", "scripts/build/**",
            "scripts/ci/terminal-truth-*", "scripts/ci/terminal_truth_*.py", ".github/workflows/terminal-truth.yml",
            "web/src/features/session/useSessionWorkspace.ts", "ios/Sources/Shared/Streams/SessionWorkspaceStream.swift",
        ):  # fmt: skip
            self.assertIn(needed, paths)

    def test_the_workflow_publishes_one_stable_status_and_uses_no_secret_but_the_receipt_store(self):
        text = WORKFLOW.read_text()
        self.assertEqual(len(re.findall(r"terminal_truth_status\.py", text)), 2)  # pending, result
        self.assertNotIn("gh api", text)  # the Linux runner has no gh
        secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
        self.assertTrue(all(name.startswith("CI_RECEIPTS_S3_") for name in secrets), secrets)
        self.assertNotIn("upload-artifact", text)
        self.assertNotIn("--live", text)  # never a real model

    def test_every_run_is_the_one_command_and_a_push_and_the_nightly_are_the_whole_gate(self):
        text = WORKFLOW.read_text()
        runs = [line for line in text.splitlines() if "make -s terminal-truth-all" in line]
        self.assertEqual(len(runs), 2, runs)  # the run, and the extra reply passes of a hand-started run
        gate, extra = runs
        for part in ("--engine $TT_ENGINE", "--longhouse-cli $TT_CLI", "--out-dir $TT_OUT ", "--jobs $TT_JOBS", "--fault-engine $TT_FAULT_ENGINE", "--fault-longhouse-cli $TT_FAULT_CLI",
                     "--embed-model-cache $TT_CACHE/embedding-model"):
            self.assertIn(part, gate)
        self.assertTrue(gate.rstrip().endswith('$TT_NARROW_ARGS$TT_CHECK_ARGS$TT_RERUN_ARGS" \\'), gate)
        self.assertIn("--no-mutations --no-rerun --pass-index $i", extra)
        # Nothing narrows a push or the nightly: every narrowing variable comes from a dispatch input.
        for name in ("TT_PROVIDER", "TT_MODE", "TT_TOKENS", "TT_RATE", "TT_REPEATS", "TT_MUTATION_NAMES"):
            line = next(line for line in text.splitlines() if line.strip().startswith(f"{name}:"))
            self.assertRegex(line, r"\$\{\{ inputs\.[a-z_]+ \|\| '[a-z0-9]*' \}\}$", name)
        self.assertIn("TT_MUTATIONS: ${{ (github.event_name == 'workflow_dispatch' && !inputs.mutations) && '0' || '1' }}", text)
        # Verdict requires the whole gate of every run that is not a narrowed hand-started one.
        self.assertIn('if [[ "$TT_EVENT" != workflow_dispatch || "$TT_PUBLISH" == 1 ]]; then whole=(--gate-profile); fi', text)
        self.assertIn('python3 scripts/ci/terminal_truth_summary.py "$TT_OUT" --repeats "$TT_REPEATS" "${whole[@]}" "${previous[@]}"', text)
        # The run's exit status and the verdict script must both be good.
        self.assertIn('if [[ "$rc" != 0 || "${TT_RC:-1}" != 0 ]]; then', text)

    def test_the_nightly_never_runs_a_row_again_and_is_the_only_schedule(self):
        text = WORKFLOW.read_text()
        crons = re.findall(r'^\s+- cron: "([^"]+)"$', text, re.M)
        self.assertEqual(len(crons), 1, crons)  # the weekly mutation run is gone: every run has the mutation tests
        self.assertEqual(crons[0].split()[2:], ["*", "*", "*"])
        self.assertNotIn("github.event.schedule", text)
        self.assertIn(
            "TT_RERUN: ${{ (github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && !inputs.rerun)) && '0' || '1' }}", text
        )
        self.assertIn('if [[ "$TT_RERUN" != 1 ]]; then rerun=" --no-rerun"; fi', text)

    def test_one_queue_a_short_limit_and_scratch_on_the_ram_backed_work_volume(self):
        text = WORKFLOW.read_text()
        # One queue for main (push, nightly, a dispatch on main): two runs on one host disk made a pass fail
        # (2026-11-12). Every other ref has a queue of its own, so a branch dispatch cannot take main's pending slot
        # (it cancelled the pending run of 430ea7f3c on 2026-11-15). Nothing is ever cancelled once running.
        group = next(line for line in text.splitlines() if line.strip().startswith("group:"))
        self.assertEqual(
            group.strip(),
            "group: terminal-truth-${{ github.repository }}-${{ github.ref == 'refs/heads/main' && 'main' || github.ref }}",
        )
        self.assertIn("cancel-in-progress: false", text)
        limit = next(line for line in text.splitlines() if line.strip().startswith("timeout-minutes:"))
        self.assertIn("github.event_name == 'workflow_dispatch' && 240 || 45", limit)
        # /tmp in the pod is the host's shared disk; the work volume is RAM-backed. Short, for unix socket paths.
        scratch = next(line for line in text.splitlines() if line.strip().startswith("TT_SCRATCH_ROOT:"))
        self.assertEqual(scratch.strip(), "TT_SCRATCH_ROOT: /home/runner/_work/tt")
        self.assertNotIn("/tmp/agents", text)

    def test_the_engine_the_mutated_engines_and_the_provider_install_come_from_the_cache_when_unchanged(self):
        text = WORKFLOW.read_text()
        self.assertIn('make -s terminal-truth-engine ARGS="--out $RUNNER_TEMP/terminal-truth-engine --cache $TT_CACHE/engine"', text)
        self.assertIn('--cache "$TT_CACHE/providers"', text)
        self.assertIn("cache=/ci-cache/terminal-truth", text)
        # The provider archive is keyed by its path, so the path is fixed.
        self.assertIn('fixture_root="$RUNNER_TEMP/terminal-truth-fixture"', text)
        self.assertNotIn("mktemp", text)

    def test_only_the_scratch_of_shards_with_a_wrong_row_is_kept_and_never_a_provider_home(self):
        text = WORKFLOW.read_text()
        self.assertIn('.get("kept_scratch", [])', text)
        self.assertIn("tar --exclude='*/h' --exclude='*/host-home' --exclude='*.db' --exclude='*.db-*'", text)
        self.assertIn('tar -czf "$TT_OUT/shards.tgz" -C "$TT_OUT" shards', text)

    def test_the_collect_step_keeps_the_results_of_a_run_that_kept_no_scratch(self):
        """The first push run with every row right the first time (2026-11-13) had nothing kept, the step's grep matched
        nothing, and under bash -e the step failed before the receipt was stored: a red job on a passing gate."""

        text = WORKFLOW.read_text()
        start = text.index("      - name: Collect recordings and keep the results\n")
        body = text[text.index("        run: |\n", start) + len("        run: |\n") : text.index("\n      - name:", start + 1)]
        script = "\n".join(line[10:] for line in body.splitlines())
        for kept in ([], ["{root}/a1001"]):
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                out, scratch = work / "out", work / "scratch"
                (out / "shards").mkdir(parents=True)
                (scratch / "a1001" / "0f3c" / "h").mkdir(parents=True)
                (scratch / "a1001" / "0f3c" / "runtime-host.log").write_text("log\n")
                (out / "fleet.json").write_text(json.dumps({"kept_scratch": [k.format(root=scratch) for k in kept]}))
                (work / "scripts" / "ops").mkdir(parents=True)
                (work / "scripts" / "ops" / "ci_receipts.py").write_text(
                    "import sys, pathlib\npathlib.Path('stored.txt').write_text('\\n'.join(sys.argv[1:]))\n"
                )
                env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "TT_OUT": str(out), "TT_SCRATCH_ROOT": str(scratch), "GITHUB_SHA": "abc", "TT_EVENT": "push"}
                result = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                stored = (work / "stored.txt").read_text()
                self.assertIn("fleet.json", stored)
                self.assertIn("shards.tgz", stored)
                self.assertEqual("scratch-recordings.tgz" in stored, bool(kept))

    def test_the_budget_file_names_every_declared_cell(self):
        data = json.loads(BUDGET.read_text())
        for cell, value in data["budget_s"].items():
            self.assertIn(cell.split("/")[0], PROVIDERS)
            self.assertGreater(value, 0)


class StatusTests(unittest.TestCase):
    def test_the_payload_has_the_one_context_the_promotion_requirement_reads(self):
        sys.path.insert(0, str(ROOT / "scripts" / "ci"))
        import terminal_truth_status as status

        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7", "GITHUB_SERVER_URL": "https://github.com"}
        body = status.payload("success", "x" * 300, env)
        self.assertEqual(body["context"], "longhouse/terminal-truth")
        self.assertEqual(body["target_url"], "https://github.com/o/r/actions/runs/7")
        self.assertEqual(len(body["description"]), 140)
        with self.assertRaises(SystemExit):
            status.payload("cancelled", "x", env)


def fleet(cells: dict[str, str], mutations: tuple[str, ...] = (), **extra) -> dict:
    """fleet.json of a run that planned ``cells`` (id -> outcome) and ``mutations``."""

    return {
        "state": "success", "narrowed": False, "jobs": 8, "wall_s": 290.0, "reruns": [],
        "planned": {"cells": sorted(cells), "mutations": list(mutations)},
        "cells": [
            {"id": cell_id, "cell": cell_id.split(":")[1], "scenario": cell_id.split(":")[0], "outcome": outcome,
             "verdict": outcome if outcome != "finding" else "fail", "stage": None if outcome == "pass" else "state_shown",
             "reason": "" if outcome == "pass" else "woke nobody"}
            for cell_id, outcome in cells.items() if outcome != "absent"
        ],
        **extra,
    }  # fmt: skip


def mutation(name: str, ok: bool = True, **extra) -> dict:
    return {"name": name, "check": f"mutation {name}", "cell": "claude/helm (thinking)", "expected_stage": "engine_posted", "baseline": "pass",
            "observed_outcome": "fail" if ok else "pass", "observed_stage": "engine_posted" if ok else None, "reason": "", "ok": ok, **extra}  # fmt: skip


GOOD_RUN = {
    "fleet.json": fleet({"long_reply:codex/helm": "pass", "thinking:claude/helm": "pass"}, ("m1", "half-speed")),
    "pass-1-long_reply.json": [row("codex/helm")],
    "mutations.json": [mutation("m1"), mutation("half-speed")],
}


class SummaryTests(unittest.TestCase):
    def run_summary(self, files: dict, *extra: str):
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in files.items():
                (Path(tmp) / name).write_text(content if isinstance(content, str) else json.dumps(content))
            result = subprocess.run([sys.executable, str(SUMMARY), tmp, *extra], capture_output=True, text=True)
            return result, json.loads((Path(tmp) / "gate.json").read_text())

    def test_a_run_whose_every_planned_item_is_good_succeeds(self):
        result, gate = self.run_summary(GOOD_RUN, "--gate-profile")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(gate["state"], "success")
        self.assertEqual((gate["cells"], gate["mutation_checks"]), (2, 2))
        self.assertEqual(gate["description"], "terminal truth: 2 cells, 2 mutation checks ok in 290 s")
        self.assertEqual(gate["medians"], {"codex/helm [long_reply]": [0.2]})

    def test_no_fleet_file_fails_closed_even_when_every_other_file_is_good(self):
        files = {k: v for k, v in GOOD_RUN.items() if k != "fleet.json"}
        result, gate = self.run_summary(files)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no readable fleet.json: the run did not finish", gate["problems"])
        result, gate = self.run_summary({**GOOD_RUN, "fleet.json": "{not json"})
        self.assertEqual(result.returncode, 1)
        result, gate = self.run_summary({**GOOD_RUN, "fleet.json": fleet({})})
        self.assertEqual(result.returncode, 1)
        self.assertIn("the run planned no cell", gate["problems"])

    def test_a_planned_cell_with_a_failing_verdict_or_none_fails_and_names_the_stage(self):
        files = {**GOOD_RUN, "fleet.json": fleet({"long_reply:codex/helm": "pass", "thinking:claude/helm": "fail"}, ("m1", "half-speed"))}
        result, gate = self.run_summary(files)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(gate["failing_cells"], ["claude/helm [thinking]"])
        self.assertIn("::error::terminal truth failure claude/helm [thinking]: fail at state_shown: woke nobody", result.stdout)
        files = {**GOOD_RUN, "fleet.json": fleet({"long_reply:codex/helm": "pass", "thinking:claude/helm": "absent"}, ("m1", "half-speed"))}
        result, gate = self.run_summary(files)
        self.assertEqual(result.returncode, 1)
        self.assertIn("claude/helm [thinking]: no verdict for this planned cell", gate["problems"])

    def test_a_finding_is_reported_and_does_not_fail(self):
        files = {**GOOD_RUN, "fleet.json": fleet({"long_reply:codex/helm": "pass", "thinking:opencode/console": "finding"}, ("m1", "half-speed"))}
        result, gate = self.run_summary(files)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(gate["findings"], ["opencode/console [thinking]: woke nobody"])
        self.assertIn("::warning::terminal truth finding opencode/console [thinking]", result.stdout)

    def test_an_embedding_lane_that_never_loaded_and_a_lowered_shard_count_are_notes_not_failures(self):
        cells = {"long_reply:codex/helm": "pass", "thinking:claude/helm": "pass"}
        loaded = fleet(cells, ("m1", "half-speed"), embedding_lane={"shard": "long_reply-0", "loaded": True}, jobs_asked=8)
        result, gate = self.run_summary({**GOOD_RUN, "fleet.json": loaded}, "--gate-profile")
        self.assertEqual((result.returncode, gate["notes"]), (0, []))
        short = fleet(cells, ("m1", "half-speed"), embedding_lane={"shard": "long_reply-0", "loaded": False}, jobs=4, jobs_asked=8)
        result, gate = self.run_summary({**GOOD_RUN, "fleet.json": short}, "--gate-profile")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            gate["notes"],
            [
                "the embedding model did not load in long_reply-0, so no cell ran beside the embedding lane",
                "4 shards ran at a time, not the 8 asked for: memory held no more",
            ],
        )
        self.assertIn("::warning::terminal truth: the embedding model did not load in long_reply-0", result.stdout)

    def test_a_planned_mutation_check_that_is_missing_or_not_ok_fails_with_its_reason(self):
        result, gate = self.run_summary({**GOOD_RUN, "mutations.json": [mutation("m1")]})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(gate["mutation_misses"], ["mutation check half-speed: no row in mutations.json"])
        result, gate = self.run_summary({k: v for k, v in GOOD_RUN.items() if k != "mutations.json"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(gate["mutation_misses"]), 2)
        missed = mutation("m1", ok=False, reason="the gate passed the mutated engine")
        result, gate = self.run_summary({**GOOD_RUN, "mutations.json": [missed, mutation("half-speed")]})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            gate["mutation_misses"],
            ["mutation check m1 (claude/helm (thinking)): expected engine_posted, saw pass at None: the gate passed the mutated engine"],
        )
        unmutated = mutation("m1", ok=False, baseline="fail", baseline_stage="preflight", baseline_reason="never ready")
        result, gate = self.run_summary({**GOOD_RUN, "mutations.json": [unmutated, mutation("half-speed")]})
        self.assertIn("its unmutated cell came out fail at preflight: never ready", gate["mutation_misses"][0])

    def test_a_row_that_passed_only_when_run_again_alone_is_a_warning_and_stays_in_the_record(self):
        again = {"id": "long_reply:codex/helm", "first": "fail at stream_delivered: median 1.7 s, budget 1.0 s", "again": "pass"}
        files = {**GOOD_RUN, "fleet.json": {**GOOD_RUN["fleet.json"], "reruns": [again]}}
        result, gate = self.run_summary(files)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(gate["passed_on_rerun"], [{**again, "cell": "codex/helm [long_reply]"}])
        self.assertTrue(gate["description"].endswith(", 1 run again alone"))
        self.assertIn("::warning::terminal truth long_reply:codex/helm passed only when run again alone", result.stdout)

    def test_a_narrowed_run_cannot_be_the_gate(self):
        narrowed = {**GOOD_RUN, "fleet.json": {**GOOD_RUN["fleet.json"], "narrowed": True}}
        result, gate = self.run_summary(narrowed)
        self.assertEqual(result.returncode, 0, result.stdout)  # a narrowed hand-started run is judged on what it planned
        result, gate = self.run_summary(narrowed, "--gate-profile")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any("the run was narrowed" in p for p in gate["problems"]))
        unmarked = {k: v for k, v in GOOD_RUN["fleet.json"].items() if k != "narrowed"}
        result, gate = self.run_summary({**GOOD_RUN, "fleet.json": unmarked}, "--gate-profile")
        self.assertEqual(result.returncode, 1)  # a fleet.json that does not say is not trusted to be whole

    def test_a_reply_row_that_is_not_good_fails_whatever_the_fleet_says(self):
        bad = row("codex/helm", "fail", None, stage="host_published")
        result, gate = self.run_summary({**GOOD_RUN, "pass-1-long_reply.json": [bad]})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(gate["failing_cells"], ["codex/helm [long_reply]"])
        self.assertIn("host_published", result.stdout)
        known = row("codex/helm", "known_bad", None, stage="engine_posted")
        result, gate = self.run_summary({**GOOD_RUN, "pass-1-long_reply.json": [known]})
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_missing_pass_file_fails_closed(self):
        result, gate = self.run_summary({k: v for k, v in GOOD_RUN.items() if not k.startswith("pass-")})
        self.assertEqual(result.returncode, 1)
        self.assertIn("pass 1 (long_reply): no readable result file", gate["problems"])
        result, gate = self.run_summary(GOOD_RUN, "--repeats", "2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pass 2 (long_reply): no readable result file", gate["problems"])

    def test_a_cell_whose_fake_model_served_nothing_fails_closed(self):
        result, gate = self.run_summary({**GOOD_RUN, "pass-1-long_reply.json": [row("codex/helm", requests=0)]})
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any("served no request" in p for p in gate["problems"]))

    def test_a_pass_with_fewer_cells_than_the_first_fails_the_coverage_floor(self):
        files = {**GOOD_RUN, "pass-1-long_reply.json": [row("codex/helm"), row("pi/helm")], "pass-2-long_reply.json": [row("codex/helm")]}
        result, gate = self.run_summary(files, "--repeats", "2")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any("1 rows, the first pass had 2" in p for p in gate["problems"]))

    def test_the_previous_nightly_marks_which_failures_are_new(self):
        bad = {**GOOD_RUN, "fleet.json": fleet({"long_reply:codex/helm": "pass", "thinking:claude/helm": "fail", "tool:pi/helm": "fail"})}
        with tempfile.TemporaryDirectory() as previous:
            (Path(previous) / "gate.json").write_text(json.dumps({"failing_cells": ["pi/helm [tool]"]}))
            result, gate = self.run_summary(bad, "--previous", previous)
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::terminal truth NEW failure claude/helm [thinking]", result.stdout)
        self.assertIn("::warning::terminal truth still failing pi/helm [tool]", result.stdout)


if __name__ == "__main__":
    unittest.main()
