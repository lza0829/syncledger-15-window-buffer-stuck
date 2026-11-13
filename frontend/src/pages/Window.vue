<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { getJSON, postJSON, putJSON } from '../api'

const pairs = ref([])
const pairsInfo = ref([])
const pairId = ref(null)
const form = reactive({
  window_start: '',
  window_end: '',
  max_lateness_seconds: 0,
  left_time_field: 'updated_at',
  right_time_field: 'updated_at',
  liquidation: 'finalize_side_only',
})
const buffer = ref([])
const events = ref([])
const batches = ref([])
const last = ref(null)
const err = ref('')
const notice = ref('')
const saving = ref(false)
const busyRun = ref(false)
const demoBusy = ref('')
const demos = ref([])
const demoResult = ref(null)

const selected = computed(() => pairsInfo.value.find((p) => p.pair_id === pairId.value) || null)
const activeBuffer = computed(() => buffer.value.filter((b) => b.state === 'buffered'))
const closedBuffer = computed(() => buffer.value.filter((b) => b.state !== 'buffered').slice(-20).reverse())

const kindLabel = { left_only: '仅左有', right_only: '仅右有', field_mismatch: '字段不一致' }
const eventLabel = {
  buffer_entered: '进入缓冲',
  buffer_continued: '缓冲延续',
  aligned_after_buffer: '窗内对齐',
  buffer_finalized: '逾窗落终态',
  buffer_discarded: '逾窗丢弃',
  buffer_superseded: '被新到达行替换',
  outside_window: '窗外未参与',
  not_arrived: '未到达本趟',
}

function secs(v) {
  const n = Number(v || 0)
  if (n >= 3600) return `${n / 3600} 小时`
  if (n >= 60) return `${n / 60} 分钟`
  return `${n} 秒`
}

function toLocalInput(iso) {
  // Keep the value as stored (UTC ISO); the input is type=text for fidelity.
  return iso || ''
}

