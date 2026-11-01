/** Translate only task identifiers for identity-bound proposal preparation, never grant execution. */
export function scheduleToolInput(action:string,input:string):string{
  if(!['authorize','delete'].includes(action))return input
  const value=JSON.parse(input),id=typeof value==='string'?value:value?.task_id??value?.id
  if(typeof id!=='string'||!/^[A-Za-z0-9._:-]{1,200}$/.test(id)||(typeof value==='object'&&(Object.keys(value).length!==1||Object.keys(value).some(key=>!['id','task_id'].includes(key)))))throw new Error('exact_task_id_required')
  return id
}
