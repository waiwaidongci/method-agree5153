<script setup lang="ts">import {computed} from 'vue';import type {MonitoringResult} from '../types/domain';import {number} from '../utils/format';
const props=defineProps<{result:Partial<MonitoringResult>;limit:number}>();
const rows=computed(()=>props.result.batches||[]);
const points=computed(()=>rows.value.filter(b=>b.bias_delta!==null));
const bounds=computed(()=>{const allow=props.result.allowance||0;const ys=points.value.map(b=>b.bias_delta as number);const ci=points.value.flatMap(b=>b.bias_delta_ci||[]);const all=[...ys,...ci,0,allow,-allow];let mn=Math.min(...all),mx=Math.max(...all);if(mx-mn<.2){mn-=.1;mx+=.1}const pad=(mx-mn)*.15;return {min:mn-pad,max:mx+pad};});
const W=560,H=250,L=64,R=16,T=18,B=42;
const sx=(i:number)=>L+(points.value.length<=1?0:i/(points.value.length-1))*(W-L-R);
const sy=(v:number)=>T+(bounds.value.max-v)/(bounds.value.max-bounds.value.min)*(H-T-B);
const firstFailure=computed(()=>points.value.find(p=>p.out_of_limit));
const yTicks=computed(()=>[0,.25,.5,.75,1].map(t=>bounds.value.min+t*(bounds.value.max-bounds.value.min)));
</script>
<template><section class="agreement-plot deviation-plot"><div class="plot-heading"><h3>批次偏差变化</h3><span>BIAS DELTA / 相对冻结基线</span></div><svg v-if="points.length" viewBox="0 0 600 310" role="img" aria-label="各采样日期批次相对基线偏差的变化与连续超限告警">
<g v-for="(t,i) in yTicks" :key="i" class="plot-grid"><line :x1="L" :x2="W-R" :y1="sy(t)" :y2="sy(t)"/><text :x="L-8" :y="sy(t)+3" text-anchor="end">{{number(t,2)}}</text></g>
<rect class="limit-band" :x="L" :y="sy(result.allowance||0)" :width="W-L-R" :height="Math.max(sy(-(result.allowance||0))-sy(result.allowance||0),0)"/>
<line class="zero-line" :x1="L" :x2="W-R" :y1="sy(0)" :y2="sy(0)"/>
<line v-for="line in [result.allowance||0,-(result.allowance||0)]" :key="line" class="allowance-line" :x1="L" :x2="W-R" :y1="sy(line)" :y2="sy(line)"/>
<template v-for="(p,i) in points" :key="p.batch_key"><line v-if="p.bias_delta_ci" class="ci-line" :x1="sx(i)" :x2="sx(i)" :y1="sy(p.bias_delta_ci[0])" :y2="sy(p.bias_delta_ci[1])"/><circle :cx="sx(i)" :cy="sy(p.bias_delta as number)" :r="p.alert?4.2:3" :class="p.alert?'alert-point':p.out_of_limit?'out-point':'measurement-point'"><title>{{p.batch_key}} / {{p.sampling_date}} / 偏差变化 {{number(p.bias_delta??undefined,3)}} / 连续超限 {{p.consecutive_out}}</title></circle><text v-if="p.alert" :x="sx(i)" :y="sy(p.bias_delta as number)-9" text-anchor="middle" class="alert-label">告警</text></template>
<line v-if="firstFailure" class="first-failure-line" :x1="sx(points.indexOf(firstFailure))" :x2="sx(points.indexOf(firstFailure))" :y1="T" :y2="H-B"/>
<text v-if="firstFailure" :x="Math.min(Math.max(sx(points.indexOf(firstFailure)),L+60),W-R-60)" :y="H-B+26" text-anchor="middle" class="first-failure">首次失效 {{firstFailure.sampling_date}}</text>
<text :x="L" :y="H-8" class="axis-title">{{points[0]?.sampling_date}}</text><text :x="W-R" :y="H-8" text-anchor="end" class="axis-title">{{points[points.length-1]?.sampling_date}}</text>
<text :x="L" y="14" class="axis-title">偏差变化 / 百分点（候选 − 参考，减基线）</text>
</svg><p v-else class="muted">尚无已发布的批次。首批到达并完成重放后显示偏差变化。</p><div class="plot-legend"><span class="green">● 批内偏差变化</span><span class="plum">● 超允许误差</span><span>— 连续 {{limit}} 批触发告警 / 灰色带为 ±{{(result.allowance||0)}} 允许误差</span></div></section></template>
