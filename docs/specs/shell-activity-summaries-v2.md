# Shell Activity Summaries v2

Status: implemented and validated (2026-07-23)
Owner: Longhouse timeline reading experience
Related: `docs/specs/timeline-reading-experience.md`,
`docs/specs/tool-translation-experience.md`

## Problem

The prose-first activity grouping fixed the original transcript failure: dozens
of provider/tool rows no longer dominate the conversation. It also exposed the
next failure. A collapsed row such as:

> Ran 2

preserves quantity but removes the only useful meaning. In a real hosted Codex
session, expanding `Ran 2` revealed two `gh run view ...` commands. The compact
row should have read:

> Ran `gh run view` ×2

The goal is not to reveal every argument or reproduce a terminal command line.
It is to retain the recognizable operation while keeping the transcript quiet.

## Product contract

- Activity rows remain collapsed by default and every original call remains
  reachable through the existing disclosure.
- Shell activity uses the executable plus a small meaningful subcommand shape
  when deterministic syntax supports it.
- Repeated equivalent operations deduplicate with `×N`.
- Show at most two distinct shell operations in the collapsed row, followed by
  `+N more` when necessary.
- Never search arbitrary command text for a familiar executable. `rg 'gh run
  view' docs/` is `rg`, not `gh`.
- Never claim runtime execution from source parsing. The collapsed summary is a
  syntactic description of what the provider asked the shell to evaluate.
- Dynamic or unsupported source falls back honestly to `Ran N`.
- Raw source, output, provider identity, timing, and call/result pairing remain
  unchanged and immediately expandable.

Examples:

| Source | Collapsed operation | Confidence |
|---|---|---|
| `TOKEN=x gh run view 123` | `gh run view` | syntactic |
| `env TOKEN=x sudo timeout 30 gh run view 123` | `gh run view` | syntactic |
| `cd repo && git status --short` | remains `Read 1` via existing salience | syntactic |
| `bash -lc 'gh run view 1 && make test'` | `gh run view · make test` | partial |
| `npm test \| tee test.log` | `npm test` | partial |
| `eval "$BUILD_CMD"` | no operation label | opaque |
| `"$PROGRAM" "${ARGS[@]}"` | no operation label | opaque |
| `rg 'gh run view' docs/` | `rg` | syntactic |

An activity group may combine semantic categories and shell operations:

> Searched 3 · Read 4 · Edited 1 · Ran `gh run view` ×2

If distinct shell operations exceed the display budget:

> Ran `git diff` · `make test` · +3 more

The existing call-count badge remains the exact number of canonical calls, not
the number of displayed operation labels.

## Authority and provenance

Longhouse must keep three different facts separate:

1. **Provider source** — the exact `command`/`cmd` sent in the tool call. This is
   archive evidence.
2. **Syntactic summary** — a deterministic read-time interpretation of literal
   shell structure. This is disposable presentation.
3. **Observed execution** — actual spawned processes/argv captured at runtime.
   Longhouse does not collect this for the feature.

Runtime observation remains a possible future, opt-in managed-session
diagnostic. Linux eBPF/audit/ptrace and macOS EndpointSecurity add privilege,
privacy, platform, and lifecycle costs; they cannot cover historical or Shadow
sessions and miss shell builtins. They must never silently replace source
evidence or syntactic summaries.

## Architecture

Parse once in the Runtime Host's existing `tool_presentation` projection and
send the result to web and iOS. Do not add a third client-side shell grammar or
teach Swift and TypeScript independent summary rules.

The raw event stays untouched. Extend `ToolPresentationResponse` with an
optional shell summary:

```json
{
  "shell_summary": {
    "version": 1,
    "confidence": "syntactic",
    "operations": [
      {
        "key": "gh run view",
        "label": "gh run view",
        "executable": "gh",
        "subcommands": ["run", "view"],
        "count": 1
      }
    ],
    "candidate_count": 1,
    "truncated": false,
    "dynamic": false,
    "parse_error": null,
    "parser_id": "bounded-shell-v1",
    "shape_registry_version": 1
  }
}
```

Contract rules:

- `confidence`: `syntactic | partial | opaque`.
- `operations` contains only positive AST-derived executable candidates.
- `candidate_count` counts distinct candidates before the UI display cap.
- `truncated` means the parser hit a configured source/node/depth/candidate
  bound, not that the UI hid extra labels.
- `dynamic` records syntax such as `eval`, dynamic command words, aliases or
  unresolved nested source that prevents a trustworthy headline.
- `parse_error` is a bounded diagnostic code, never raw source or an exception.
- `parser_id` and `shape_registry_version` are part of cache identity and replay
  diagnostics.
