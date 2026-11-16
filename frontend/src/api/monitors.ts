import {request,post} from './client';import type {Monitor,MonitorDetail,MonitorEvaluation} from '../types/domain';
export const getMonitors=()=>request<Monitor[]>('/monitors');
export const getMonitor=(id:number)=>request<MonitorDetail>('/monitors/'+id);
export const createMonitor=(data:unknown)=>post<MonitorDetail>('/monitors',data);
export const ingestBatch=(id:number,data:unknown)=>post<{duplicate:boolean;changed:boolean;batch:{id:number;batch_key:string;sampling_date:string;revision:number;content_hash:string;row_count:number};pending:MonitorDetail['pending']}>('/monitors/'+id+'/batches',data);
export const replayMonitor=(id:number,data:unknown)=>request<{evaluation:{id:number;version:number;status:string;replay_from:string|null};pending:MonitorDetail['pending']}>('/monitors/'+id+'/replay',{method:'POST',body:JSON.stringify(data)});
export const getMonitorEvaluations=(id:number)=>request<MonitorEvaluation[]>('/monitors/'+id+'/evaluations');
export async function downloadMonitor(id:number){const response=await fetch(`/api/monitors/${id}/export`,{headers:{Authorization:'Bearer '+localStorage.getItem('optics-token')}});if(!response.ok){const data=await response.json();throw new Error(data.detail);}const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=`ciguang-monitor-${id}.csv`;a.click();URL.revokeObjectURL(url);}