async function refreshAll() {
  err.value = ''
  try {
    pairsInfo.value = await getJSON('/api/windows')
    pairs.value = pairsInfo.value
    if (pairId.value == null && pairs.value.length) pairId.value = pairs.value[0].pair_id
    await loadPair()
    await loadBatches()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function loadPair() {
  if (!pairId.value) return
  const data = await getJSON(`/api/pairs/${pairId.value}/window`)
  const w = data.window || {}
  form.window_start = toLocalInput(w.window_start)
  form.window_end = toLocalInput(w.window_end)
  form.max_lateness_seconds = w.max_lateness_seconds ?? 0
  form.left_time_field = w.left_time_field || 'updated_at'
  form.right_time_field = w.right_time_field || form.left_time_field
  form.liquidation = w.liquidation || 'finalize_side_only'
  buffer.value = await getJSON(`/api/buffer?pair_id=${pairId.value}&state=all`)
  events.value = await getJSON(`/api/buffer/events?pair_id=${pairId.value}`)
}

async function loadBatches() {
  const q = pairId.value ? `&pair_id=${pairId.value}` : ''
  batches.value = await getJSON(`/api/batches?limit=30${q}`)
}

async function saveWindow() {
  if (!pairId.value) return
  saving.value = true
  err.value = ''
  try {
    await putJSON(`/api/pairs/${pairId.value}/window`, {
      window_start: form.window_start,
      window_end: form.window_end,
      max_lateness_seconds: Number(form.max_lateness_seconds),
      left_time_field: form.left_time_field,
      right_time_field: form.right_time_field,
      liquidation: form.liquidation,
      auto_slide: false,
    })
    notice.value = '窗口已保存。'
    await refreshAll()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    saving.value = false
  }
}

async function runPass() {
  if (!pairId.value) return
  busyRun.value = true
  err.value = ''
  try {
    last.value = await postJSON('/api/reconcile', {
      pair_id: Number(pairId.value),
      note: '窗口对账',
      window_start: form.window_start,
      window_end: form.window_end,
      max_lateness_seconds: Number(form.max_lateness_seconds),
      left_time_field: form.left_time_field,
      right_time_field: form.right_time_field,
      liquidation: form.liquidation,
    })
    await refreshAll()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busyRun.value = false
  }
}

async function runDemo(key) {
  demoBusy.value = key
  err.value = ''
  try {
    demoResult.value = await postJSON(`/api/demo/scenarios/${key}/run`)
    demoBusy.value = ''
    await refreshAll()
  } catch (e) {
    err.value = String(e.message || e)
    demoBusy.value = ''
  }
}

onMounted(async () => {
  try {
    demos.value = await getJSON('/api/demo/scenarios')
  } catch (e) {
    err.value = String(e.message || e)
  }
  await refreshAll()
})

watch(pairId, async () => {
  try {
    notice.value = ''
    demoResult.value = null
    await loadPair()
    await loadBatches()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <div class="page">
    <h1>窗口与缓冲</h1>
    <p class="muted">
      行上事件/更新时间只有落进可配时间窗才参与本趟。窗内一侧先到、对侧未到 → 进入可审计缓冲，
      <strong>不立即落仅左/仅右终态</strong>；对侧窗内补到即对齐或转字段比对；逾窗仍单边才落终态。
      缓冲中的行不算进差异合计。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <p v-if="notice" class="tag ok">{{ notice }}</p>

    <div class="panel">
      <h3 style="margin-top:0">窗口配置</h3>
      <div class="form-grid">
        <label>对账对
          <select v-model.number="pairId">
            <option v-for="p in pairs" :key="p.pair_id" :value="p.pair_id">{{ p.pair_title }}</option>
          </select>
        </label>
        <label>窗口下沿（含）
          <input v-model="form.window_start" class="mono" placeholder="2026-11-12T08:00:00Z" />
        </label>
        <label>窗口上沿（不含）
          <input v-model="form.window_end" class="mono" placeholder="2026-11-12T08:15:00Z" />
        </label>
        <label>允许迟到（秒）
          <input v-model.number="form.max_lateness_seconds" type="number" min="0" />
        </label>
        <label>左端时间字段
          <input v-model="form.left_time_field" class="mono" />
        </label>
        <label>右端时间字段
          <input v-model="form.right_time_field" class="mono" />
        </label>
        <label>逾窗规则
          <select v-model="form.liquidation">
            <option value="finalize_side_only">落仅左/仅右终态</option>
            <option value="discard">显式丢弃并计数</option>
          </select>
        </label>
      </div>
      <p class="muted" style="margin:0.6rem 0 0">
        缓冲截止 = 窗口上沿 + 允许迟到（{{ secs(form.max_lateness_seconds) }}）。
        时钟越过截止、或窗口上沿推进把缓冲行的事件时间甩出窗口，该行必须按同一逾窗规则清算。
      </p>
      <div style="margin-top:0.75rem;display:flex;gap:0.6rem;flex-wrap:wrap">
        <button :disabled="saving || !pairId" @click="saveWindow">{{ saving ? '保存中…' : '保存窗口' }}</button>
        <button class="secondary" :disabled="busyRun || !pairId" @click="runPass">
          {{ busyRun ? '抽取中…' : '按当前窗口跑一趟' }}
        </button>
        <router-link v-if="last" :to="{ path: '/diffs', query: { batch_id: last.id } }">查看本批差异 →</router-link>
      </div>
    </div>

    <div class="panel" v-if="last">
      <h3 style="margin-top:0">本趟计数</h3>
      <p class="muted">
        批次 <span class="mono">{{ last.batch_no }}</span> · 时钟 <span class="mono">{{ last.as_of }}</span>
        · 窗口 <span class="mono">{{ last.window?.start }} → {{ last.window?.end }}</span>
      </p>
      <div class="stat-grid">
        <div class="stat"><div class="stat-num">{{ last.counts.matched }}</div><div class="stat-label">一致</div></div>
        <div class="stat"><div class="stat-num">{{ last.counts.field_mismatch }}</div><div class="stat-label">字段不一致</div></div>
        <div class="stat"><div class="stat-num">{{ last.counts.left_only }}</div><div class="stat-label">仅左有</div></div>
        <div class="stat"><div class="stat-num">{{ last.counts.right_only }}</div><div class="stat-label">仅右有</div></div>
        <div class="stat emphasis"><div class="stat-num">{{ last.counts.buffered }}</div><div class="stat-label">缓冲中（不计差异）</div></div>
        <div class="stat"><div class="stat-num">{{ last.counts.expired_total }}</div><div class="stat-label">逾窗落单边</div></div>
        <div class="stat"><div class="stat-num">{{ last.counts.aligned_after_buffer }}</div><div class="stat-label">缓冲后对齐</div></div>
        <div class="stat"><div class="stat-num">{{ last.counts.outside_window }}</div><div class="stat-label">窗外未参与</div></div>
        <div class="stat"><div class="stat-num">{{ last.counts.discarded }}</div><div class="stat-label">逾窗丢弃</div></div>
        <div class="stat"><div class="stat-num">{{ last.counts.diff_total }}</div><div class="stat-label">终态差异合计</div></div>
      </div>
      <p class="muted" style="margin-top:0.6rem">
        左/右行 {{ last.counts.left_rows }} / {{ last.counts.right_rows }} ·
        窗内 {{ last.counts.inside_left }} / {{ last.counts.inside_right }} ·
        未到达 {{ last.counts.not_arrived }}
      </p>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">缓冲账本 <span class="tag warn">{{ activeBuffer.length }} 条待对齐</span>
        <span class="tag">累计已结算 {{ buffer.length - activeBuffer.length }} 条</span></h3>
      <p class="muted" style="margin:0 0 0.5rem">
        缓冲中的行不落终态、不计差异合计。表内按截止时间排序；已结算（对齐/落终态/丢弃/替换）的历史在下部。
      </p>
      <table>
        <thead>
          <tr><th>端</th><th>对齐键</th><th>事件时间</th><th>截止</th><th>状态</th><th>进入批次</th><th>结算原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in activeBuffer" :key="b.id">
            <td>{{ b.side === 'left' ? '左端' : '右端' }}</td>
            <td class="mono">{{ JSON.stringify(b.align_key) }}</td>
            <td class="mono">{{ b.event_time }}</td>
            <td class="mono">{{ b.deadline }}</td>
            <td><span class="tag warn">buffered</span></td>
            <td>{{ b.entered_batch_id || '—' }}</td>
            <td class="muted">{{ b.settled_reason || '—' }}</td>
          </tr>
          <tr v-if="!activeBuffer.length"><td colspan="7" class="muted">缓冲为空。</td></tr>
        </tbody>
      </table>

      <h4 style="margin:0.9rem 0 0.3rem">最近已结算</h4>
      <table>
        <thead>
          <tr><th>端</th><th>对齐键</th><th>事件时间</th><th>截止</th><th>结算状态</th><th>结算批次</th><th>结算原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in closedBuffer" :key="b.id">
            <td>{{ b.side === 'left' ? '左端' : '右端' }}</td>
            <td class="mono">{{ JSON.stringify(b.align_key) }}</td>
            <td class="mono">{{ b.event_time }}</td>
            <td class="mono">{{ b.deadline }}</td>
            <td><span class="tag" :class="b.state === 'discarded' || b.state === 'superseded' ? 'danger' : 'ok'">{{ b.state }}</span></td>
            <td>{{ b.settled_batch_id || '—' }}</td>
            <td class="muted mono">{{ b.settled_reason || '—' }}</td>
          </tr>
          <tr v-if="!closedBuffer.length"><td colspan="7" class="muted">暂无已结算记录。</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">演示（一把跑通三种走势）</h3>
      <p class="muted">演示会先按逾窗规则清算该对账对的历史缓冲，再按剧本逐步跑真实批次（带观测时钟）。</p>
      <div class="demo-grid">
        <button
          v-for="d in demos"
          :key="d.key"
          :class="{ secondary: demoBusy && demoBusy !== d.key }"
          :disabled="!!demoBusy"
          @click="runDemo(d.key)"
        >
          <span v-if="demoBusy === d.key">运行中…</span>
          <span v-else>{{ d.title }}</span>
        </button>
      </div>
      <div v-if="demoResult" class="demo-result">
        <h4 style="margin:0.85rem 0 0.25rem">{{ demoResult.title }}</h4>
        <p class="muted">{{ demoResult.summary }}</p>
        <p v-if="demoResult.reset" class="muted">
          前置清算：批次 <span class="mono">{{ demoResult.reset.batch_no }}</span>，
          丢弃 {{ demoResult.reset.counts.discarded }} 条历史缓冲。
        </p>
        <table>
          <thead>
            <tr><th>步骤</th><th>时钟</th><th>到达 左/右</th><th>终态差异</th><th>缓冲</th><th>逾窗落单边</th><th>丢弃</th><th>缓冲后对齐</th></tr>
          </thead>
          <tbody>
            <tr v-for="p in demoResult.passes" :key="p.step">
              <td>{{ p.label }}<div class="muted">{{ p.note }}</div></td>
              <td class="mono">{{ p.as_of }}</td>
              <td class="mono">{{ p.left_arrivals }} / {{ p.right_arrivals }}</td>
              <td><strong>{{ p.batch.counts.diff_total }}</strong></td>
              <td>{{ p.batch.counts.buffered }}</td>
              <td>{{ p.batch.counts.expired_total }}</td>
              <td>{{ p.batch.counts.discarded }}</td>
              <td>{{ p.batch.counts.aligned_after_buffer }}</td>
            </tr>
          </tbody>
        </table>
        <p class="muted">
          演示结束后活跃缓冲：{{ demoResult.active_buffer.length }} 条。
        </p>
      </div>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">缓冲事件审计（最近）</h3>
      <table>
        <thead><tr><th>批次</th><th>事件</th><th>端</th><th>对齐键</th><th>事件时间</th><th>截止</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="e in events.slice(0, 40)" :key="e.id">
            <td>{{ e.batch_id }}</td>
            <td><span class="tag" :class="e.event_type === 'buffer_entered' ? 'warn' : (e.event_type === 'aligned_after_buffer' ? 'ok' : '')">{{ eventLabel[e.event_type] || e.event_type }}</span></td>
            <td>{{ e.side === 'left' ? '左端' : '右端' }}</td>
            <td class="mono">{{ JSON.stringify(e.align_key) }}</td>
            <td class="mono">{{ e.event_time || '—' }}</td>
            <td class="mono">{{ e.deadline || '—' }}</td>
            <td class="muted">{{ e.reason || '—' }}</td>
          </tr>
          <tr v-if="!events.length"><td colspan="7" class="muted">暂无缓冲事件。</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">本对账对批次</h3>
      <table>
        <thead>
          <tr><th>批次</th><th>时钟</th><th>窗口</th><th>终态差异</th><th>缓冲</th><th>逾窗</th><th>丢弃</th><th>窗外</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="b.id">
            <td class="mono">{{ b.batch_no }}</td>
            <td class="mono">{{ b.as_of || '—' }}</td>
            <td class="mono">{{ b.windowed ? `${b.window_start} → ${b.window_end}` : '（全量口径）' }}</td>
            <td>{{ b.diff_total }}</td>
            <td>{{ b.buffered }}</td>
            <td>{{ b.expired_total }}</td>
            <td>{{ b.discarded }}</td>
            <td>{{ b.outside_window }}</td>
            <td class="mono">{{ b.created_at }}</td>
          </tr>
          <tr v-if="!batches.length"><td colspan="9" class="muted">还没有批次。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