- The presentation version increments when semantic output changes.
- The server may cache the pure projection by source digest and parser version;
  it must not persist it as archive authority.

Web and iOS consume the same payload. Their only local logic is group-level
deduplication, display budgeting, typography, and fallback. Historical sessions
are improved immediately because presentation is projected at read time.

## Parser policy

Use one bounded, fail-closed server-side scanner in v1. It handles only the
literal structures below, reusing the segment, assignment, quote and wrapper
policies already exercised by Longhouse's shell-salience corpus. This avoids a
native parser/wheel/Alpine dependency and does not create another client
grammar. `tree-sitter-bash` remains the preferred replacement backend only if
real-corpus replay shows that the bounded scanner leaves an unacceptable share
of useful literal commands opaque. It must replace the scanner behind the same
response contract, never become a parallel parser.

Supported v1 structures:

- literal leading assignments (`A=1 B=x command`)
- literal lists and `;`, `&&`, `||`; later commands may contribute within the
  two-label budget, but make the result `partial`
- pipelines, taking only the first non-wrapper simple command as the headline
- literal wrapper unwrapping for:
  - `env`
  - `sudo` / `doas`
  - `timeout`
  - `command`
  - `nice` / `nohup`
- recursive parsing of a literal payload for `sh`, `bash`, or `zsh` with
  `-c`/`-lc`
- neutral `cd`/`pushd`/`popd` navigation when followed by another command
- redirections without exposing their target in the headline; later pipeline
  stages such as `tee` are supporting mechanics and do not become labels

Opaque/partial conditions:

- dynamic command word (`"$PROGRAM"`, `${cmd}`)
- `eval` or `source` of unresolved content
- alias/function identity that cannot be proved from the source
- malformed syntax or error nodes intersecting a candidate
- nested literal recursion, source length, AST node, or candidate bounds hit
- dialect-specific constructs unsupported by the selected grammar

The parser never expands variables, reads environment state, resolves PATH,
loads aliases/functions, executes substitutions, or evaluates branches.

## Operation labels

Default label: executable basename only. Include subcommands through a small,
reviewed command-shape registry rather than arbitrary positional arguments.

Initial shapes:

- `gh run view|watch|list`, `gh pr ...`, `gh workflow ...`
- `git <subcommand>` after global options
- `docker compose <subcommand>` and `docker <subcommand>`
- `kubectl <verb>`
- `npm|pnpm|yarn|bun run <script>` only when the script name passes a strict
  safe-token policy; otherwise stop at `run`. Common direct verbs such as
  `test` are safe.
- `uv run <command>` / `uv <verb>`
- `make <target>` only when the target passes the safe-token policy; otherwise
  display `make`
- test/lint tools such as `pytest`, `ruff`, `cargo test`, `go test`

Unknown executables remain useful as their basename (`acme-deploy`) without
guessing which arguments are semantic. Never include values likely to contain
identifiers, paths, URLs, tokens, query text, or user data in the collapsed
headline.

Operation equivalence is the normalized displayed shape, not the full argv.
Therefore `gh run view 123` and `gh run view 456` display as
`gh run view ×2` while expansion preserves both exact calls.

## Privacy

- Do not collect environment variables or observed process argv.
- Never put assignment values, flag values, URLs, paths, redirection targets,
  substitutions, or stdin in the summary.
- Preserve the existing protected raw command under disclosure.
- Treat parser diagnostics as codes only.
- Add adversarial fixtures for secrets in assignments, flags, URLs, heredocs,
  nested scripts, and substitutions. Tests must assert absence from summary.

## Group summary algorithm

For every interaction in an activity group:

1. Preserve existing `Searched`, `Read`, `Listed`, `Viewed`, `Edited`, `Called`,
   and `Waited` classification.
2. Only after existing shell salience resolves the interaction to `Ran`, use
   non-opaque `shell_summary.operations`. Commands demoted to Search/Read/List
   never reappear as named Ran operations.
3. Deduplicate by operation key and count occurrences.
4. Render up to two operation labels in first-evidence order.
5. Append `×N` for repeated labels.
6. Append `+N more` for remaining distinct labels.
7. Count opaque shell calls and non-shell Ran interactions in one generic
   remainder. If all Ran interactions are unnamed, retain `Ran N`. If some are
   named, append one `+N other` suffix.
8. Never emit two Ran segments. The exact unstyled fixture string is
   `Ran gh run view ×2`; code styling is a rendering concern only.

One shell call may yield more than one list operation, but pipeline support
commands never do. The call-count badge remains canonical-call count regardless
of the number of displayed operation labels.

## Codex wrapper rule

