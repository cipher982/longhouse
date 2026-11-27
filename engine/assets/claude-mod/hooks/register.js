// Longhouse owner input for a Claude Helm session.
//
// The session's owner can message it from the Longhouse app. Claude Code labels
// channel messages "NOT from your user", so this mod submits the owner's
// message as the user's own words ($.prompt.submit with asUser) and ends the
// running turn when the owner aborts it ($.turn.abort). Messages from other
// sessions never come this way: they stay on the channel, labelled as peers'.
//
// The mod also forwards the reply text Claude is streaming, as it arrives, to
// the `longhouse claude` launcher (engine/src/claude_helm_live.rs), which
// publishes it as the session's live preview. Claude writes an assistant line
// to its transcript only when the whole message has ended, and its `MessageDisplay`
// hook changes how Claude paints, so the `turn.step` stream is the source.
// Only reply text goes: thinking, tool names and tool arguments are dropped.
//
// It also lets the owner answer Claude's AskUserQuestion dialog from the app:
// the dialog stays in the terminal, and whichever answers first, the terminal
// or the app, wins. A message the owner sends while the dialog is open is their
// free-text answer.
//
// longhouse-engine owns the queue and the record of what was delivered
// (engine/src/claude_owner_input.rs). `claude-owner-input watch` streams this
// session's owner inputs as JSON lines; `claude-owner-input claim` takes one
// before it is submitted, so an input is never submitted twice or after the
// engine gave up on it; `claude-owner-input ack` records each later step.

const RESTART_DELAY_MS = 1000
const MAX_QUICK_RESTARTS = 5
const QUICK_RUN_MS = 10000
// How long a refused submit takes to say so; a queued one resolves only when
// its turn starts.
const REFUSAL_WINDOW_MS = 200

let engine
let sessionId
// This copy of the module. A question it opens is answerable only while its
// watcher is the session's current one: a reloaded mod has no open dialogs.
const instance = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10)
// The main loop's running turn, from turn.start to turn.complete.
let turnId
// The turn the owner aborted from Longhouse, until its turn.complete.
let abortedByOwner
// Inputs an earlier copy of this module submitted whose turn has not started:
// this copy records the delivery when it does.
const awaitingTurn = new Map()
// The owner's slash command this copy is running, until it either starts a
// model turn (a prompt command such as /init, a skill) or finishes without one.
let runningCommand

async function engineRun($, args, options) {
  try {
    return await $.process.run([engine, 'claude-owner-input', ...args, '--session', sessionId], options)
  } catch (error) {
    $.ui.log('longhouse: ' + String(error), { to: 'debug' })
    return { exitCode: -1, stdout: '', stderr: String(error) }
  }
}

function ack($, id, state, extra = {}) {
  const args = ['ack', '--id', id, '--state', state]
  if (extra.detail) args.push('--detail', extra.detail)
  if (extra.turnId) args.push('--turn-id', extra.turnId)
  return engineRun($, args)
}

async function claim($, id) {
  const result = await engineRun($, ['claim', '--id', id])
  return result.exitCode === 0
}

// `/name args`, as Claude Code reads a typed command. `//text` never gets here:
// the engine sends a message that starts with a slash through the channel.
const COMMAND = /^\s*\/([A-Za-z0-9_\-:.]+)(?:\s+([\s\S]*))?$/

async function send($, input) {
  const text = typeof input.text === 'string' ? input.text : ''
  if (input.state === 'claimed') {
    awaitingTurn.set(input.id, text)
    return
  }
  if (!(await claim($, input.id))) return
  if (text.trim() === '') {
    await ack($, input.id, 'failed', { detail: 'text is empty' })
    return
  }
  const command = COMMAND.exec(text)
  if (command) {
    await runCommand($, input.id, command[1], (command[2] || '').trim())
    return
  }
  if (text.trimStart().startsWith('/')) {
    // A slash the engine did not read as a command ($.prompt.submit refuses it).
    await ack($, input.id, 'failed', { detail: 'text that starts with / is not a command Claude Code knows' })
    return
  }
  let started
  try {
    started = $.prompt.submit({ text, asUser: true })
  } catch (error) {
    await ack($, input.id, 'failed', { detail: String(error) })
    return
  }
  // Resolves when the turn it opens starts: now when idle, after the running
  // turn otherwise. A refusal rejects at once, so it is awaited briefly before
  // the input is reported submitted, and its outcome is recorded after that.
  let refused
  const outcome = started.then(
    () => undefined,
    (error) => {
      refused = error
    },
  )
  await Promise.race([outcome, new Promise((resolve) => $.clock.after(REFUSAL_WINDOW_MS, resolve))])
  if (refused !== undefined) {
    await ack($, input.id, 'failed', { detail: String(refused) })
    return
  }
  await ack($, input.id, 'submitted')
  outcome.then(() =>
    refused === undefined
      ? ack($, input.id, 'delivered', { turnId })
      : ack($, input.id, 'failed', { detail: String(refused) }),
  )
}

