// Longhouse owner input for a Claude Helm session.
//
// The session's owner can message it from the Longhouse app. Claude Code labels
// channel messages "NOT from your user", so this mod submits the owner's
// message as the user's own words ($.prompt.submit with asUser) and ends the
// running turn when the owner aborts it ($.turn.abort). Messages from other
// sessions never come this way: they stay on the channel, labelled as peers'.
//
// longhouse-engine owns the queue and the record of what was delivered
// (engine/src/claude_owner_input.rs). `claude-owner-input watch` streams this
// session's owner inputs as JSON lines; `claude-owner-input claim` takes one
// before it is submitted, so an input is never submitted twice or after the
// engine gave up on it; `claude-owner-input ack` records each later step.

const RESTART_DELAY_MS = 1000
const MAX_QUICK_RESTARTS = 5
const QUICK_RUN_MS = 10000

let engine
let sessionId
// The main loop's running turn, from turn.start to turn.complete.
let turnId
// Inputs an earlier copy of this module submitted whose turn has not started:
// this copy records the delivery when it does.
const awaitingTurn = new Map()

async function engineRun($, args) {
  try {
    return await $.process.run([engine, 'claude-owner-input', ...args, '--session', sessionId])
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

async function send($, input) {
  const text = typeof input.text === 'string' ? input.text : ''
  if (input.state === 'claimed') {
    awaitingTurn.set(input.id, text)
    return
  }
  if (!(await claim($, input.id))) return
  // $.prompt.submit refuses an empty text and one that starts with a slash
  // (it would run a command); the engine refuses both before they get here.
  if (text.trim() === '' || text.trimStart().startsWith('/')) {
    await ack($, input.id, 'failed', { detail: 'text is empty or starts with a slash' })
    return
  }
  let started
  try {
    started = $.prompt.submit({ text, asUser: true })
  } catch (error) {
    await ack($, input.id, 'failed', { detail: String(error) })
    return
  }
  await ack($, input.id, 'submitted')
  // Resolves when the turn it opens starts: now when idle, after the running
  // turn otherwise.
  started.then(
    () => ack($, input.id, 'delivered', { turnId }),
    (error) => ack($, input.id, 'failed', { detail: String(error) }),
  )
}

async function abort($, input) {
  if (input.state === 'claimed') return
  if (!(await claim($, input.id))) return
  const running = turnId
  if (running === undefined) {
    await ack($, input.id, 'no_turn')
    return
  }
  try {
    await $.turn.abort({ turnId: running })
    await ack($, input.id, 'aborted', { turnId: running })
  } catch (error) {
    // The turn ended between the check and the call.
    if (turnId === undefined) await ack($, input.id, 'no_turn')
    else await ack($, input.id, 'failed', { detail: String(error) })
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
}

async function watchOnce($) {
  const stream = $.process.spawn({ argv: [engine, 'claude-owner-input', 'watch', '--session', sessionId] })
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
    else $.ui.status('owner messages from the Longhouse app now arrive through its channel')
  })
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    engine = await $.env.get('LONGHOUSE_OWNER_INPUT_ENGINE')
    sessionId = await $.env.get('LONGHOUSE_CHANNEL_SESSION_ID')
    if (engine && sessionId) watch($, 0)
    return started
  })

  on('turn.start', async ($, e, next) => {
    turnId = e.turnId
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
    if (e.agentId === undefined && e.turnId === turnId) turnId = undefined
    return next(e)
  })
}
