import { createServer } from 'node:http'
import { once } from 'node:events'

function text(content) {
  if (typeof content === 'string') return content
  return (content ?? []).map(part => part.text ?? '').join('\n')
}

/** Deterministic loopback model; records actual tool receipts and can hold a request for cancellation. */
export async function startMockModel({ deliveryToolProbe = false } = {}) {
  const requests = []
  const toolCases = new Map()
  const intentReplies=new Map()
  const waiters = new Set()
  const heldResponses = new Set()
  const server = createServer(async (request, response) => {
    if (request.url !== '/v1/chat/completions') { response.writeHead(404).end(); return }
    let raw = ''
    for await (const chunk of request) raw += chunk
    let body
    try { body = JSON.parse(raw) } catch { response.writeHead(400).end(); return }
    const messages = body.messages ?? []
    let testIndex = messages.findLastIndex(message => message.role === 'user' && text(message.content).startsWith('TELEBOT_TEST:'))
    const deliveryIndex = deliveryToolProbe ? messages.findLastIndex(message => message.role === 'user' && text(message.content).includes('TELEBOT_TEST:NATIVE_UNTRUSTED_DELIVERY')) : -1
    if(deliveryIndex>testIndex)testIndex=deliveryIndex
    const semantic=messages.some(message=>message.role==='system'&&text(message.content).includes('TELEBOT_CONFIRMATION_INTENT_V1'))
    const intentInput=semantic?JSON.parse(text(messages.findLast(message=>message.role==='user').content)):undefined
    const instruction = semantic?'CONFIRMATION_INTENT':deliveryIndex===testIndex&&deliveryIndex>=0 ? 'NATIVE_UNTRUSTED_DELIVERY' : testIndex < 0 ? '' : text(messages[testIndex].content).slice('TELEBOT_TEST:'.length)
    const names = (body.tools ?? []).map(tool => tool.function?.name)
    const tool = messages.slice(testIndex + 1).findLast(message => message.role === 'tool')
    const receipt = tool ? text(tool.content) : undefined
    const entry = { body, model: body.model, names, instruction, receipt }
    requests.push(entry)
    for (const waiter of [...waiters]) {
      if (waiter.predicate(entry)) { waiters.delete(waiter); waiter.resolve(entry) }
    }
    if (instruction.includes('WAIT_FOR_CANCEL:')) {
      heldResponses.add(response)
      response.once('close', () => heldResponses.delete(response))
      return
    }
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })
    const emit = (delta, finish_reason = null) => response.write('data: ' + JSON.stringify({
      id: 'telebot-keyless-completion', object: 'chat.completion.chunk', created: 1,
      model: body.model, choices: [{ index: 0, delta, finish_reason }],
    }) + '\n\n')
    if(instruction==='THINKING_ONLY'){
      emit({role:'assistant',reasoning_content:'PRIVATE_REASONING_FIXTURE'})
      await new Promise(resolve=>setTimeout(resolve,1200))
      emit({reasoning_content:'PRIVATE_REASONING_FIXTURE'})
    }
    const firstCase = toolCases.get(instruction.split(':')[0])
    const nextCase = receipt && firstCase?.then && !messages.slice(testIndex + 1).some(message => message.role === 'assistant' && message.tool_calls?.some(call => call.function?.name === firstCase.then.name)) ? firstCase.then : undefined
    const toolCase = nextCase || firstCase
    if(semantic){
      emit({role:'assistant',content:JSON.stringify(intentReplies.get(intentInput.reply)??{decision:'other',selection:null})});emit({},'stop')
    }else if ((!receipt || nextCase) && toolCase && names.includes(toolCase.name)) {
      emit({ role: 'assistant', tool_calls: [{ index: 0, id: nextCase ? 'probe-next-call' : 'probe-call', type: 'function',
        function: { name: toolCase.name, arguments: JSON.stringify(toolCase.arguments) } }] })
      emit({}, 'tool_calls')
    } else if (!receipt && instruction.includes('ECHO:') && names.includes('telebot_echo')) {
      const token = instruction.match(/ECHO:([^\s]+)/)?.[1]
      emit({ role: 'assistant', tool_calls: [{ index: 0, id: 'echo-call', type: 'function',
        function: { name: 'telebot_echo', arguments: JSON.stringify({ text: token }) } }] })
      emit({}, 'tool_calls')
    } else {
      emit({ role: 'assistant', content: receipt ? `Receipt received: ${receipt}` : 'Keyless response complete.' })
      emit({}, 'stop')
    }
    response.end('data: [DONE]\n\n')
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  return {
    requests, toolCases,intentReplies, url: `http://127.0.0.1:${server.address().port}/v1`,
    waitFor(predicate) {
      const found = requests.find(predicate)
      if (found) return Promise.resolve(found)
      return new Promise(resolve => { waiters.add({ predicate, resolve }) })
    },
    async close() {
      for (const response of heldResponses) response.destroy()
      server.closeAllConnections()
      await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
    },
  }
}