// Runs the owner's slash command as if they typed it ($.command.run, queued
// until the session is idle). A command that opens a model turn (a prompt
// command, a skill) is delivered when that turn starts; one that does its work
// without the model (/model x, /rename) is `command_ran` when it returns, so
// Longhouse frees the session at once instead of waiting for a turn end.
async function runCommand($, id, name, args) {
  let known
  try {
    const listed = await $.command.list()
    if (Array.isArray(listed)) known = listed.some((c) => c && c.name === name)
  } catch (error) {
    await ack($, id, 'failed', {
      detail: 'this Claude Code cannot run a command for the Longhouse app (' + String(error) + '); update Claude Code',
    })
    return
  }
  if (known === false) {
    await ack($, id, 'failed', { detail: 'Claude Code has no /' + name + ' command in this session' })
    return
  }
  const running = { id, settled: false }
  runningCommand = running
  try {
    await $.command.run({ command: name, args })
  } catch (error) {
    if (runningCommand === running) runningCommand = undefined
    if (!running.settled) {
      running.settled = true
      await ack($, id, 'failed', { detail: 'Claude Code could not run /' + name + ': ' + String(error) })
    }
    return
  }
  if (runningCommand === running) runningCommand = undefined
  if (!running.settled) {
    running.settled = true
    await ack($, id, 'command_ran', { detail: name })
  }
}

async function abort($, input) {
  if (input.state === 'claimed') return
  if (!(await claim($, input.id))) return
  const running = turnId
  if (running === undefined) {
    await ack($, input.id, 'no_turn')
    return
  }
  // Set before the call: turn.complete can fire before it returns.
  abortedByOwner = running
  try {
    await $.turn.abort({ turnId: running })
    await ack($, input.id, 'aborted', { turnId: running })
  } catch (error) {
    abortedByOwner = undefined
    // The turn ended between the check and the call.
    if (turnId === undefined) await ack($, input.id, 'no_turn')
    else await ack($, input.id, 'failed', { detail: String(error) })
  }
}

// What Esc leaves behind, which `$.turn.abort` does not (Claude Code 2.1.296):
// the user row that tells the model its turn was interrupted.
const INTERRUPTED = '[Request interrupted by user]'

// An interrupted turn runs no Stop hook, so Longhouse would keep showing the
// session as working. Report the turn's end through Longhouse's own lifecycle
// hook, as Stop reports an answered one.
// Turns whose end was already reported: one Stop per turn, whatever fires twice.
const reportedTurns = new Set()
const MOD_SNAPSHOT_DEBOUNCE_MS = 500
let lastSnapshotAt = 0
let snapshotTimer
let snapshotPending = false

// Claude Code checks a mod's source before it loads it and refuses the whole
// module when `$.agent` or `$.session` is read as a value (Claude Code 2.1.322:
// "$.agent is used as a value"). So no feature test here: each noun is only ever
// called, and a Claude Code without the method throws into the catch.
async function readModSnapshot($) {
  let agents
  let sessionUsage
  try {
    const list = await $.agent.list()
    if (Array.isArray(list)) agents = list
  } catch (error) {
    $.ui.log('longhouse: agent.list unavailable: ' + String(error), { to: 'debug' })
  }
  try {
    const usage = await $.session.usage()
    if (usage && typeof usage === 'object') sessionUsage = usage
  } catch (error) {
    $.ui.log('longhouse: session.usage unavailable: ' + String(error), { to: 'debug' })
  }
  return { agents, sessionUsage }
}

async function emitModSnapshot($, trigger) {
  if (!engine) return
  const { agents, sessionUsage } = await readModSnapshot($)
  if (agents === undefined && sessionUsage === undefined) return
  try {
    const payload = {
      hook_event_name: 'ClaudeModState',
      trigger,
      session_id: await $.session.id(),
      cwd: await $.session.cwd(),
    }
    if (agents !== undefined) payload.agents = agents
    if (sessionUsage !== undefined) payload.session_usage = sessionUsage
    await $.process.run([engine, 'claude-lifecycle-hook'], { stdin: JSON.stringify(payload) })
  } catch (error) {
    $.ui.log('longhouse: mod snapshot not reported: ' + String(error), { to: 'debug' })
  }
}

