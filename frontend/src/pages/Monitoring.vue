<script setup lang="ts">
import {ref,onMounted,computed,watch} from 'vue';
import {useRoute} from 'vue-router';
import {useLab} from '../stores/laboratory';
import {useAuth} from '../stores/auth';
import {useMonitoring} from '../stores/monitoring';
import {createMonitor,ingestBatch,replayMonitor,getMonitorEvaluations,downloadMonitor} from '../api/monitors';
import type {MonitorEvaluation} from '../types/domain';
import {useTask} from '../hooks/useTask';
import {usePolling} from '../hooks/usePolling';
import PageTitle from '../components/common/PageTitle.vue';
import ErrorNotice from '../components/common/ErrorNotice.vue';
import StatusBadge from '../components/common/StatusBadge.vue';
import DeviationPlot from '../components/DeviationPlot.vue';
import {number,date} from '../utils/format';
const route=useRoute(),lab=useLab(),auth=useAuth(),mon=useMonitoring(),{busy,error,perform}=useTask();
const selected=ref(Number(route.query.monitor)||0),evaluations=ref<MonitorEvaluation[]>([]),message=ref('');
const form=ref({name:'N02 · 每日批次监测',baseline_run_id:0,consecutive_limit:3,note:'每日独立试件批次，按采样日期顺序与冻结基线比较；连续超限触发告警。'});
const batch=ref({batch_key:'',sampling_date:new Date().toISOString().slice(0,10),source_file:'daily-batch.csv',note:'',csv_text:'sample_code,reference_value,candidate_value\n'});
const replay=ref({from_date:'',reason:'补录/修订后重放受影响区间'});
const current=computed(()=>mon.current&&mon.current.id===selected.value?mon.current:null);
const result=computed(()=>current.value?.published.result||{});
const summary=computed(()=>result.value.summary||{});
const frozenRunIds=computed(()=>new Set(mon.monitors.map(m=>m.baseline_run_id)));
function selectRunOptions(){return lab.runs.filter(r=>r.status==='completed'&&!frozenRunIds.value.has(r.id));}
async function refresh(){await mon.load();if(selected.value){await mon.loadOne(selected.value);evaluations.value=await getMonitorEvaluations(selected.value);}}
onMounted(()=>perform(async()=>{await lab.load();await mon.load();if(!selected.value)selected.value=mon.monitors[0]?.id||0;form.value.baseline_run_id=selectRunOptions()[0]?.id||0;if(selected.value)await refresh();}));
watch(selected,id=>{if(id)perform(refresh);});
usePolling(async()=>{if(current.value&&(current.value.pending.stale||current.value.pending.replaying))await refresh();});
function create(){perform(async()=>{const created=await createMonitor(form.value);await mon.load();selected.value=created.id;await refresh();message.value='监测计划已建立，冻结基线已入队计算。';});}
function submitBatch(){perform(async()=>{const r=await ingestBatch(selected.value,batch.value);message.value=r.duplicate?'该批次内容已存在，未重复计入。':`批次 ${r.batch.batch_key} 第 ${r.batch.revision} 版已接收；正在重放受影响区间。`;batch.value.batch_key='';batch.value.csv_text='sample_code,reference_value,candidate_value\n';await refresh();});}
function requestReplay(){perform(async()=>{const r=await replayMonitor(selected.value,replay.value);message.value=`第 ${r.evaluation.version} 版重放已排队，将覆盖 ${r.evaluation.replay_from||'全部'} 之后的区间。`;await refresh();});}
</script>
<template>
<PageTitle index="06" title="让缓慢漂移在交付之后也被看见" text="以已归档研究冻结基线，按采样日期连续评估每日独立批次；迟到或修订批次重放受影响区间，完成前仍展示上一版。"><button class="outline" :disabled="!current" @click="perform(()=>downloadMonitor(selected))">导出监测CSV ↓</button></PageTitle>
<ErrorNotice :message="error" @retry="perform(refresh)"/>
<div class="run-selector"><label>监测计划<select v-model="selected"><option :value="0">选择或新建监测计划</option><option v-for="m in mon.monitors" :key="m.id" :value="m.id">#{{m.id}} {{m.name}}</option></select></label><StatusBadge v-if="summary.alert_active" status="alert"/></div>
<div class="qualification-layout" :class="{'viewer-only':!auth.canEdit}">
  <aside v-if="auth.canEdit">
    <form v-if="auth.canEdit" class="research-form" @submit.prevent="create"><h2>冻结基线，建立监测</h2><label>监测计划名称<input v-model="form.name" required minlength="2" maxlength="160"></label><label>冻结基线研究<select v-model="form.baseline_run_id"><option v-for="r in selectRunOptions()" :key="r.id" :value="r.id">#{{r.id}} {{r.name}}</option></select></label><label>连续超限批次门槛<input type="number" v-model.number="form.consecutive_limit" min="2" max="30" required></label><label>监测说明<textarea v-model="form.note" rows="3" maxlength="1000"></textarea></label><p class="input-note">基线偏差、观测X范围与允许误差在研究完成后冻结；之后新增读数不改变基线，只作为批次比较对象。</p><p v-if="!selectRunOptions().length" class="input-note plum">没有可冻结的已完成研究，或所有研究均已冻结为基线。</p><button class="primary" :disabled="busy||!form.baseline_run_id">冻结并建立 →</button></form>
    <form v-if="auth.canEdit&&current" class="entry-form" @submit.prevent="submitBatch"><h2>接收每日批次</h2><div class="form-grid two"><label>批次键<input v-model="batch.batch_key" required placeholder="如 2026-11-16-A"></label><label>采样日期<input type="date" v-model="batch.sampling_date" required></label></div><label>来源文件<input v-model="batch.source_file" required></label><label>条件说明<input v-model="batch.note" maxlength="600"></label><label>配对CSV<textarea v-model="batch.csv_text" rows="5" required></textarea></label><p class="input-note">同一批次键重复投递且内容相同不会重复计入；内容不同则记为同一批次的新修订版，并重放该采样日期起的区间。</p><button class="primary" :disabled="busy">投递批次 →</button></form>
    <form v-if="auth.canEdit&&current" class="entry-form" @submit.prevent="requestReplay"><h2>手动重放</h2><label>从采样日期起<input type="date" v-model="replay.from_date"></label><label>原因<input v-model="replay.reason" maxlength="300"></label><button class="outline" :disabled="busy">排队重放 ↻</button><p class="input-note">留空则从最早批次起重放。重放期间照常接收新批，完成前展示上一发布版本。</p></form>
  </aside>
  <section v-if="current" class="monitor-main">
    <div class="monitor-state" :class="current.pending.stale?'stale':'fresh'">
      <div><p class="eyebrow">PUBLISHED VERSION / 已发布</p><b>v{{current.published.version??'—'}}</b><small>{{current.published.completed_at?date(current.published.completed_at):'等待首次计算'}} · 指纹 {{current.published.fingerprint.slice(0,12)||'—'}}</small></div>
      <div v-if="current.pending.stale"><p class="eyebrow">PENDING REPLAY / 待发布</p><b>v{{(current.published.version||0)+1}}</b><small>已接收 {{current.pending.change_seq}} 次变更，重放自 {{current.pending.replay_from||'最早批次'}}；完成前继续展示 v{{current.published.version??'—'}}。</small></div>
      <div v-else><p class="eyebrow">STATE</p><b>已同步</b><small>全部已接收批次均已纳入当前发布版本。</small></div>
    </div>
    <p v-if="message" class="success" role="status">{{message}}</p>
    <div v-if="summary.alert_active" class="monitor-alert" role="alert"><p class="eyebrow">CONSECUTIVE BREACH ALERT</p><h2>连续 {{summary.consecutive_out}} 批偏差超出允许误差，告警已触发</h2><p>连续区间自 <b>{{summary.current_streak_start}}</b> 起；历史首次失效采样日期 <b>{{summary.first_failure_date}}</b>，依据批次：<span v-for="b in summary.first_failure_batches" :key="b.batch_key+b.sampling_date" class="basis-chip">{{b.sampling_date}} / {{b.batch_key}} · r{{b.revision}}</span></p><p class="muted">已批准资格保留原结论；此告警作为新交付的时效标注，不追溯改写历史批准。</p></div>
    <div v-else-if="summary.first_failure_date" class="monitor-cleared"><p class="eyebrow">ALERT CLEARED / 已恢复</p><p>最近连续超限已在可比的范围内批次后结束。历史首次失效采样日期仍为 <b>{{summary.first_failure_date}}</b>；依据批次 <span v-for="b in summary.first_failure_batches" :key="b.batch_key+b.sampling_date" class="basis-chip">{{b.sampling_date}} / {{b.batch_key}} · r{{b.revision}}</span>。</p></div>
    <div class="monitor-baseline"><p class="eyebrow">FROZEN BASELINE / {{current.baseline.run_name}}</p><h2>{{current.baseline.instrument_codes.join(' ↔ ')}} · X {{current.baseline.x_range.map(v=>number(v,2)).join('—')}}%</h2><dl><div><dt>基线平均偏差</dt><dd>{{number(current.baseline.bias,3)}}</dd></div><div><dt>基线一致性界限</dt><dd>{{number(current.baseline.loa_lower,2)}} / {{number(current.baseline.loa_upper,2)}}</dd></div><div><dt>允许误差</dt><dd>±{{number(current.baseline.allowance,2)}}</dd></div><div><dt>冻结时点</dt><dd>{{current.baseline.frozen_at.slice(0,10)}}</dd></div></dl><small>基线指纹 {{current.baseline.run_fingerprint.slice(0,20)}}… · 原值互换{{current.baseline.raw_eligible?'达到门槛':'未达门槛'}}</small></div>
    <div class="replay-scope" v-if="result.replay&&(current.published.version||0)>1"><span class="eyebrow">REPLAY SCOPE</span><b>{{result.replay.scope==='interval'?'区间重放':'完整重算'}}</b><small>请求自 {{result.replay.requested_from||'最早批次'}}；复用未受影响批次 {{result.replay.reused_batches??0}} 个，自 {{result.replay.recomputed_from||'—'}} 起重算。结果等同一次按日期算完。</small></div>
    <DeviationPlot :result="result" :limit="current.consecutive_limit"/>
    <div class="table-wrap"><table><thead><tr><th>采样日期 / 批次</th><th>版本</th><th>批内偏差</th><th>相对基线变化</th><th>95% CI</th><th>连续超限</th><th>判定</th></tr></thead><tbody><tr v-for="b in result.batches||[]" :key="b.batch_key+b.sampling_date"><td><b>{{b.sampling_date}}</b><small>{{b.batch_key}} · {{b.n_in_range}}/{{b.n_total}} 对在基线范围</small></td><td>r{{b.revision}}</td><td>{{number(b.raw_bias??undefined,3)}}</td><td :class="b.out_of_limit?'plum':''">{{number(b.bias_delta??undefined,3)}}</td><td>{{b.bias_delta_ci?b.bias_delta_ci.map(v=>number(v,2)).join(' — '):'—'}}</td><td>{{b.consecutive_out||'—'}}</td><td><span v-if="b.alert" class="study-verdict plum">告警</span><span v-else-if="b.out_of_limit" class="plum">超限</span><span v-else-if="b.status==='comparable'" class="green">范围内</span><span v-else class="muted">批次不可比</span></td></tr></tbody></table></div>
    <p v-if="!(result.batches||[]).length" class="muted">基线已冻结，尚无已发布批次。投递每日批次后按采样日期连续评估。</p>
    <div class="monitor-subsection"><div class="section-heading"><h2>批次清单</h2><span>含迟到与修订</span></div><div class="table-wrap"><table><thead><tr><th>采样日期</th><th>批次键</th><th>当前版本</th><th>行数</th><th>首收时间</th><th>最近接收</th></tr></thead><tbody><tr v-for="b in current.batches" :key="b.id"><td>{{b.sampling_date}}</td><td>{{b.batch_key}}</td><td>r{{b.revision}}</td><td>{{b.row_count}}</td><td>{{date(b.first_received_at)}}</td><td>{{date(b.updated_at)}}<small v-if="b.revision>1">修订版</small></td></tr></tbody></table></div></div>
    <div class="monitor-subsection"><div class="section-heading"><h2>发布版本</h2><span>每次重放生成新版本</span></div><div class="table-wrap"><table><thead><tr><th>版本</th><th>状态</th><th>重放区间 / 范围</th><th>已纳入变更序号</th><th>完成时间</th><th>指纹</th></tr></thead><tbody><tr v-for="e in evaluations" :key="e.id"><td>v{{e.version}}</td><td><StatusBadge :status="e.status==='completed'?'approved':e.status"/></td><td>{{e.replay_from||'全部'}}<small v-if="e.reason">{{e.reason}}</small></td><td>{{e.applied_seq>=0?e.applied_seq:'—'}}</td><td>{{e.completed_at?date(e.completed_at):'—'}}</td><td><code>{{e.fingerprint.slice(0,12)||'—'}}</code><small v-if="e.error" class="plum">{{e.error}}</small></td></tr></tbody></table></div></div>
  </section>
  <section v-else class="empty"><span>◷</span><h3>尚未选择监测计划</h3><p>从已完成研究冻结基线，然后按采样日期投递每日独立批次；系统连续评估偏差变化并在连续超限时告警。</p></section>
</div>
</template>
