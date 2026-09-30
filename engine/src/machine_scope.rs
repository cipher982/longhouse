//! `longhouse machine scope`: choose which local history the Machine Agent may
//! import (see `import_scope`).
//!
//! The installers call this before the agent starts, so a stranger's old
//! transcripts are imported only after they say so. It can be run again at any
//! time: widening the scope backfills what became eligible, narrowing it stops
//! importing more (what already reached the Runtime Host stays there).

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{bail, Context};
use chrono::{DateTime, Utc};
use clap::Args;

use crate::import_scope::{parse_since, ImportScope};

#[derive(Args)]
pub struct MachineScopeArgs {
    /// Import sessions that started on or after this point: `now` (only new
    /// sessions), `all` (everything on this computer), a date such as
    /// 2026-09-01, or an RFC 3339 time.
    #[arg(long, value_name = "now|all|DATE")]
    pub since: Option<String>,
    /// Also import the full history of sessions run in this folder or below it.
    /// Repeat for several folders; the list replaces the current one.
    #[arg(long = "project", value_name = "PATH")]
    pub projects: Vec<PathBuf>,
    /// Remove every per-folder opt-in.
    #[arg(long, conflicts_with = "projects")]
    pub no_projects: bool,
    /// Ask what to import. Needs a terminal.
    #[arg(long, conflicts_with_all = ["since", "projects", "no_projects"])]
    pub prompt: bool,
    /// Print the scope and what it covers as JSON.
    #[arg(long)]
    pub json: bool,
    /// Longhouse state root override (tests and scratch installs).
    #[arg(long)]
    pub state_root: Option<PathBuf>,
}

pub fn run(args: MachineScopeArgs) -> anyhow::Result<()> {
    let home = match &args.state_root {
        Some(root) => root.clone(),
        None => crate::longhouse_home()?,
    };
    let machine_dir = home.join("machine");
    let changing =
        args.prompt || args.since.is_some() || !args.projects.is_empty() || args.no_projects;
    if !changing {
        return show(&home, &machine_dir, args.json);
    }
    let stored = ImportScope::load(&machine_dir)?;
    let now = Utc::now();
    let next = if args.prompt {
        if !crate::interactive_stdio() {
            bail!(
                "no terminal to ask on. Choose with --since now|all|DATE (and --project PATH for older folders)."
            );
        }
        let summary = engine_summary(&home);
        prompt_for_scope(
            &mut std::io::stdin().lock(),
            &mut std::io::stdout(),
            summary.as_ref(),
            now,
        )?
    } else {
        scope_from_flags(stored.as_ref(), &args, now)?
    };
    let narrowing = stored.as_ref().is_some_and(|old| narrows(old, &next));
    next.save(&machine_dir)?;
    if args.json {
        println!("{}", serde_json::to_string_pretty(&next)?);
        return Ok(());
    }
    println!("Import scope: {}", next.describe());
    if narrowing {
        println!(
            "Sessions already uploaded stay on your Runtime Host; this only stops importing more."
        );
    } else if !next.is_unrestricted() && next.projects.is_empty() {
        println!("Older sessions on this computer stay local and are never uploaded.");
        println!("Import history later with: longhouse machine scope --since all");
    }
    println!(
        "A running Machine Agent applies this within seconds and backfills anything newly in scope."
    );
    Ok(())
}

/// Apply flags to the stored scope. A dimension whose flag is absent keeps its
/// stored value; a machine with no stored scope starts from now.
fn scope_from_flags(
    stored: Option<&ImportScope>,
    args: &MachineScopeArgs,
    now: DateTime<Utc>,
) -> anyhow::Result<ImportScope> {
    let mut next = stored
        .cloned()
        .unwrap_or_else(|| ImportScope::starting(now, "cli"));
    if let Some(since) = &args.since {
        next.since = parse_since(since, now)?;
    }
    if args.no_projects {
        next.projects.clear();
    }
    if !args.projects.is_empty() {
        next.projects = canonical_projects(&args.projects)?;
    }
    if next.since.is_none() {
        if !args.projects.is_empty() {
            bail!(
                "--project narrows nothing while the scope is `all` (every session is imported). \
Pass --since now or --since DATE with it."
            );
        }
        next.projects.clear();
    }
    next.chosen_at = now;
    next.chosen_via = "cli".to_string();
    Ok(next)
}

fn canonical_projects(paths: &[PathBuf]) -> anyhow::Result<Vec<PathBuf>> {
    let mut projects: Vec<PathBuf> = Vec::new();
    for path in paths {
        let canonical = path
            .canonicalize()
            .with_context(|| format!("{} is not a folder that exists", path.display()))?;
        if !canonical.is_dir() {
            bail!("{} is not a folder", path.display());
        }
        if canonical.parent().is_none() {
            bail!("refusing to opt in the whole file system as a project");
        }
        if !projects.contains(&canonical) {
            projects.push(canonical);
        }
    }
    Ok(projects)
}

