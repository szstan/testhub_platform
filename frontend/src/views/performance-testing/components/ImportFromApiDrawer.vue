<template>
  <el-drawer
    :model-value="modelValue"
    :title="t('performanceTesting.importApi.title')"
    size="620px"
    @update:model-value="(v) => emit('update:modelValue', v)"
  >
    <div class="import-drawer">
      <el-alert :title="t('performanceTesting.importApi.tip')" type="info" :closable="false" show-icon class="tip" />
      <el-form :inline="true" class="filter-bar">
        <el-form-item :label="t('performanceTesting.importApi.selectProject')">
          <el-select v-model="projectId" filterable :placeholder="t('performanceTesting.common.selectProject')" style="width: 240px" @change="loadRequests">
            <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-input v-model="keyword" :placeholder="t('performanceTesting.importApi.searchPlaceholder')" clearable style="width: 200px" @input="filterRequests" />
        </el-form-item>
      </el-form>

      <div class="request-list" v-loading="loading">
        <el-checkbox-group v-model="selectedIds">
          <template v-for="r in filteredRequests" :key="r.id">
            <div class="request-item" :class="{ active: selectedIds.includes(r.id) }">
              <el-checkbox :value="r.id" />
              <el-tag size="small" :type="methodTag(r.method)">{{ r.method }}</el-tag>
              <el-tooltip placement="top" :show-after="150">
                <template #content>
                  <div style="font-weight: 600;">{{ r.method }} · {{ r.name }}</div>
                  <div style="margin-top: 4px; color: #a0cfff; max-width: 460px; word-break: break-all; white-space: pre-wrap;">{{ displayUrl(r) }}</div>
                </template>
                <span class="r-name">{{ r.name }}</span>
              </el-tooltip>
              <el-tooltip :content="displayUrl(r)" placement="top" :show-after="150">
                <span class="r-url">{{ displayUrl(r) }}</span>
              </el-tooltip>
              <span class="expand-icon" :class="{ expanded: expandedId === r.id }" @click="toggleExpand(r.id)">{{ expandedId === r.id ? '▾' : '▸' }}</span>
            </div>
            <!-- 展开后展示该接口的完整信息（请求头/参数/请求体/鉴权），数据来自列表接口已返回的字段 -->
            <div v-if="expandedId === r.id" class="r-detail">
              <div v-if="r.url" class="d-row"><span class="d-label">URL</span><span class="d-value full">{{ displayUrl(r) }}</span></div>
              <div class="d-row"><span class="d-label">请求头</span><span class="d-value">{{ fmtObj(r.headers) }}</span></div>
              <div class="d-row"><span class="d-label">Query参数</span><span class="d-value">{{ fmtObj(r.params) }}</span></div>
              <div class="d-row"><span class="d-label">请求体</span><span class="d-value">{{ fmtBody(r.body) }}</span></div>
              <div class="d-row"><span class="d-label">鉴权</span><span class="d-value">{{ fmtObj(r.auth) }}</span></div>
            </div>
          </template>
        </el-checkbox-group>
        <el-empty v-if="!loading && filteredRequests.length === 0" :description="projectId ? t('performanceTesting.importApi.empty') : t('performanceTesting.common.selectProject')" />
      </div>

      <!-- 基础地址（baseUrl）配置：让导入的 {{baseUrl}}/path 可选、可配 -->
      <el-divider content-position="left">{{ t('performanceTesting.importApi.baseUrlTitle') }}</el-divider>
      <el-form label-width="110px" class="base-url-form">
        <el-form-item :label="t('performanceTesting.importApi.baseUrlModeLabel')">
          <el-radio-group v-model="baseUrlMode">
            <el-radio value="keep">
              {{ t('performanceTesting.importApi.baseUrlKeep') }}
              <code class="token">{{ token }}</code>
            </el-radio>
            <el-radio value="replace">{{ t('performanceTesting.importApi.baseUrlReplace') }}</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="baseUrlMode === 'keep'" :label="t('performanceTesting.importApi.baseUrlScenarioLabel')">
          <el-input
            v-model="baseUrlValue"
            :placeholder="t('performanceTesting.importApi.baseUrlPlaceholder')"
          />
          <div class="form-tip">{{ t('performanceTesting.importApi.baseUrlKeepTip') }}</div>
        </el-form-item>

        <el-form-item v-else :label="t('performanceTesting.importApi.baseUrlSelectLabel')">
          <el-select
            v-model="baseUrlValue"
            filterable
            allow-create
            default-first-option
            :placeholder="t('performanceTesting.importApi.baseUrlPlaceholder')"
            style="width: 100%"
          >
            <el-option v-for="c in candidateBaseUrls" :key="c" :label="c" :value="c" />
          </el-select>
          <div class="form-tip">{{ t('performanceTesting.importApi.baseUrlReplaceTip') }}</div>
        </el-form-item>

        <el-alert
          v-if="previewUrl"
          :title="t('performanceTesting.importApi.baseUrlPreviewTitle')"
          type="success"
          :closable="false"
          class="preview"
        >
          <code class="preview-code">{{ previewUrl }}</code>
        </el-alert>
        <el-alert
          v-else-if="baseUrlMode === 'keep' && !baseUrlValue"
          :title="t('performanceTesting.importApi.baseUrlNotSetWarn')"
          type="warning"
          :closable="false"
          class="preview-bg"
        />
      </el-form>

      <el-checkbox v-model="asSetup" class="as-setup">{{ t('performanceTesting.importApi.asSetup') }}</el-checkbox>

      <div class="footer">
        <span class="selected-text">{{ t('performanceTesting.importApi.selected', { count: selectedIds.length }) }}</span>
        <el-button type="primary" :disabled="selectedIds.length === 0" :loading="submitting" @click="confirm">
          {{ t('performanceTesting.common.confirm') }}
        </el-button>
      </div>
    </div>
  </el-drawer>
