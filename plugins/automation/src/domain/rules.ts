import { Temporal } from '@js-temporal/polyfill'
export interface Rule {kind:'once'|'daily'|'weekly';first:string;timeZone:string;interval:number;weekdays?:number[];count?:number;until?:string}
export interface TaskInput {title:string;prompt:string;mode:'remind'|'auto'|'confirm';executor:'research'|'codex_apply';rule:Rule;catchUp:'skip'|'run_once'|'ask'}
export function validateTask(input:TaskInput) {
  if(!input||Object.keys(input).some(key=>!['title','prompt','mode','executor','rule','catchUp'].includes(key)))throw new Error('task_fields_not_supported')
  if(!input||!input.title?.trim()||input.title.length>120||!input.prompt?.trim()||input.prompt.length>120000||!['remind','auto','confirm'].includes(input.mode)||!['research','codex_apply'].includes(input.executor)||!['skip','run_once','ask'].includes(input.catchUp))throw new Error('invalid_task_input')
  const rule=input.rule
  if(rule&&Object.keys(rule).some(key=>!['kind','first','timeZone','interval','weekdays','count','until'].includes(key)))throw new Error('task_rule_fields_not_supported')
  if(!rule||!['once','daily','weekly'].includes(rule.kind)||!Number.isSafeInteger(rule.interval)||rule.interval<1||rule.interval>52)throw new Error('invalid_task_rule')
  const first=Temporal.Instant.from(rule.first).toZonedDateTimeISO(rule.timeZone)
  if(rule.count!==undefined&&(!Number.isSafeInteger(rule.count)||rule.count<1||rule.count>100000))throw new Error('invalid_task_count')
  if(rule.until!==undefined&&Temporal.Instant.compare(Temporal.Instant.from(rule.until),first.toInstant())<0)throw new Error('invalid_task_until')
  if(rule.kind==='weekly'&&(!rule.weekdays?.length||rule.weekdays.some(day=>!Number.isInteger(day)||day<0||day>6)||new Set(rule.weekdays).size!==rule.weekdays.length||!rule.weekdays.includes(first.dayOfWeek-1)))throw new Error('invalid_task_weekdays')
}
/** Calendar arithmetic, never a timer. Weeks use legacy Monday=0 and anchor to the first week. */
export function nextOccurrence(rule:Rule,current:string,consumed:number):string|undefined{
  if(rule.kind==='once'||(rule.count!==undefined&&consumed>=rule.count))return
  const first=Temporal.Instant.from(rule.first).toZonedDateTimeISO(rule.timeZone),previous=Temporal.Instant.from(current).toZonedDateTimeISO(rule.timeZone),anchorMonday=first.toPlainDate().subtract({days:first.dayOfWeek-1})
  for(let distance=1;distance<=rule.interval*7+14;distance++){
    const date=previous.toPlainDate().add({days:distance}),days=first.toPlainDate().until(date,{largestUnit:'day'}).days,weeks=Math.floor(anchorMonday.until(date,{largestUnit:'day'}).days/7)
    if(rule.kind==='daily'?days%rule.interval!==0:weeks%rule.interval!==0||!rule.weekdays!.includes(date.dayOfWeek-1))continue
    const local=date.toPlainDateTime(first.toPlainTime()),value=local.toZonedDateTime(rule.timeZone,{disambiguation:'earlier'})
    if(!value.toPlainDateTime().equals(local))continue // DST gap: skip that date; overlap uses earlier.
    const at=value.toInstant().toString({fractionalSecondDigits:3})
    if(rule.until&&Temporal.Instant.compare(at,rule.until)>0)return
    return at
  }
  throw new Error('task_next_occurrence_unresolved')
}
export function dueOccurrences(rule:Rule,planned:string,consumed:number,now:number){
  const due:string[]=[];let next:string|undefined=planned,count=consumed
  while(next&&Date.parse(next)<=now){if(due.length>=50000)throw new Error('missed_history_requires_manual_review');due.push(next);next=nextOccurrence(rule,next,++count)}
  return{due,next,consumed:count}
}
export function executionDecision(mode:TaskInput['mode'],policy:TaskInput['catchUp'],planned:number,now:number,graceMs=300000){
  const late=now-planned>graceMs
  if(late&&policy==='skip')return'skipped'
  if(mode==='remind')return'reminded'
  if(mode==='confirm'||late&&policy==='ask')return'awaiting_auth'
  return'queued'
}