function scheduleSnapshot($, trigger) {
  if (!engine) return
  const now = Date.now()
  const elapsed = now - lastSnapshotAt
  if (lastSnapshotAt === 0 || elapsed >= MOD_SNAPSHOT_DEBOUNCE_MS) {
    lastSnapshotAt = now
    if (snapshotTimer) {
      snapshotTimer.cancel?.()
      snapshotTimer = undefined
    }
    snapshotPending = false
    return emitModSnapshot($, trigger)
  }
  snapshotPending = true
  if (snapshotTimer) return
  snapshotTimer = $.clock.after(MOD_SNAPSHOT_DEBOUNCE_MS - elapsed, async () => {
    snapshotTimer = undefined
    if (!snapshotPending) return
    snapshotPending = false
    lastSnapshotAt = Date.now()
    await emitModSnapshot($, trigger)
  })
}

async function reportTurnEnd($, turn, aborted = false) {
  if (reportedTurns.has(turn)) return
  reportedTurns.add(turn)
  try {
    const { agents, sessionUsage } = await readModSnapshot($)
    const payload = {
      hook_event_name: 'Stop',
      session_id: await $.session.id(),
      cwd: await $.session.cwd(),
      stop_hook_active: false,
    }
    if (aborted) payload.aborted = true
    if (agents !== undefined) payload.agents = agents
    if (sessionUsage !== undefined) payload.session_usage = sessionUsage
    await $.process.run([engine, 'claude-lifecycle-hook'], { stdin: JSON.stringify(payload) })
  } catch (error) {
    $.ui.log('longhouse: turn end not reported: ' + String(error), { to: 'debug' })
  }
}

// ---- Questions answered from Longhouse ----------------------------------
//
// tool_use_id -> { settle, decided }, for each AskUserQuestion dialog this
// copy has open. `decided` is set the moment either side answers, so an app
// answer that arrives after the terminal's is refused, never reported answered.
const openQuestions = new Map()
// Set when the watcher gave up: questions stay in the terminal from then on.
let watcherStopped = false

async function answer($, input) {
  if (input.state === 'claimed') {
    // A question this copy holds is this copy's to report on.
    if (openQuestions.has((input.answer || {}).tool_use_id)) return
    // An earlier copy took it and reloaded before answering: its dialog is gone.
    await ack($, input.id, 'failed', { detail: 'the Longhouse mod reloaded before answering' })
    return
  }
  if (!(await claim($, input.id))) return
  const reply = input.answer || {}
  const open = openQuestions.get(reply.tool_use_id)
  if (open === undefined || open.decided) {
    await ack($, input.id, 'failed', { detail: 'the question was already answered in the terminal' })
    return
  }
  open.decided = true
  open.settle(reply)
  await ack($, input.id, 'answered')
}

// Longhouse learns a question closed from Claude's PostToolUse hook. A call the
// mod answered may never reach that hook, so it says so itself; a repeat is
// ignored.
async function reportQuestionAnswered($, toolUseId, questions) {
  try {
    const payload = {
      hook_event_name: 'PostToolUse',
      session_id: await $.session.id(),
      cwd: await $.session.cwd(),
      tool_name: 'AskUserQuestion',
      tool_use_id: toolUseId,
      tool_input: { questions },
    }
    await $.process.run([engine, 'claude-lifecycle-hook'], { stdin: JSON.stringify(payload) })
  } catch (error) {
    $.ui.log('longhouse: answered question not reported: ' + String(error), { to: 'debug' })
  }
}

// Races the terminal dialog (`next(e)`) against an answer from the app. The
// question is recorded before the dialog opens, so Claude's PreToolUse hook,
// which runs beneath this one, tells Longhouse the app may answer it. When the
// app answers first this hook settles, which abandons the dialog beneath it.
async function askUserQuestion($, e, next) {
  // Without a running watcher no answer from the app could reach the dialog.
  if (!engine || !sessionId || watcherStopped || e.agentId !== undefined || typeof e.tool_use_id !== 'string') {
    return next(e)
  }
  const toolUseId = e.tool_use_id
  const questions = Array.isArray(e.questions) ? e.questions : []
  const open = { decided: false, settle: undefined }
  const fromApp = new Promise((resolve) => {
    open.settle = resolve
  })
  openQuestions.set(toolUseId, open)
  try {
    const opened = await engineRun($, ['question-open', '--tool-use-id', toolUseId, '--instance', instance], {
      stdin: JSON.stringify(questions),
    })
    if (opened.exitCode !== 0) {
      open.decided = true
      $.ui.log('longhouse: question not offered to the app: ' + opened.stderr, { to: 'debug' })
      return await next(e)
    }
    const inTerminal = next(e)
    inTerminal.catch(() => {})
    const won = await Promise.race([
      inTerminal.then(
        (result) => {
          open.decided = true
          return { from: 'terminal', result }
        },
        (error) => {
          open.decided = true
          throw error
        },
      ),
      fromApp.then((reply) => ({ from: 'app', reply })),
    ])
    if (won.from === 'terminal') return won.result
    const reply = won.reply
    // A decline comes only from the app's answer route, which records it as
    // declined itself; reporting it here would record it as answered.
    if (reply.declined) return { deny: 'The user declined to answer this question from the Longhouse app.' }
    void reportQuestionAnswered($, toolUseId, questions)
    const result = { questions, answers: reply.answers || {} }
    if (typeof reply.response === 'string' && reply.response !== '') result.response = reply.response
    return { result }
  } finally {
    openQuestions.delete(toolUseId)
    // Awaited, so an owner message sent right after the answer is a message.
    await engineRun($, ['question-close', '--tool-use-id', toolUseId])
  }
}