</template>

<script setup>
import { ref, watch, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { getApiProjects, getApiRequests, getApiCollections } from '@/api/api-testing'
import { importStepsFromApi, getPerfScenario } from '@/api/performance-testing'

// 导入 URL 中常见的 baseUrl 变量引用，仅用于在界面上展示（避免写进 i18n 触发插值陷阱）
const token = '{{baseUrl}}'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  scenarioId: { type: [Number, String], default: null }
})
const emit = defineEmits(['update:modelValue', 'imported'])

const { t } = useI18n()
const projects = ref([])
const projectId = ref(null)
const requests = ref([])
const filteredRequests = ref([])
const keyword = ref('')
const selectedIds = ref([])
const asSetup = ref(false)
const loading = ref(false)
const submitting = ref(false)
const expandedId = ref(null) // 当前展开查看完整信息的接口 id

// 基础地址配置
const baseUrlMode = ref('keep') // 'keep' | 'replace'
const baseUrlValue = ref('')
const scenarioBaseUrl = ref('')      // 场景当前 env_config.base_url（keep 默认填充）
const collectionBaseUrls = ref([])   // 来源项目各集合的 base_url（replace 候选）

const methodTag = (m) => {
  const map = { GET: 'success', POST: 'warning', PUT: 'primary', DELETE: 'danger', PATCH: 'info' }
  return map[(m || 'GET').toUpperCase()] || 'info'
}

function toggleExpand(id) {
  expandedId.value = expandedId.value === id ? null : id
}

// 展示 URL：若配置了 baseUrl，则把行内常见的 {{baseUrl}} / {{base_url}} 占位符
// 以及字面 base_url/ 前缀（不带花括号）解析为真实地址；
// 同时折叠路径中的冗余双斜杠（保留 scheme 的 ://），与后端导入规整保持一致。
function displayUrl(r) {
  let url = (r && r.url) || ''
  const base = (baseUrlValue.value || '').trim().replace(/\/+$/, '')
  if (base) {
    url = url.replace(/^\s*\/?(?:\{\{\s*base[_-]?url\s*\}\}|base[_-]?url(?=\/|$))\s*/i, (m) => {
      const rest = url.slice(m.length).replace(/^\/+/, '')
      return rest ? `${base}/${rest}` : base
    })
  }
  // 折叠冗余双斜杠（保留 :// ）
  return url.replace(/(?<!:)\/{2,}/g, '/')
}

