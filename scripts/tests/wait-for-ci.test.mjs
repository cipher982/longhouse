import fs from 'node:fs';
import path from 'node:path';
// The deploy gate's wait (.github/actions/wait-for-ci) run against a fake
// GitHub API: which newer main head may stand a deploy down, and when the
// deploy has to follow that head's CI instead.
const actionPath = path.join(import.meta.dir, '../../.github/actions/wait-for-ci/action.yml');
// The `script: |` block, by indentation (no YAML parser in every Bun we run).
function actionScript(text) {
  const lines = text.split('\n');
  const start = lines.findIndex((line) => /^\s+script: \|\s*$/.test(line));
  if (start < 0) throw new Error('no script block in wait-for-ci/action.yml');
  const keyIndent = lines[start].search(/\S/);
  const block = [];
  for (const line of lines.slice(start + 1)) {
    if (line.trim() && line.search(/\S/) <= keyIndent) break;
    block.push(line);
  }
  const indent = Math.min(...block.filter((line) => line.trim()).map((line) => line.search(/\S/)));
  return block.map((line) => line.slice(indent)).join('\n');
}
const body = actionScript(fs.readFileSync(actionPath, 'utf8'));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
globalThis.setTimeout = (fn) => { fn(); return 0; };

async function run(name, { env, runs, jobs = {}, head }, expect) {
  const outputs = {}, notices = [], failed = [];
  const core = {
    info() {}, warning() {}, notice: (m) => notices.push(m),
    setOutput: (k, v) => (outputs[k] = v), setFailed: (m) => failed.push(m),
  };
  const github = { rest: {
    actions: {
      listWorkflowRuns: async ({ workflow_id }) => {
        const list = runs.filter((r) => r.workflow === workflow_id);
        return { data: { workflow_runs: list, total_count: list.length } };
      },
      listJobsForWorkflowRun: async ({ run_id }) => ({ data: { jobs: jobs[run_id] || [] } }),
    },
    repos: {
      getBranch: async () => ({ data: { commit: { sha: head } } }),
      compareCommits: async () => ({ data: { status: 'ahead' } }),
    },
  } };
  const context = { repo: { owner: 'o', repo: 'r' } };
  const saved = { ...process.env };
  Object.assign(process.env, { WORKFLOW_ID: 'contract-first-ci.yml', TIMEOUT_SECONDS: '60',
    REQUIRE_SUCCESS: 'true', SUPERSEDED_BRANCH: 'main', HEAD_BRANCH: 'main', COVERING_WORKFLOW: '',
    REQUIRED_JOBS: '', IGNORED_FAILURE_JOBS: '', ...env });
  await new AsyncFunction('github', 'context', 'core', 'process', body)(github, context, core, process);
  process.env = saved;
  const got = { superseded_by: outputs.superseded_by || '', failed: failed.length > 0 };
  const ok = got.superseded_by === expect.superseded_by && got.failed === expect.failed;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}: ${JSON.stringify(got)}${notices.length ? ' | ' + notices.at(-1).slice(0, 110) : ''}`);
  if (!ok) process.exitCode = 1;
}

const ci = (id, sha, conclusion) => ({ id, workflow: 'contract-first-ci.yml', head_sha: sha, status: 'completed', conclusion });
const img = (sha) => ({ id: 900, workflow: 'runtime-image.yml', head_sha: sha, status: 'completed', conclusion: 'success' });
const base = { HEAD_SHA: 'A' };

await run('old behaviour without covering_workflow: stand down',
  { env: base, head: 'B', runs: [ci(1, 'A', 'cancelled'), ci(2, 'B', 'success')] },
  { superseded_by: 'B', failed: false });
await run('head publishes a runtime image: stand down',
  { env: { ...base, COVERING_WORKFLOW: 'runtime-image.yml' }, head: 'B', runs: [ci(1, 'A', 'cancelled'), ci(2, 'B', 'success'), img('B')] },
  { superseded_by: 'B', failed: false });
await run('head publishes nothing, its CI passes: deploy this SHA',
  { env: { ...base, COVERING_WORKFLOW: 'runtime-image.yml' }, head: 'B', runs: [ci(1, 'A', 'cancelled'), ci(2, 'B', 'success')] },
  { superseded_by: '', failed: false });
await run('head publishes nothing, its CI fails: fail',
  { env: { ...base, COVERING_WORKFLOW: 'runtime-image.yml' }, head: 'B', runs: [ci(1, 'A', 'cancelled'), ci(2, 'B', 'failure')] },
  { superseded_by: '', failed: true });
await run('required jobs gate on the followed head',
  { env: { ...base, COVERING_WORKFLOW: 'runtime-image.yml', REQUIRED_JOBS: 'Engine tests' }, head: 'B',
    runs: [ci(1, 'A', 'cancelled'), { ...ci(2, 'B', null), status: 'in_progress' }],
    jobs: { 2: [{ name: 'Engine tests', status: 'completed', conclusion: 'success' }] } },
  { superseded_by: '', failed: false });