async function handle($, line) {
  let input
  try {
    input = JSON.parse(line)
  } catch (error) {
    $.ui.log('longhouse: unreadable owner input line', { to: 'debug' })
    return
  }
  if (input.kind === 'send') await send($, input)
  else if (input.kind === 'abort') await abort($, input)
  else if (input.kind === 'answer') await answer($, input)
}

async function watchOnce($) {
  const stream = $.process.spawn({
    argv: [engine, 'claude-owner-input', 'watch', '--session', sessionId, '--instance', instance],
  })
  let buffered = ''
  for await (const { stream: pipe, text } of stream) {
    if (pipe !== 'stdout') {
      $.ui.log('longhouse: ' + text, { to: 'debug' })
      continue
    }
    buffered += text
    let end = buffered.indexOf('\n')
    while (end >= 0) {
      const line = buffered.slice(0, end).trim()
      buffered = buffered.slice(end + 1)
      if (line !== '') await handle($, line)
      end = buffered.indexOf('\n')
    }
  }
}

// The watcher runs for the session's life, from a timer so that it runs outside
// any event. If it exits it is started again; one that keeps failing at once is
// left stopped, and the engine, seeing no watcher, sends the owner's messages
// through the channel and says so.
function watch($, quickRestarts) {
  $.clock.after(quickRestarts === 0 ? 0 : RESTART_DELAY_MS, async () => {
    const began = await $.clock.now()
    try {
      await watchOnce($)
    } catch (error) {
      $.ui.log('longhouse: owner input watcher stopped: ' + String(error), { to: 'debug' })
    }
    const quick = (await $.clock.now()) - began < QUICK_RUN_MS
    const restarts = quick ? quickRestarts + 1 : 0
    if (restarts < MAX_QUICK_RESTARTS) watch($, restarts)
    else {
      watcherStopped = true
      $.ui.status('owner messages from the Longhouse app now arrive through its channel')
    }
  })
}

// ---- Live reply text ----------------------------------------------------
//
// Runs inside the person's Claude process, in the path of every streamed
// piece, so each piece costs one string append and nothing else: the pieces are
// passed on unchanged and unawaited, nothing here throws into Claude, and
// every failure ends in silence. The launcher listens on a Unix socket named by
// LONGHOUSE_CLAUDE_LIVE_SOCKET; without it (or after repeated refusals) the
// mod forwards nothing.

// A new send waits this long after the last one began to finish, so a fast
// model costs about ten posts a second, not one per token.
const LIVE_SPACING_MS = 100
const LIVE_MAX_FAILURES = 3

let liveSocket
// Set once a reply text has been forwarded: a turn end means nothing to the
// launcher before that, and a Claude Code without `turn.step` never gets here.
let liveSeen = false
let liveBusy = false
let liveFailures = 0
// Not yet sent, oldest first: { turn, step, text } for reply text, { turn,
// done: true } when a turn ends. Text of one step is joined into one entry.
const liveQueue = []

function liveEnqueue($, entry) {
  if (liveSocket === undefined) return
  if (!entry.done) liveSeen = true
  const last = liveQueue[liveQueue.length - 1]
  const same =
    last !== undefined &&
    last.turn === entry.turn &&
    last.step === entry.step &&
    last.kind === entry.kind &&
    last.index === entry.index
  if (same && entry.text !== undefined && last.text !== undefined) {
    last.text += entry.text
  } else if (same && entry.json !== undefined && last.json !== undefined) {
    last.json += entry.json
  } else {
    liveQueue.push(entry)
  }
  liveKick($)
}

function liveKick($) {
  if (liveSocket === undefined || liveBusy || liveQueue.length === 0) return
  liveBusy = true
  void liveSend($)
}

