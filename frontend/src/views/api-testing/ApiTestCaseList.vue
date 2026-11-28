<template>
  <main class="api-case-list">
    <header class="page-heading">
      <div>
        <h1>{{ tx('caseListTitle', '接口测试用例') }}</h1>
        <p>{{ tx('caseListSubtitle', '管理可复用的接口步骤与异步执行记录') }}</p>
      </div>
      <el-button type="primary" @click="createCase">{{ tx('createCase', '新建用例') }}</el-button>
    </header>

    <el-alert v-if="pageError" :title="pageError" type="error" show-icon :closable="false" class="page-error" />
    <div class="filters" @keyup.enter="applyFilters">
      <el-select v-model="filters.project" clearable :placeholder="tx('filterProject', '全部项目')" @change="applyFilters">
        <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
      </el-select>
      <el-select v-model="filters.priority" clearable :placeholder="tx('filterPriority', '全部优先级')" @change="applyFilters">
        <el-option v-for="item in priorities" :key="item.value" :label="tx(item.key, item.fallback)" :value="item.value" />
      </el-select>
      <el-select v-model="filters.status" clearable :placeholder="tx('filterStatus', '全部状态')" @change="applyFilters">
        <el-option v-for="item in statuses" :key="item.value" :label="tx(item.key, item.fallback)" :value="item.value" />
      </el-select>
      <el-input v-model="filters.search" clearable :placeholder="tx('searchCases', '搜索用例名称')" />
      <el-button :loading="loading" @click="applyFilters">{{ tx('search', '搜索') }}</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" row-key="id" class="case-table">
      <el-table-column prop="name" :label="tx('caseName', '用例名称')" min-width="220" show-overflow-tooltip />
      <el-table-column :label="tx('project', '项目')" min-width="150">
        <template #default="{ row }">{{ projectName(row.project) }}</template>
      </el-table-column>
      <el-table-column prop="priority" :label="tx('priorityLabel', '优先级')" width="110">
        <template #default="{ row }"><el-tag :type="priorityType(row.priority)">{{ tx(`priority.${row.priority}`, row.priority) }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="status" :label="tx('status', '状态')" width="110">
        <template #default="{ row }"><el-tag :type="caseStatusType(row.status)">{{ tx(`caseStatus.${row.status}`, row.status) }}</el-tag></template>
      </el-table-column>
      <el-table-column :label="tx('stepCountLabel', '步骤数')" width="90">
        <template #default="{ row }">{{ row.steps?.length ?? row.step_count ?? 0 }}</template>
      </el-table-column>
      <el-table-column prop="updated_at" :label="tx('updatedAt', '更新时间')" min-width="170">
        <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column :label="tx('operation', '操作')" min-width="310" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="editCase(row)">{{ tx('editCase', '编辑') }}</el-button>
          <el-button link @click="showHistoryFor(row)">{{ tx('recentResult', '执行历史') }}</el-button>
          <el-button link :disabled="Boolean(executingCaseId || mutatingCaseId)" @click="runCase(row)">{{ tx('runCase', '执行') }}</el-button>
          <el-dropdown trigger="click" @command="command => handleMore(command, row)">
            <el-button link :disabled="Boolean(executingCaseId || mutatingCaseId)">{{ tx('moreActions', '更多') }}</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="duplicate">{{ tx('duplicateCase', '复制') }}</el-dropdown-item>
                <el-dropdown-item command="delete" divided>{{ tx('deleteCase', '删除') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>
      <template #empty><el-empty :description="pageError ? tx('casesUnavailable', '用例列表暂不可用') : tx('noCases', '暂无用例')" :image-size="64" /></template>
    </el-table>

    <el-pagination
      v-if="total > pageSize"
      v-model:current-page="page"
      :page-size="pageSize"
      :total="total"
      layout="total, prev, pager, next, jumper"
      @current-change="loadCases"
    />

    <ApiCaseRunPanel
      v-model:visible="showRunPanel"
      :execution="currentExecution"
      @update:execution="currentExecution = $event"
      @history="openSelectedHistory"
      @poll-limit="handlePollLimit"
    />
    <ApiCaseExecutionHistory
      v-model:visible="showHistory"
      :test-case-id="selectedCase?.id"
      @select="viewExecution"
    />
  </main>
</template>

<script setup>
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/utils/api'
import { deleteApiTestCase, duplicateApiTestCase, executeApiTestCase, getApiTestCase, listApiTestCases } from '@/api/api-test-cases'
import { fetchAllApiPages } from '@/utils/api-test-case-pagination.mjs'
import { apiCaseExecutionFingerprint, createApiCasePendingIdentityStore } from '@/utils/api-case-pending-identity.mjs'
import ApiCaseRunPanel from './components/ApiCaseRunPanel.vue'
import ApiCaseExecutionHistory from './components/ApiCaseExecutionHistory.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const pendingIdentityStore = createApiCasePendingIdentityStore()
const rows = ref([])
const projects = ref([])
const filters = reactive({ project: null, priority: '', status: '', search: '' })
const page = ref(1)
const pageSize = 20
const total = ref(0)
const loading = ref(false)
const pageError = ref('')
const selectedCase = ref(null)
const currentExecution = ref(null)
const showRunPanel = ref(false)
const showHistory = ref(false)
const executingCaseId = ref(null)
const mutatingCaseId = ref(null)
let alive = true
let listVersion = 0
let projectVersion = 0

const priorities = [
  { value: 'low', key: 'priority.low', fallback: '低' },
  { value: 'medium', key: 'priority.medium', fallback: '中' },
  { value: 'high', key: 'priority.high', fallback: '高' },
  { value: 'critical', key: 'priority.critical', fallback: '紧急' }
]
const statuses = [
  { value: 'draft', key: 'caseStatus.draft', fallback: '草稿' },
  { value: 'active', key: 'caseStatus.active', fallback: '启用' },
  { value: 'deprecated', key: 'caseStatus.deprecated', fallback: '废弃' }
]

function tx(key, fallback) {
  const fullKey = `apiTesting.cases.${key}`
  const translated = t(fullKey)
  return translated === fullKey ? fallback : translated
}
function responseData(response) { return response?.data ?? response }
function errorText(error) {
  const data = error?.response?.data
  const detail = data?.detail || data?.error
  return typeof detail === 'string' ? detail : data ? JSON.stringify(data) : error?.message || tx('operationFailed', '操作失败，请重试')
}
function projectName(projectId) {
  return projects.value.find(project => Number(project.id) === Number(projectId))?.name || String(projectId || '-')
}
function priorityType(priority) { return ({ low: 'info', medium: '', high: 'warning', critical: 'danger' })[priority] || 'info' }
function caseStatusType(status) { return ({ active: 'success', deprecated: 'warning', draft: 'info' })[status] || 'info' }
function formatDate(value) { return value ? new Date(value).toLocaleString() : '-' }

async function loadProjects() {
  const token = ++projectVersion
  try {
    const available = await fetchAllApiPages(api.get, '/api-testing/projects/', {})
    if (!alive || token !== projectVersion) return
    projects.value = available.filter(project => project.project_type !== 'WEBSOCKET')
    const queryProject = route.query?.project
    const requested = projects.value.find(project => Number(project.id) === Number(queryProject))
    if (filters.project == null) filters.project = requested?.id ?? projects.value[0]?.id ?? null
  } catch (error) {
    if (alive && token === projectVersion) pageError.value = errorText(error)
  }
}

async function loadCases() {
  const token = ++listVersion
  const params = { page: page.value, page_size: pageSize }
  if (filters.project != null && filters.project !== '') params.project = filters.project
  if (filters.priority) params.priority = filters.priority
  if (filters.status) params.status = filters.status
  if (filters.search.trim()) params.search = filters.search.trim()
  loading.value = true
  pageError.value = ''
  try {
    const data = responseData(await listApiTestCases(params))
    if (!alive || token !== listVersion) return
    rows.value = Array.isArray(data) ? data : data?.results || []
    total.value = Number(data?.count ?? rows.value.length)
  } catch (error) {
    if (alive && token === listVersion) pageError.value = errorText(error)
  } finally {
    if (alive && token === listVersion) loading.value = false
  }
}

async function initialize() {
  await loadProjects()
  if (alive) await loadCases()
}

function applyFilters() {
  page.value = 1
  return loadCases()
}
function createCase() { router.push('/api-testing/test-cases/create') }
function editCase(row) { router.push(`/api-testing/test-cases/${row.id}/edit`) }

async function handleMore(command, row) {
  if (command === 'duplicate') return duplicateCase(row)
  if (command === 'delete') return removeCase(row)
}

async function duplicateCase(row) {
  if (mutatingCaseId.value || executingCaseId.value) return
  mutatingCaseId.value = row.id
  pageError.value = ''
  try {
    const data = responseData(await duplicateApiTestCase(row.id))
    if (!alive) return
    ElMessage.success(tx('duplicateSucceeded', '已复制用例'))
    if (data?.id != null) await router.push(`/api-testing/test-cases/${data.id}/edit`)
    else await loadCases()
  } catch (error) {
    if (alive) pageError.value = errorText(error)
  } finally {
    if (alive) mutatingCaseId.value = null
  }
}

async function removeCase(row) {
  if (mutatingCaseId.value || executingCaseId.value) return
  try {
    await ElMessageBox.confirm(tx('deleteCaseConfirm', '确定删除此接口测试用例？'), tx('confirm', '确认'), { type: 'warning' })
  } catch {
    return
  }
  mutatingCaseId.value = row.id
  pageError.value = ''
  try {
    await deleteApiTestCase(row.id)
    if (!alive) return
    ElMessage.success(tx('deleteSucceeded', '已删除用例'))
    if (rows.value.length === 1 && page.value > 1) page.value -= 1
    await loadCases()
  } catch (error) {
    if (alive) pageError.value = errorText(error)
  } finally {
    if (alive) mutatingCaseId.value = null
  }
}

function idempotencyKey() {
  return globalThis.crypto?.randomUUID?.() || `api-case-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function currentActorId() {
  return userStore.user?.id == null ? '' : String(userStore.user.id)
}

async function runCase(row) {
  if (executingCaseId.value || mutatingCaseId.value) return
  const actorId = currentActorId()
  const caseId = row?.id == null ? '' : String(row.id)
  if (!actorId || !caseId) {
    pageError.value = tx('executionIdentityUnavailable', 'Cannot safely track this execution. Refresh the case and verify your signed-in project.')
    return
  }

  executingCaseId.value = row.id
  pageError.value = ''
  currentExecution.value = null
  try {
    const context = responseData(await getApiTestCase(caseId))
    if (!alive || currentActorId() !== actorId) return
    const contextCaseId = context?.id == null ? '' : String(context.id)
    const projectValue = context?.project_id ?? context?.project?.id ?? context?.project
    const projectId = projectValue == null ? '' : String(projectValue)
    if (contextCaseId !== caseId || !projectId) {
      throw new Error(tx('executionIdentityUnavailable', 'Cannot safely track this execution. Refresh the case and verify your signed-in project.'))
    }

    const fingerprint = await apiCaseExecutionFingerprint(context)
    if (!fingerprint) {
      throw new Error(tx('executionIdentityUnavailable', 'Cannot safely track this execution. Refresh the case and verify your signed-in project.'))
    }
    if (!alive || currentActorId() !== actorId) return
    const identity = { actorId, projectId, caseId, fingerprint }
    const key = pendingIdentityStore.get(identity) || pendingIdentityStore.remember(identity, idempotencyKey())
    if (!key) throw new Error(tx('executionIdentityUnavailable', 'Cannot safely track this execution. Refresh the case and verify your signed-in project.'))

    selectedCase.value = context
    try {
      let data
      try {
        data = responseData(await executeApiTestCase(row.id, {
          idempotency_key: key,
          execution_fingerprint: fingerprint,
        }))
      } catch (error) {
        const status = Number(error?.response?.status)
        const uncertain = !status || status >= 500 || status === 408
        if (!uncertain) pendingIdentityStore.forget(identity, key)
        if (!alive || currentActorId() !== actorId) return
        pageError.value = uncertain ? `${errorText(error)} ${tx('refreshBeforeRetry', '执行状态未知，请先检查历史再重试。')}` : errorText(error)
        if (uncertain) showHistoryFor(context)
        else showHistory.value = false
        return
      }

      pendingIdentityStore.forget(identity, key)
      if (!alive || currentActorId() !== actorId) return
      currentExecution.value = data
      showRunPanel.value = true
      await loadCases()
    } catch (error) {
      if (alive && currentActorId() === actorId) pageError.value = errorText(error)
    }
  } catch (error) {
    if (alive && currentActorId() === actorId) pageError.value = errorText(error)
  } finally {
    if (alive) executingCaseId.value = null
  }
}

function showHistoryFor(row) {
  selectedCase.value = row
  showHistory.value = true
}
function openSelectedHistory() {
  showRunPanel.value = false
  if (selectedCase.value) showHistory.value = true
}
function viewExecution(execution) {
  currentExecution.value = execution
  showHistory.value = false
  showRunPanel.value = true
}
function handlePollLimit() {
  pageError.value = tx('pollLimitReached', '执行仍在进行，请刷新历史查看最新状态。')
}

onMounted(initialize)
onBeforeUnmount(() => {
  alive = false
  listVersion += 1
  projectVersion += 1
})
</script>

<style scoped>
.api-case-list { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.page-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
h1 { margin: 0; font-size: 22px; font-weight: 600; }
.page-heading p { margin: 6px 0 0; color: var(--el-text-color-secondary); }
.filters { display: grid; grid-template-columns: minmax(170px, 1fr) repeat(2, minmax(140px, 0.7fr)) minmax(180px, 1.4fr) auto; gap: 10px; align-items: center; }
.case-table { width: 100%; }
.el-pagination { justify-content: flex-end; }
.page-error { margin: 0; }
@media (max-width: 900px) { .filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .page-heading { align-items: stretch; flex-direction: column; } .filters { grid-template-columns: 1fr; } }
</style>
