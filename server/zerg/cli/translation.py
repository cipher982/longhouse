"""Read-only tool translation evaluation commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from zerg.services.tool_translation_evaluator import TranslationEvaluationError
from zerg.services.tool_translation_evaluator import evaluate_manifest

app = typer.Typer(help="Evaluate provider tool-call translation against a replay corpus")


@app.command(name="evaluate")
def evaluate(
    corpus: Path = typer.Option(..., "--corpus", exists=True, dir_okay=False, readable=True),
    json_output: bool = typer.Option(False, "--json", help="Emit the complete machine-readable report"),
    output: Path | None = typer.Option(None, "--output", help="Also write the report to this local path"),
) -> None:
    """Replay a privacy-safe manifest without changing its source evidence."""

    try:
        report = evaluate_manifest(corpus)
    except TranslationEvaluationError as exc:
        typer.echo(f"translation evaluation failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if output is not None:
        output.write_text(rendered + "\n")
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
    if not report["passed"]:
        raise typer.Exit(code=1)