async function liveSend($) {
  const batch = liveQueue.splice(0)
  try {
    const response = await $.http.fetch('http://longhouse.invalid/live', {
      method: 'POST',
      body: JSON.stringify(batch),
      socketPath: liveSocket,
    })
    if (!response.ok) throw new Error('status ' + response.status)
    liveFailures = 0
  } catch (error) {
    liveFailures += 1
    if (liveFailures >= LIVE_MAX_FAILURES) {
      liveSocket = undefined
      liveQueue.length = 0
    }
    try {
      $.ui.log('longhouse: live text not delivered: ' + String(error), { to: 'debug' })
    } catch (ignored) {}
  }
  try {
    $.clock.after(LIVE_SPACING_MS, () => {
      liveBusy = false
      liveKick($)
    })
  } catch (error) {
    liveBusy = false
  }
}

// Passes every chunk on as it came. The main loop's reply text, thinking text
// the start of each tool call (its id and name) and the call's argument JSON are
// forwarded, each saying what it is; `stop` chunks are not. Chunks are only read: the
// terminal paints exactly what it painted before.
async function* liveTurnStep($, e, next) {
  if (liveSocket === undefined || e.agentId !== undefined) return yield* next(e)
  for await (const chunk of next(e)) {
    try {
      if (chunk.kind === 'text' && typeof chunk.text === 'string' && chunk.text !== '') {
        liveEnqueue($, { turn: e.turnId, step: e.index, text: chunk.text })
      } else if (chunk.kind === 'thinking' && typeof chunk.text === 'string' && chunk.text !== '') {
        liveEnqueue($, { turn: e.turnId, step: e.index, kind: 'thinking', text: chunk.text })
      } else if (
        chunk.kind === 'tool' &&
        typeof chunk.id === 'string' &&
        typeof chunk.name === 'string' &&
        chunk.name !== ''
      ) {
        liveEnqueue($, { turn: e.turnId, step: e.index, kind: 'tool', index: chunk.index, id: chunk.id, name: chunk.name })
      } else if (chunk.kind === 'input' && typeof chunk.json === 'string' && chunk.json !== '') {
        liveEnqueue($, { turn: e.turnId, step: e.index, kind: 'input', index: chunk.index, json: chunk.json })
      }
    } catch (ignored) {}
    yield chunk
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    engine = await $.env.get('LONGHOUSE_OWNER_INPUT_ENGINE')
    sessionId = await $.env.get('LONGHOUSE_CHANNEL_SESSION_ID')
    if (engine && sessionId) watch($, 0)
    try {
      const socket = await $.env.get('LONGHOUSE_CLAUDE_LIVE_SOCKET')
      if (socket) {
        liveSocket = socket
      }
    } catch (error) {
      $.ui.log('longhouse: live text unavailable: ' + String(error), { to: 'debug' })
    }
    return started
  })

  // A Claude Code without `turn.step` has no such event to subscribe to: the
  // mod then loads and forwards nothing, and owner input works as before.
  try {
    on('turn.step', liveTurnStep)
  } catch (error) {}

  on('tool.call', { tool: 'AskUserQuestion' }, askUserQuestion)

  on('turn.start', async ($, e, next) => {
    if (e.agentId !== undefined) {
      void scheduleSnapshot($, 'turn.start')
      return next(e)
    }
    turnId = e.turnId
    const command = runningCommand
    if (command && !command.settled) {
      // The owner's command opened this turn.
      command.settled = true
      runningCommand = undefined
      void ack($, command.id, 'delivered', { turnId: e.turnId })
    }
    for (const [id, text] of awaitingTurn) {
      if (text === e.text) {
        awaitingTurn.delete(id)
        void ack($, id, 'delivered', { turnId: e.turnId })
        break
      }
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const completed = await next(e)
    if (e.agentId !== undefined) {
      void scheduleSnapshot($, 'turn.complete')
      return completed
    }
    if (liveSeen) liveEnqueue($, { turn: e.turnId, done: true })
    if (e.turnId === turnId) turnId = undefined
    const ownerAborted = e.turnId === abortedByOwner
    if (ownerAborted) abortedByOwner = undefined
    if (e.reason === 'aborted' && engine) {
      if (ownerAborted) {
        try {
          await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: INTERRUPTED }] } })
        } catch (error) {
          $.ui.log('longhouse: interrupted row not recorded: ' + String(error), { to: 'debug' })
        }
      }
      await reportTurnEnd($, e.turnId, true)
    } else {
      void scheduleSnapshot($, 'turn.complete')
    }
    return completed
  })

}