Attach `shell_summary` only to the final presented tool after structural wrapper
recession. A single completely recovered, result-forwarded Codex `exec` child
that becomes `exec_command` receives the summary from its promoted `cmd`.
Non-receded multi-child wrappers remain `Called N tools`; v2 does not mine their
children into activity-header Ran operations. Do not add `shell_summary` to
`ToolPresentationChildResponse` until recovered children become first-class
timeline interactions.

## Reliability and observability

The parser is presentation infrastructure and must fail visibly, not silently
decay after provider or dependency changes.

Track aggregate diagnostics without command contents:

- eligible shell calls
- syntactic / partial / opaque rate
- parse-error code rate
- truncation rate
- summaries with zero, one, two, or more candidates
- provider and shell-tool-name dimensions

Add a replay command/test helper that runs historical fixtures or exported
events through the current projection and reports coverage without persisting
source. A sudden increase in opaque or parse failures should fail the provider
presentation canary or CI fixture gate.

## Test strategy

### Parser conformance

Create a shared JSON corpus used by server tests and client projection fixtures.
It must include:

- simple commands and arguments
- quoted arguments containing shell operators or executable names
- leading assignments, including quoted/spaced values
- every supported wrapper with options and assignments
- literal and dynamic `bash|zsh|sh -c/-lc`
- `cd &&`, lists, pipelines, subshells, substitutions and conditionals
- comments, multiline input and malformed/incomplete syntax
- aliases/functions/eval/source/dynamic command words
- absolute and relative executable paths (display basename, retain provenance)
- secret-shaped assignments, flags, headers, URLs and heredocs
- source/node/depth/candidate bound cases

Each fixture asserts confidence, ordered operations, deduplication key, dynamic
and truncation flags, and forbidden substrings that must never appear.

### Projection integration

- `project_tool_presentation` produces the shell summary for every configured
  shell tool and for a Codex `exec` wrapper whose recovered child is a shell
  tool.
- The original provider input and wrapper provenance remain unchanged.
- API response/OpenAPI/generated Swift types and the hand-maintained web
  `AgentToolPresentation` carry the optional structure exactly.
- Old/missing summaries decode safely and fall back to `Ran N`.

### Timeline integration

- `gh run view` twice renders `Ran gh run view ×2` while preserving two child
  calls under disclosure.
- Mixed semantic activity renders categories plus named shell operations.
- More than two distinct operations uses `+N more` deterministically.
- Partial/opaque mixtures use `+N other` and never invent an executable.
- Failures, pending calls and questions remain outside collapsed activity rows.
- Deep-linking a child still expands the parent and exact call.
- Web and iOS golden/projection fixtures produce the same summary text.

### Real-corpus regression

Replay the previously sampled worst-case Codex, Claude, Cursor and OpenCode
sessions. Record:

- activity rows still achieve the existing compression target
- percentage of generic `Ran N` rows before and after
- percentage of named operations by confidence
- no change to total expandable call count
- no failures/questions/live tools newly collapsed

Visually inspect at least the original `Ran 2` / `gh run view` example and one
mixed worst-case transcript at desktop and mobile widths.

## Delivery plan

1. Write the shared best-case/adversarial corpus first, including forbidden
   substrings, before product code.
2. Add the bounded server-side parser and command-shape registry.
3. Extend the versioned presentation response, regenerate API clients, and
   update the hand-maintained web presentation type.
4. Update web and iOS group-summary formatting with identical text fixtures.
5. Add projection, API, timeline, disclosure and conservation coverage.
6. Replay real hosted transcripts and tune only evidence-backed shape rules.
7. Promote to tree-sitter only if corpus replay shows a material literal-command
   miss rate that the bounded scanner cannot safely address.
8. Run focused tests during implementation, then frontend/backend/iOS, core
   E2E, visual capture and exact-SHA production verification at cutover.

## Non-goals

- No shell execution or evaluation.
- No claim that a syntactic candidate actually executed.
- No default eBPF, auditd, ptrace, shell-hook, or EndpointSecurity capture.
- No environment/alias/function/PATH reconstruction.
- No LLM-generated command summaries.
- No unbounded command-specific ontology.
- No alternate raw-vs-pretty timeline mode.
- No replacing the existing client-side shell-salience classifier in this epic.
- No mining non-receded wrapper children into group summaries.

## Acceptance criteria

- The motivating production row renders `Ran gh run view ×2` collapsed.
- Best-case literal commands receive stable, useful summaries.
- Worst-case dynamic/malformed commands degrade to honest generic counts.
- Secret-shaped values never enter summary payloads or telemetry.
- Web and iOS render identical group copy from one server projection.
- Every raw call/result remains reachable and total call conservation holds.
- Historical, Shadow, Helm and Console sessions all benefit without migration
  or new machine privileges.