// 对象/数组 -> 可读文本（空则显示 -）
function fmtObj(v) {
  if (v === null || v === undefined || v === '') return '-'
  if (typeof v === 'object') {
    const entries = Object.entries(v)
    if (entries.length === 0) return '-'
    return entries.map(([k, val]) => `${k}: ${typeof val === 'object' ? JSON.stringify(val) : val}`).join('；')
  }
  return String(v)
}

// 请求体 -> 可读文本（body 是 {type, data} 结构，兼容旧字段 content）
function fmtBody(body) {
  if (!body || typeof body !== 'object') return (body === '' || body === undefined || body === null) ? '-' : String(body)
  const type = body.type || 'none'
  let content = body.data
  if (content === undefined || content === null) content = body.content
  const txt = typeof content === 'object' ? JSON.stringify(content) : String(content || '')
  return type !== 'none' && type !== 'NONE' ? `[${type}] ${txt}` : (txt || '-')
}

// replace 模式下的候选基础地址：来源集合的 base_url + 场景当前 base_url，去重
const candidateBaseUrls = computed(() => {
  const set = new Set()
  if (scenarioBaseUrl.value) set.add(scenarioBaseUrl.value)
  collectionBaseUrls.value.forEach((b) => { if (b) set.add(b) })
  return Array.from(set)
})

// 预览：取首个含 baseUrl 变量（或字面 base_url/ 前缀）的已选接口，按当前配置展示其解析结果
const BASE_URL_PREFIX_RE = /^\s*\/?(?:\{\{\s*base[_-]?url\s*\}\}|base[_-]?url(?=\/|$))\s*/i
const previewUrl = computed(() => {
  const sample = requests.value.find(
    (r) => selectedIds.value.includes(r.id) && BASE_URL_PREFIX_RE.test(r.url || '')
  )
  if (!sample || !baseUrlValue.value) return ''
  const m = sample.url.match(BASE_URL_PREFIX_RE)
  let rest = sample.url.slice(m[0].length)
  let base = baseUrlValue.value.replace(/\/+$/, '')
  if (rest.startsWith('/')) rest = rest.slice(1)
  return rest ? `${base}/${rest}` : base
})

watch(() => props.modelValue, (v) => {
  if (v) {
    selectedIds.value = []
    asSetup.value = false
    keyword.value = ''
    baseUrlMode.value = 'keep'
    baseUrlValue.value = ''
    scenarioBaseUrl.value = ''
    collectionBaseUrls.value = []
    if (projects.value.length === 0) loadProjects()
    loadScenario()
  }
})

async function loadProjects() {
  try {
    const res = await getApiProjects({ page_size: 200 })
    projects.value = res.data.results || res.data || []
  } catch (e) { /* ignore */ }
}

async function loadScenario() {
  if (!props.scenarioId) return
  try {
    const res = await getPerfScenario(props.scenarioId)
    const env = (res.data && res.data.env_config) || {}
    scenarioBaseUrl.value = env.base_url || ''
    if (!baseUrlValue.value) baseUrlValue.value = scenarioBaseUrl.value
  } catch (e) { /* ignore */ }
}

async function loadRequests() {
  if (!projectId.value) { requests.value = []; filteredRequests.value = []; collectionBaseUrls.value = []; return }
  loading.value = true
  try {
    const res = await getApiRequests({ project: projectId.value, page_size: 500 })
    requests.value = res.data.results || res.data || []
    filterRequests()
    // 顺带收集来源集合的 base_url，作为 replace 模式的候选
    const cres = await getApiCollections({ project: projectId.value, page_size: 200 })
    const cols = cres.data.results || cres.data || []
    collectionBaseUrls.value = cols.map((c) => c.base_url).filter(Boolean)
  } catch (e) {
    ElMessage.error('加载接口用例失败')
  } finally {
    loading.value = false
  }
}

