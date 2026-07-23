"""Read-only tool translation evaluation commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from zerg.services.tool_translation_evaluator import TranslationEvaluationError
from zerg.services.tool_translation_evaluator import evaluate_codex_archive
from zerg.services.tool_translation_evaluator import evaluate_manifest
from zerg.services.tool_translation_evaluator import write_evidence_package

app = typer.Typer(help="Evaluate provider tool-call translation against a replay corpus")


@app.command(name="evaluate")
def evaluate(
    corpus: Path = typer.Option(..., "--corpus", exists=True, dir_okay=False, readable=True),
    json_output: bool = typer.Option(False, "--json", help="Emit the complete machine-readable report"),
    output: Path | None = typer.Option(None, "--output", help="Also write the report to this local path"),
    output_dir: Path | None = typer.Option(None, "--output-dir", help="Write an immutable factory evidence package"),
    profile: str = typer.Option("hermetic", "--profile", help="Factory proof profile"),
) -> None:
    """Replay a privacy-safe manifest without changing its source evidence."""

    try:
        report = evaluate_manifest(corpus, profile=profile)
    except TranslationEvaluationError as exc:
        typer.echo(f"translation evaluation failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if output is not None:
        output.write_text(rendered + "\n")
    package = write_evidence_package(report, output_dir) if output_dir is not None else None
    if json_output:
        typer.echo(rendered)
    else:
        totals = report["totals"]
        typer.echo(
            f"{'PASS' if report['passed'] else 'FAIL'}: "
            f"{totals.get('outer_calls', 0)} calls, {totals.get('paired', 0)} paired, "
            f"{totals.get('exact', 0)} Exact, {totals.get('unknown', 0)} Unknown"
        )
        if output is not None:
            typer.echo(f"Report: {output}")
        if package is not None:
            typer.echo(f"Evidence: {package}")
    if not report["passed"]:
        raise typer.Exit(code=1)


@app.command(name="discover")
def discover(
    archive_root: Path = typer.Option(
        Path.home() / ".codex" / "sessions",
        "--archive-root",
        exists=True,
        file_okay=False,
        readable=True,
        help="Authorized native Codex archive root",
    ),
    max_files: int = typer.Option(500, "--max-files", min=1, max=10000),
    output_dir: Path = typer.Option(..., "--output-dir", help="Factory evidence package root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inventory real Codex wire shapes without exporting transcript values."""

    try:
        report = evaluate_codex_archive(archive_root, max_files=max_files)
        package = write_evidence_package(report, output_dir)
    except TranslationEvaluationError as exc:
        typer.echo(f"translation discovery failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if json_output:
        typer.echo(json.dumps({**report, "evidence_package": str(package)}, indent=2, sort_keys=True))
    else:
        totals = report["totals"]
        typer.echo(
            f"{report['verdicts']['transcript']['verdict'].upper()}: "
            f"{totals['outer_calls']} calls across {totals['archive_files']} archives; "
            f"{totals['wrappers_receded']} wrappers safely receded, {totals['unknown']} Unknown"
        )
        typer.echo(f"Evidence: {package}")
    if not report["passed"]:
        raise typer.Exit(code=1)
