import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import path from 'node:path'

/** Real Git Smart HTTP against one synthetic bare repository. No user remotes. */
export async function startMockGitHttp(repository, env) {
  const children = new Set()
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://localhost')
    if (!/^\/remote\.git\/(?:info\/refs|git-receive-pack)$/.test(url.pathname)) return response.writeHead(404).end()
    const body = []; let bytes = 0
    for await (const chunk of request) { bytes += chunk.length; if (bytes > 4194304) return response.writeHead(413).end(); body.push(chunk) }
    const child = spawn('git', ['http-backend'], { windowsHide: true, env: { ...env,
      GIT_PROJECT_ROOT: path.dirname(repository), GIT_HTTP_EXPORT_ALL: '1',
      PATH_INFO: url.pathname, QUERY_STRING: url.search.slice(1), REQUEST_METHOD: request.method,
      CONTENT_TYPE: request.headers['content-type'] ?? '', CONTENT_LENGTH: String(bytes),
      REMOTE_ADDR: '127.0.0.1', REMOTE_USER: 'synthetic-acceptance', GIT_PROTOCOL: request.headers['git-protocol'] ?? '',
    }, stdio: ['pipe','pipe','pipe'] })
    children.add(child); const output=[]
    child.stdout.on('data', chunk=>output.push(chunk)); child.stderr.resume()
    child.once('error',()=>{if(!response.headersSent)response.writeHead(502).end()})
    child.once('exit', code=>{
      children.delete(child);if(response.headersSent)return
      const raw=Buffer.concat(output), boundary=raw.indexOf('\r\n\r\n')
      if(code!==0||boundary<0)return response.writeHead(502).end()
      let status=200;const headers={}
      for(const line of raw.subarray(0,boundary).toString('utf8').split('\r\n')) {
        const index=line.indexOf(':');if(index<0)continue
        const key=line.slice(0,index),value=line.slice(index+1).trim()
        if(key.toLowerCase()==='status')status=Number(value.split(' ')[0]);else headers[key]=value
      }
      response.writeHead(status,headers).end(raw.subarray(boundary+4))
    })
    child.stdin.end(Buffer.concat(body))
  })
  server.listen(0,'127.0.0.1');await once(server,'listening')
  return {url:'http://127.0.0.1:'+server.address().port+'/remote.git',async close(){for(const child of children)child.kill();server.closeAllConnections();await new Promise(resolve=>server.close(resolve))}}
}
