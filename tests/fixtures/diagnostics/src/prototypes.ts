/** Opt-in P0 experiments. Never part of the production installation set. */
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-commands'
import type {} from '@deepseek-ai/dsh-jobs'
import type {} from '@deepseek-ai/dsh-shell'
import { ScheduleId } from '@deepseek-ai/dsh-schedule'
import { SessionId } from '@deepseek-ai/dsh-session'
import { ToolCallId, MessageId, freezeMessage } from '@deepseek-ai/dsh-llm'
import { receiptPreset } from '@telebot/core/presets'
import { defineTool } from '@deepseek-ai/dsh-tools'

export function installPrototypes(ctx: Context): void {
  const tasks = new Map<string, string>()
  ctx.tools.register(defineTool({
    name: 'telebot_p0_probe',
    description: 'Isolated P0 acceptance only; invokes official commands, tools and schedule services.',
    parameters: {
      action: { type: 'string', enum: ['commands', 'scope', 'cold-receipt', 'schedule-create', 'schedule-read', 'schedule-delete', 'schedule-fault'] },
      id: { type: 'string' },
      delay: { type: 'integer' },
    },
    output: {
      schema: { type: 'object', properties: { json: { type: 'string', required: true } }, additionalProperties: false },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(args, exec) {
      assert(exec.agent, 'A real official Agent must own every probe')
      const agent = exec.agent
      let value: unknown
      if (args.action === 'commands') value = await commandProbe(ctx, agent, exec.signal)
      else if (args.action === 'scope') value = await scopeProbe(ctx, agent, exec.signal)
      else if (args.action === 'cold-receipt') value = await coldReceiptProbe(ctx, agent, exec.signal)
      else if (args.action === 'schedule-create' || args.action === 'schedule-fault') {
        let fault: (() => void) | undefined
        if (args.action === 'schedule-fault') {
          // Simulate the documented inbox-flush/task-commit crash window through
          // the public persistence checkpoint. Other persistence listeners still run.
          fault = ctx.on('session/flush', session => {
            if (session.id !== agent.session.id) return
            const events = session.snapshotEvents()
            const scheduled = events.some(event => event.type === 'agent/inbox/spliced' && event.data.inserted.some(message =>
              (message.source as { kind: string })?.kind === 'schedule' && JSON.stringify(message.content).includes('P0 checkpoint fault')))
            if (!scheduled) return
            fault?.(); fault = undefined
            throw new Error('P0 injected checkpoint failure after inbox admission')
          })
        }
        try {
          const record = await ctx.schedule.create(agent.session.id, {
            title: 'P0 isolated reminder', prompt: args.action === 'schedule-fault' ? 'P0 checkpoint fault' : 'P0 reminder only',
            after_seconds: Math.max(1, Math.min(30, args.delay ?? 2)),
          }, exec.signal)
          tasks.set(record.id, agent.session.id)
          value = { record }
        } catch (error) { fault?.(); throw error }
      } else {
        assert(args.id, 'An explicit official schedule id is required')
        const id = ScheduleId(args.id)
        const sessionId = SessionId(tasks.get(id) ?? agent.session.id)
        const catalog = await ctx.schedule.catalog()
        const task = catalog.find(row => row.id === id && row.sessionId === sessionId)
        assert(!task || task.title === 'P0 isolated reminder', 'Never touch unrelated reminders')
        if (args.action === 'schedule-delete') {
          const deleted = await ctx.schedule.delete({ id, sessionId }, exec.signal)
          const history = await ctx.schedule.history({ id, sessionId, limit: 10 })
          value = { deleted, history }
        } else {
          assert.equal(args.action, 'schedule-read')
          const history = await ctx.schedule.history({ id, sessionId, limit: 10 })
          const events = (ctx.agents.get(sessionId) ?? agent).session.snapshotEvents()
          const scheduleEvents = events.filter(event => event.type === 'agent/inbox/spliced' || event.type === 'user/message')
          value = { task: task ?? null, history, scheduleEvents }
        }
      }
      return { json: JSON.stringify(value) }
    },
  }))
}

async function commandProbe(ctx: Context, agent: Agent, signal: AbortSignal) {
  let calls = 0
  const unregister = ctx.commands.register({
    name: 'telebot-p0', description: 'P0 local command; never calls a model',
    handler: invocation => { calls++; return { kind: 'success', text: invocation.rawInput } },
  })
  // The fixture owns this trusted admission context; identity is deliberately
  // not taken from the slash-command text or model-provided JSON.
  const dispatch = async (trustedActor: string, line: string) => {
    if (trustedActor !== 'p0-admin') return { rejected: true }
    if (!/^\/telebot-p0(?:\s|$)/.test(line)) return { rejected: true }
    return ctx.commands.execute(agent, line, [], signal)
  }
  try {
    assert(ctx.commands.list(agent).some(row => row.name === 'telebot-p0'))
    assert(ctx.commands.find(agent, 'telebot-p0'))
    const before = agent.session.snapshotEvents().filter(row => row.type === 'command/run').length
    assert.deepEqual(await dispatch('stranger', '/telebot-p0 {"actor":"p0-admin"}'), { rejected: true })
    assert.deepEqual(await dispatch('p0-admin', '/unknown-p0'), { rejected: true })
    assert.equal(calls, 0)
    const execution = await dispatch('p0-admin', '/telebot-p0  exact input')
    assert(execution && 'result' in execution && execution.result.kind === 'success')
    assert.equal(execution.result.text, '  exact input')
    const events = agent.session.snapshotEvents()
    assert.equal(events.filter(row => row.type === 'command/run').length, before + 1)
    assert(events.some(row => row.type === 'command/done' && row.data.commandId === execution.commandId))
    assert.equal(calls, 1)
    await ctx.sessions.flush(agent.session)
    return { admitted: calls, rejected: 2, nativeLifecycle: true }
  } finally { unregister(); assert.equal(ctx.commands.find(agent, 'telebot-p0'), undefined) }
}

async function scopeProbe(ctx: Context, parent: Agent, signal: AbortSignal) {
  const handle = await ctx.agents.create({
    sessionId: SessionId(randomUUID()), meta: { cwd: parent.session.header.cwd },
    agentOptions: {provider:parent.options.provider,model:parent.options.model},
    setup(scope) { scope.inject(['tools'], child => { child.tools.restrict({ allow: ['telebot_echo'] }) }) },
  })
  try {
    const names = ['pwsh', 'bash', 'subagent', 'subagent_codex', 'schedule', 'schedule_create']
    for (const name of names) {
      assert.equal(ctx.tools.get(name, handle.agent), undefined)
      const dispatched = await ctx.tools.execute({
        agent: handle.agent, signal,
        callId:ToolCallId(randomUUID()),name,arguments:{},
      })
      assert(dispatched.isError,'Direct dispatch must not bypass '+name+' restriction')
    }
    assert(ctx.tools.get('telebot_echo', handle.agent))
    assert(ctx.tools.get(process.platform === 'win32' ? 'pwsh' : 'bash', parent))
    return { deniedDirectEntrypoints: names, parentUnaffected: true }
  } finally { await handle.dispose() }
}
async function coldReceiptProbe(ctx: Context, parent: Agent, signal: AbortSignal) {
  const registry=ctx.get('agentPresets') as any,controller=ctx.get('sessionController') as any
  assert(registry&&controller,'Official rc.5 preset registry and controller are required')
  const preset='telebot-cold-'+randomUUID(),id=SessionId(randomUUID())
  const remove=await receiptPreset(ctx,preset,['telebot_echo'])
  try{
    const handle=await ctx.agents.create({sessionId:id,meta:{cwd:parent.session.header.cwd,agentPreset:preset},agentOptions:{provider:parent.options.provider,model:parent.options.model},setup:async scope=>{await registry.mount(scope,preset)}})
    await ctx.sessions.flush(handle.agent.session);await handle.dispose();assert.equal(ctx.agents.get(id),undefined)
    const restored=await controller.resolveAgent(id);if('error'in restored)throw restored.error
    assert(ctx.tools.get('telebot_echo',restored.agent))
    const denied=['pwsh','bash','subagent','schedule_create','telebot_schedule','telebot_publish']
    for(const name of denied){assert.equal(ctx.tools.get(name,restored.agent),undefined);assert((await ctx.tools.execute({agent:restored.agent,signal,callId:ToolCallId(randomUUID()),name,arguments:{}})).isError)}
    restored.agent.followup(freezeMessage({id:MessageId(randomUUID()),role:'user',source:{kind:'user'},content:[{type:'text',text:'Synthetic wake must never call a model or execute business actions.'}]}))
    await restored.agent.whenIdle();await ctx.sessions.flush(restored.agent.session)
    return{coldRestored:true,deniedDirectEntrypoints:denied,inert:true}
  }finally{await remove()}
}