function filterRequests() {
  const k = (keyword.value || '').trim().toLowerCase()
  filteredRequests.value = k
    ? requests.value.filter(r => (r.name || '').toLowerCase().includes(k) || (r.url || '').toLowerCase().includes(k))
    : requests.value
}

async function confirm() {
  if (!selectedIds.value.length) return
  if (!props.scenarioId) { ElMessage.error('场景未保存，无法导入'); return }
  submitting.value = true
  try {
    const res = await importStepsFromApi(props.scenarioId, {
      request_ids: selectedIds.value,
      as_setup: asSetup.value,
      base_url_mode: baseUrlMode.value,
      base_url: (baseUrlValue.value || '').trim(),
    })
    const count = (res.data.imported !== undefined) ? res.data.imported : selectedIds.value.length
    ElMessage.success(t('performanceTesting.importApi.importSuccess', { count }))
    emit('imported', res.data)
    emit('update:modelValue', false)
  } catch (e) {
    const msg = e?.response?.data?.error || '导入失败'
    ElMessage.error(msg)
  } finally {
    submitting.value = false
  }
}
</script>

<style lang="scss" scoped>
.import-drawer {
  display: flex;
  flex-direction: column;
  height: 100%;
  .tip { margin-bottom: 12px; }
  .filter-bar { margin-bottom: 10px; }
  .request-list {
    flex: 1;
    overflow-y: auto;
    border: 1px solid #ebeef5;
    border-radius: 8px;
    padding: 6px;
    min-height: 200px;
  }
  /* Element Plus 的 .el-checkbox-group 默认 font-size:0;line-height:0，
     会让其内部文本（接口名称 r-name、URL r-url 等）继承 0 而不可见。
     这里在组件作用域内重置，使接口名称/URL 正常显示。 */
  :deep(.el-checkbox-group) {
    font-size: inherit;
    line-height: normal;
    width: 100%;
  }
  .request-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 6px;
    min-width: 0;
    &:hover { background: #f5f7fa; }
    &.active { background: #ecf5ff; }
    .r-name { font-weight: 500; font-size: 14px; line-height: 22px; flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: default; }
    .r-url { color: #8c8c8c; font-size: 12px; flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: default; }
    .expand-icon {
      cursor: pointer; color: #909399; font-size: 12px; flex: 0 0 auto; user-select: none;
      &:hover { color: #409eff; }
    }
  }
  .r-detail {
    margin: 0 8px 6px 30px;
    padding: 6px 10px;
    background: #fafbfc;
    border: 1px dashed #d9dce1;
    border-radius: 6px;
    font-size: 12px;
    .d-row { display: flex; gap: 8px; padding: 3px 0; align-items: flex-start; }
    .d-label { flex: 0 0 64px; color: #909399; }
    .d-value { flex: 1 1 auto; color: #4b5563; white-space: pre-wrap; word-break: break-all; line-height: 1.5; }
    .d-value.full { color: #1890ff; }
  }
  .base-url-form {
    margin: 4px 0 6px;
    .token {
      background: #f0f2f5;
      border: 1px solid #dcdfe6;
      border-radius: 4px;
      padding: 0 5px;
      font-size: 12px;
      color: #d6326e;
    }
    .form-tip { color: #909399; font-size: 12px; line-height: 1.5; margin-top: 2px; }
    .preview-code { font-size: 12px; word-break: break-all; }
  }
  .as-setup { margin: 10px 0; }
  .footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-top: 1px solid #ebeef5;
    padding-top: 12px;
  }
  .selected-text { color: #1890ff; font-size: 13px; }
}
</style>