/// Whether `next` imports less than `old` would.
fn narrows(old: &ImportScope, next: &ImportScope) -> bool {
    match (old.since, next.since) {
        (None, Some(_)) => true,
        (Some(old_since), Some(next_since)) if next_since > old_since => true,
        _ => old
            .projects
            .iter()
            .any(|project| !next.projects.contains(project)),
    }
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

fn show(home: &Path, machine_dir: &Path, json: bool) -> anyhow::Result<()> {
    if json {
        return match run_engine_summary(home, true) {
            Some(raw) => {
                print!("{raw}");
                Ok(())
            }
            None => bail!("the paired longhouse-engine could not report the import scope"),
        };
    }
    match ImportScope::load(machine_dir)? {
        Some(scope) => println!(
            "Import scope: {} (set by {})",
            scope.describe(),
            scope.chosen_via
        ),
        None => println!("Import scope: not chosen yet; the Machine Agent will start from now on."),
    }
    if let Some(summary) = engine_summary(home) {
        for provider in summary["providers"].as_array().into_iter().flatten() {
            println!(
                "  {:<12} {} in scope, {} not imported",
                provider["provider"].as_str().unwrap_or("?"),
                provider["in_scope"],
                provider["outside_scope"]
            );
        }
        println!(
            "  {:<12} {} in scope, {} not imported",
            "total", summary["total_in_scope"], summary["total_outside_scope"]
        );
    }
    println!("Change it: longhouse machine scope --since now|all|DATE [--project PATH ...]");
    Ok(())
}

fn run_engine_summary(home: &Path, json: bool) -> Option<String> {
    let mut command = Command::new(crate::paired_engine_path().ok()?);
    command.args(["device", "import-scope"]);
    if json {
        command.arg("--json");
    }
    command.env("LONGHOUSE_HOME", home);
    let output = command.output().ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Counts of what this machine holds, from the paired engine. Best effort: a
/// missing engine only means the prompt cannot quote numbers.
fn engine_summary(home: &Path) -> Option<serde_json::Value> {
    serde_json::from_str(&run_engine_summary(home, true)?).ok()
}

// ---------------------------------------------------------------------------
// Prompt
// ---------------------------------------------------------------------------

fn prompt_for_scope(
    input: &mut impl BufRead,
    out: &mut impl Write,
    summary: Option<&serde_json::Value>,
    now: DateTime<Utc>,
) -> anyhow::Result<ImportScope> {
    // Everything found is older than the moment of choosing, so what "from now
    // on" leaves out is every session counted here.
    let existing = summary
        .map(|value| {
            let outside = value["total_outside_scope"].as_u64().unwrap_or(0);
            let inside = value["total_in_scope"].as_u64().unwrap_or(0);
            outside + inside
        })
        .filter(|count| *count > 0);
    writeln!(out)?;
    match existing {
        Some(count) => writeln!(
            out,
            "Longhouse found {count} coding-agent sessions already saved on this computer."
        )?,
        None => writeln!(
            out,
            "Longhouse can import coding-agent sessions saved on this computer."
        )?,
    }
    writeln!(
        out,
        "Old sessions can hold code and secrets from any project you ever ran an agent in.\n\
What should Longhouse import?\n\
  1) Nothing old: only sessions you start from now on (recommended)\n\
  2) Nothing old, plus the full history of project folders you name\n\
  3) Sessions since a date\n\
  4) Everything on this computer"
    )?;
    let choice = ask(input, out, "Choice [1]: ")?;
    let mut scope = ImportScope::starting(now, "prompt");
    match choice.trim() {
        "" | "1" => {}
        "2" => {
            let mut folders: Vec<PathBuf> = Vec::new();
            loop {
                let folder = ask(input, out, "Project folder (blank to finish): ")?;
                let folder = folder.trim();
                if folder.is_empty() {
                    break;
                }
                let folder = expand_home(folder);
                match canonical_projects(&[folder]) {
                    Ok(added) => {
                        for project in added {
                            if !folders.contains(&project) {
                                folders.push(project);
                            }
                        }
                    }
                    Err(error) => writeln!(out, "  {error:#}")?,
                }
            }
            scope.projects = folders;
        }
        "3" => loop {
            let date = ask(input, out, "Import sessions since (YYYY-MM-DD): ")?;
            match parse_since(&date, now) {
                Ok(since @ Some(_)) => {
                    scope.since = since;
                    break;
                }
                Ok(None) => {
                    scope.since = None;
                    break;
                }
                Err(error) => writeln!(out, "  {error:#}")?,
            }
        },
        "4" => scope.since = None,
        other => bail!("`{other}` is not one of the choices"),
    }
    Ok(scope)
}

fn ask(input: &mut impl BufRead, out: &mut impl Write, question: &str) -> anyhow::Result<String> {
    write!(out, "{question}")?;
    out.flush()?;
    let mut line = String::new();
    if input.read_line(&mut line)? == 0 {
        bail!("input ended before a choice was made");
    }
    Ok(line)
}

fn expand_home(value: &str) -> PathBuf {
    match (value.strip_prefix("~/"), std::env::var_os("HOME")) {
        (Some(rest), Some(home)) => PathBuf::from(home).join(rest),
        _ => PathBuf::from(value),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-09-30T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn args() -> MachineScopeArgs {
        MachineScopeArgs {
            since: None,
            projects: Vec::new(),
            no_projects: false,
            prompt: false,
            json: false,
            state_root: None,
        }
    }

    #[test]
    fn flags_choose_and_a_missing_flag_keeps_the_stored_dimension() {
        let dir = tempfile::tempdir().unwrap();
        let project = dir.path().canonicalize().unwrap();
        let mut first = args();
        first.since = Some("now".into());
        first.projects = vec![project.clone()];
        let scope = scope_from_flags(None, &first, now()).unwrap();
        assert_eq!(scope.since, Some(now()));
        assert_eq!(scope.projects, vec![project.clone()]);

        // Only the date changes; the project stays.
        let mut later = args();
        later.since = Some("2026-01-01".into());
        let widened = scope_from_flags(Some(&scope), &later, now()).unwrap();
        assert!(widened.since.unwrap() < now());
        assert_eq!(widened.projects, vec![project.clone()]);

        // `all` clears the project list: it would add nothing.
        let mut all = args();
        all.since = Some("all".into());
        let everything = scope_from_flags(Some(&widened), &all, now()).unwrap();
        assert!(everything.is_unrestricted());
        assert!(everything.projects.is_empty());
    }

    #[test]
    fn a_project_without_a_time_bound_is_refused_and_a_typo_is_caught() {
        let mut all = args();
        all.since = Some("all".into());
        all.projects = vec![std::env::temp_dir()];
        assert!(scope_from_flags(None, &all, now()).is_err());

        let mut typo = args();
        typo.projects = vec![PathBuf::from("/definitely/not/a/folder")];
        assert!(scope_from_flags(None, &typo, now()).is_err());

        let mut root = args();
        root.projects = vec![PathBuf::from("/")];
        assert!(scope_from_flags(None, &root, now()).is_err());
    }

    #[test]
    fn a_fresh_machine_with_only_a_project_starts_from_now() {
        let dir = tempfile::tempdir().unwrap();
        let mut only_project = args();
        only_project.projects = vec![dir.path().to_path_buf()];
        let scope = scope_from_flags(None, &only_project, now()).unwrap();
        assert_eq!(scope.since, Some(now()));
        assert_eq!(scope.projects.len(), 1);
    }

    #[test]
    fn narrowing_is_detected_and_widening_is_not() {
        let from_now = ImportScope::starting(now(), "cli");
        let all = ImportScope::all("cli");
        let earlier = ImportScope::starting(now() - chrono::Duration::days(30), "cli");
        assert!(narrows(&all, &from_now));
        assert!(narrows(&earlier, &from_now));
        assert!(!narrows(&from_now, &earlier));
        assert!(!narrows(&from_now, &all));
        let with_project = ImportScope {
            projects: vec![PathBuf::from("/work/a")],
            ..from_now.clone()
        };
        assert!(
            narrows(&with_project, &from_now),
            "dropping a project narrows"
        );
        assert!(!narrows(&from_now, &with_project));
    }

    fn scripted(
        answers: &str,
        summary: Option<serde_json::Value>,
    ) -> (anyhow::Result<ImportScope>, String) {
        let mut out = Vec::new();
        let scope = prompt_for_scope(
            &mut std::io::Cursor::new(answers.as_bytes().to_vec()),
            &mut out,
            summary.as_ref(),
            now(),
        );
        (scope, String::from_utf8(out).unwrap())
    }

    #[test]
    fn the_default_answer_imports_nothing_old_and_says_how_much_that_hides() {
        let summary = serde_json::json!({"total_in_scope": 0, "total_outside_scope": 1203});
        let (scope, shown) = scripted("\n", Some(summary));
        let scope = scope.unwrap();
        assert_eq!(scope.since, Some(now()));
        assert!(scope.projects.is_empty());
        assert_eq!(scope.chosen_via, "prompt");
        assert!(shown.contains("1203 coding-agent sessions"), "{shown}");
    }

    #[test]
    fn the_prompt_can_pick_projects_a_date_or_everything() {
        let dir = tempfile::tempdir().unwrap();
        let folder = dir.path().canonicalize().unwrap();
        let (scope, _) = scripted(
            &format!("2\n{}\n/no/such/place\n\n", folder.display()),
            None,
        );
        let scope = scope.unwrap();
        assert_eq!(scope.since, Some(now()));
        assert_eq!(scope.projects, vec![folder]);

        let (scope, shown) = scripted("3\nlast tuesday\n2026-09-01\n", None);
        assert!(scope.unwrap().since.unwrap() < now());
        assert!(
            shown.contains("expected `now`"),
            "a bad date is explained and asked again: {shown}"
        );

        let (scope, _) = scripted("4\n", None);
        assert!(scope.unwrap().is_unrestricted());
    }

    #[test]
    fn the_prompt_refuses_an_unknown_choice_and_ended_input() {
        assert!(scripted("9\n", None).0.is_err());
        assert!(scripted("", None).0.is_err());
    }
}
