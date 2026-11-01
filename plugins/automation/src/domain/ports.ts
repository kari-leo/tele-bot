import type { ConversationResult, TaskExecution, TrustedActor } from '@telebot/core/contracts'
export interface Wake {id:string;sessionId:string;scheduledAt:string;status:'active'|'inactive';fingerprint:string}
export interface Receipt {scheduledAt:string;deliveredAt:string;messageId:string}
export interface SchedulePort {catalog():Promise<Wake[]>;create(session:string,planned:string,title:string):Promise<Wake>;history(wake:Wake):Promise<Receipt[]>;delete(wake:Wake):Promise<void>}
export interface ExecutionPort {available(name:string):boolean;execute(name:string,input:TaskExecution):Promise<ConversationResult>;deliver(id:string,actor:TrustedActor,result:ConversationResult):Promise<unknown>;status?(actor:TrustedActor,receipt:unknown):unknown;retry?(actor:TrustedActor,receipt:unknown):unknown}
