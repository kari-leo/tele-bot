import test from 'node:test'
import assert from 'node:assert/strict'
import { Resources } from '../../plugins/feishu/lib/business/domain/resources.js'
const row=(alias,tableId,name)=>({alias,tableId,name,kind:'bitable',id:'base-fixture',identity:'tenant',tokenRef:'FEISHU_APP_TENANT',expiresAt:0,capabilities:['bitable.read']})
test('configured table display names resolve exactly, with ambiguity rejected and no permission expansion',()=>{
  const todo=row('base-table1','table1','Todo'),resources=new Resources([todo],['alice'],()=>{throw Error('no network')})
  assert.equal(resources.configured('todo'),todo);assert.equal(resources.configured('  TODO  '),todo)
  assert.equal(resources.configured('base-table1'),todo)
  assert.throws(()=>resources.configured('unregistered'),/unknown_resource/)
  const duplicate=new Resources([todo,row('base-table2','table2','Todo')],['alice'],()=>{throw Error('no network')})
  assert.throws(()=>duplicate.configured('todo'),/ambiguous_resource/)
  const namedBase=new Resources([{...todo,name:'个人任务看板',baseName:'Todo'}],['alice'],()=>{throw Error('no network')})
  assert.equal(namedBase.configured('todo').tableId,'table1')
  const ambiguousBase=new Resources([{...todo,name:'任务',baseName:'Todo'},{...row('other','table2','说明'),baseName:'Todo'}],['alice'],()=>{throw Error('no network')})
  assert.throws(()=>ambiguousBase.configured('todo'),/ambiguous_resource/)
})

