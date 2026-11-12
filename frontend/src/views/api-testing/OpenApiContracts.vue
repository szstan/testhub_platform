<template>
  <div class="contract-page">
    <div class="page-header">
      <div>
        <h1>{{ t('execution.contracts.title') }}</h1>
        <p>{{ t('execution.contracts.subtitle') }}</p>
      </div>
      <div class="header-actions">
        <el-button :loading="loading" @click="reloadPage">{{ t('common.refresh') }}</el-button>
        <el-button type="primary" :disabled="permissionDenied || !projects.length" @click="openCreate">{{ t('execution.contracts.create') }}</el-button>
      </div>
    </div>

    <el-alert v-if="pageError" :title="pageError" :type="permissionDenied ? 'warning' : 'error'" show-icon :closable="false" class="page-alert">
      <template #default>
        <el-button v-if="!permissionDenied" link type="primary" @click="loadSpecs">{{ t('execution.contracts.retryLoad') }}</el-button>
      </template>
    </el-alert>

    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent="applyFilters">
        <el-form-item :label="t('execution.contracts.project')">
          <el-select v-model="filters.project" clearable filterable :placeholder="t('execution.contracts.allProjects')" @change="applyFilters">
            <el-option v-for="project in projects" :key="project.id" :label="`${project.name} (#${project.id})`" :value="project.id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('execution.contracts.search')">
          <el-input v-model="filters.search" clearable :placeholder="t('execution.contracts.searchPlaceholder')" @keyup.enter="applyFilters" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="applyFilters">{{ t('common.search') }}</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <el-table :data="visibleSpecs" v-loading="loading" row-key="id" highlight-current-row @row-click="selectSpec">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column :label="t('execution.contracts.project')" min-width="170">
          <template #default="{ row }">{{ projectName(row.project) }}</template>
        </el-table-column>
        <el-table-column prop="name" :label="t('execution.contracts.name')" min-width="180" />
        <el-table-column prop="version" :label="t('execution.contracts.version')" width="120" />
        <el-table-column prop="document_hash" :label="t('execution.contracts.documentHash')" min-width="190" show-overflow-tooltip />
        <el-table-column prop="updated_at" :label="t('execution.contracts.updatedAt')" min-width="170" />
        <el-table-column :label="t('execution.contracts.actions')" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="selectSpec(row)">{{ t('execution.contracts.open') }}</el-button>
            <el-button link type="primary" :disabled="permissionDenied" @click.stop="openEdit(row)">{{ t('common.edit') }}</el-button>
            <el-button link type="danger" :disabled="permissionDenied" @click.stop="removeSpec(row)">{{ t('common.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty><el-empty :description="loading ? '' : t('execution.contracts.empty')" /></template>
      </el-table>
      <el-pagination
        v-model:current-page="pagination.page"
        :page-size="pagination.pageSize"
        :page-sizes="[10, 20, 50]"
        :total="filteredSpecs.length"
        layout="total, sizes, prev, pager, next"
        class="pagination"
        @current-change="changePage"
        @size-change="changePageSize"
      />
    </el-card>

    <el-card v-if="selectedSpec" shadow="never" class="operation-card">
      <template #header>
        <div class="operation-header">
          <strong>{{ selectedSpec.name }} · {{ selectedSpec.version }}</strong>
          <el-button link @click="selectedSpec = null">{{ t('common.close') }}</el-button>
        </div>
      </template>
      <el-tabs v-model="activeOperation">
        <el-tab-pane :label="t('execution.contracts.coverage')" name="coverage">
          <el-button :loading="operationLoading" :disabled="permissionDenied || operationPermissionDenied" @click="runCoverage">{{ t('execution.contracts.loadCoverage') }}</el-button>
        </el-tab-pane>
        <el-tab-pane :label="t('execution.contracts.diff')" name="diff">
          <el-form :inline="true">
            <el-form-item :label="t('execution.contracts.baseline')">
              <el-select v-model="baselineId" filterable :placeholder="t('execution.contracts.selectBaseline')">
                <el-option v-for="spec in baselines" :key="spec.id" :label="`${spec.name} · ${spec.version} (#${spec.id})`" :value="spec.id" />
              </el-select>
            </el-form-item>
            <el-button type="primary" :loading="operationLoading" :disabled="!baselineId || permissionDenied || operationPermissionDenied" @click="runDiff">{{ t('execution.contracts.compare') }}</el-button>
          </el-form>
        </el-tab-pane>
        <el-tab-pane :label="t('execution.contracts.cases')" name="cases">
          <el-button :loading="operationLoading" :disabled="permissionDenied || operationPermissionDenied" @click="previewCases">{{ t('execution.contracts.previewCases') }}</el-button>
        </el-tab-pane>
        <el-tab-pane :label="t('execution.contracts.materialize')" name="materialize">
          <el-form :inline="true">
            <el-form-item :label="t('execution.contracts.caseKinds')">
              <el-checkbox-group v-model="caseKinds">
                <el-checkbox v-for="kind in contractKinds" :key="kind" :label="kind">{{ t(`execution.contracts.kinds.${kind}`) }}</el-checkbox>
              </el-checkbox-group>
            </el-form-item>
            <el-form-item :label="t('execution.contracts.limit')"><el-input-number v-model="materializeLimit" :min="1" :max="500" /></el-form-item>
            <el-button type="primary" :loading="operationLoading" :disabled="permissionDenied || operationPermissionDenied" @click="materializeCases">{{ t('execution.contracts.materializeAction') }}</el-button>
          </el-form>
        </el-tab-pane>
        <el-tab-pane :label="t('execution.contracts.execute')" name="execute">
          <el-form label-position="top" class="execute-form">
            <el-form-item :label="t('execution.contracts.job')">
              <div class="job-picker">
                <el-select v-model="executeForm.job_id" filterable :loading="jobsLoading" :disabled="creatingJob || operationLoading" :placeholder="t('execution.contracts.selectJob')" @visible-change="visible => visible && loadJobs()">
                  <el-option v-for="job in jobs" :key="job.id" :label="`#${job.id} · ${job.source_type} · ${job.status}`" :value="job.id" />
                </el-select>
                <el-button :loading="creatingJob" :disabled="jobsLoading || operationLoading || permissionDenied || operationPermissionDenied" @click="createJob">{{ t('execution.contracts.createJob') }}</el-button>
              </div>
            </el-form-item>
            <el-alert v-if="jobsError" :title="jobsError" type="error" show-icon :closable="false" class="page-alert">
              <template #default><el-button link type="primary" :disabled="creatingJob || operationLoading" @click="loadJobs">{{ t('execution.contracts.retryLoad') }}</el-button></template>
            </el-alert>
            <el-form-item :label="t('execution.contracts.baseUrl')"><el-input v-model="executeForm.base_url" :placeholder="serverUrl" /></el-form-item>
            <el-form-item :label="t('execution.contracts.headers')"><el-input v-model="executeForm.headers_text" type="textarea" :rows="3" placeholder='{"Authorization":"Bearer ..."}' /></el-form-item>
            <el-form-item :label="t('execution.contracts.caseKinds')">
              <el-checkbox-group v-model="executeForm.kinds">
                <el-checkbox v-for="kind in contractKinds" :key="kind" :label="kind">{{ t(`execution.contracts.kinds.${kind}`) }}</el-checkbox>
              </el-checkbox-group>
            </el-form-item>
            <div class="execute-options">
              <el-form-item :label="t('execution.contracts.limit')"><el-input-number v-model="executeForm.limit" :min="1" :max="10" /></el-form-item>
              <el-form-item :label="t('execution.contracts.timeout')"><el-input-number v-model="executeForm.timeout" :min="0.1" :precision="1" :max="5" /></el-form-item>
              <el-button type="primary" :loading="operationLoading" :disabled="!executeForm.job_id || jobsLoading || creatingJob || permissionDenied || operationPermissionDenied" @click="executeContract">{{ t('execution.contracts.executeAction') }}</el-button>
            </div>
          </el-form>
        </el-tab-pane>
      </el-tabs>
      <el-alert v-if="operationError" :title="operationError" :type="operationPermissionDenied ? 'warning' : 'error'" show-icon :closable="false" class="page-alert" />
      <el-skeleton v-if="operationLoading" :rows="4" animated />
      <el-empty v-else-if="!operationResult" :description="t('execution.contracts.noResult')" />
      <pre v-else class="result-output">{{ formatResult(operationResult) }}</pre>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editing ? t('execution.contracts.edit') : t('execution.contracts.create')" width="760px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item :label="t('execution.contracts.project')" required>
          <el-select v-model="form.project" filterable :disabled="Boolean(editing)" :placeholder="t('execution.contracts.selectProject')">
            <el-option v-for="project in projects" :key="project.id" :label="`${project.name} (#${project.id})`" :value="project.id" />
          </el-select>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="16"><el-form-item :label="t('execution.contracts.name')" required><el-input v-model="form.name" maxlength="200" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item :label="t('execution.contracts.version')" required><el-input v-model="form.version" maxlength="100" /></el-form-item></el-col>
        </el-row>
        <el-form-item :label="t('execution.contracts.document')" required>
          <el-input v-model="form.document_text" type="textarea" :rows="16" spellcheck="false" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" :disabled="permissionDenied" @click="saveSpec">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import {
  createOpenApiContractSpec,
  deleteOpenApiContractSpec,
  diffOpenApiContractSpecs,
  executeOpenApiContractSpec,
  getOpenApiContractCoverage,
  listGeneratedOpenApiContractCases,
  listOpenApiContractSpecs,
  materializeOpenApiContractCases,
  updateOpenApiContractSpec,
} from '@/api/contracts'
import { createExecutionJob, listExecutionJobs, listExecutionProjects } from '@/api/executions'
import { useUserStore } from '@/stores/user'

const { t } = useI18n()
const userStore = useUserStore()
const projects = ref([])
const specs = ref([])
const jobs = ref([])
const loading = ref(false)
const jobsLoading = ref(false)
const creatingJob = ref(false)
const jobsError = ref('')
let operationRequestId = 0
let jobsRequestId = 0
let createJobRequestId = 0
const saving = ref(false)
const operationLoading = ref(false)
const permissionDenied = ref(false)
const operationPermissionDenied = ref(false)
const pageError = ref('')
const operationError = ref('')
const selectedSpec = ref(null)
const operationResult = ref(null)
const activeOperation = ref('coverage')
const baselineId = ref(null)
const dialogVisible = ref(false)
const editing = ref(null)
const filters = ref({ project: null, search: '' })
const pagination = ref({ page: 1, pageSize: 20, total: 0 })
const contractKinds = ['positive', 'negative', 'security']
const caseKinds = ref([...contractKinds])
const materializeLimit = ref(200)
const executeForm = ref({ job_id: null, base_url: '', headers_text: '{}', kinds: [...contractKinds], limit: 10, timeout: 5 })
const form = ref({ project: null, name: '', version: '', document_text: '{\n  "openapi": "3.0.3",\n  "info": { "title": "API", "version": "1.0.0" },\n  "paths": {}\n}' })

const filteredSpecs = computed(() => specs.value.filter(spec => {
  const matchesProject = !filters.value.project || Number(spec.project) === Number(filters.value.project)
  const query = filters.value.search.trim().toLowerCase()
  return matchesProject && (!query || `${spec.name} ${spec.version} ${spec.id}`.toLowerCase().includes(query))
}))
const visibleSpecs = computed(() => filteredSpecs.value.slice((pagination.value.page - 1) * pagination.value.pageSize, pagination.value.page * pagination.value.pageSize))
const baselines = computed(() => specs.value.filter(spec => Number(spec.project) === Number(selectedSpec.value?.project) && spec.id !== selectedSpec.value?.id))
const serverUrl = computed(() => selectedSpec.value?.document?.servers?.[0]?.url || '')

const listRows = response => {
  const data = response.data || {}
  return { rows: data.results || (Array.isArray(data) ? data : []), count: Number(data.count ?? (Array.isArray(data) ? data.length : 0)) }
}
const projectName = id => projects.value.find(project => Number(project.id) === Number(id))?.name || `#${id}`
const formatResult = value => JSON.stringify(value, null, 2)
const errorText = error => {
  const detail = error.response?.data?.detail
  const fallback = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : error.message || t('execution.contracts.loadFailed')
  return error.response?.status === 401
    ? t('execution.contracts.authRequired')
    : error.response?.status === 403 || error.response?.status === 404
      ? t('execution.contracts.permissionDenied')
      : fallback
}

const loadProjects = async () => {
  try {
    const response = await listExecutionProjects({ page_size: 100, ordering: 'name' })
    projects.value = listRows(response).rows
    return true
  } catch (error) {
    pageError.value = errorText(error)
    permissionDenied.value = [401, 403, 404].includes(error.response?.status)
    return false
  }
}
const reloadPage = async () => {
  pageError.value = ''
  permissionDenied.value = false
  if (await loadProjects()) await loadSpecs()
}
const loadSpecs = async () => {
  loading.value = true
  pageError.value = ''
  permissionDenied.value = false
  try {
    const firstResponse = await listOpenApiContractSpecs({ page: 1 })
    const firstPage = listRows(firstResponse)
    const allSpecs = [...firstPage.rows]
    const lastPage = Math.ceil(firstPage.count / 20)
    for (let startPage = 2; startPage <= lastPage; startPage += 5) {
      const pageNumbers = Array.from({ length: Math.min(5, lastPage - startPage + 1) }, (_, index) => startPage + index)
      const responses = await Promise.all(pageNumbers.map(page => listOpenApiContractSpecs({ page })))
      responses.forEach(response => allSpecs.push(...listRows(response).rows))
    }
    specs.value = allSpecs
    pagination.value.total = allSpecs.length
    if (selectedSpec.value) selectedSpec.value = specs.value.find(spec => spec.id === selectedSpec.value.id) || null
  } catch (error) {
    pageError.value = errorText(error)
    permissionDenied.value = [401, 403, 404].includes(error.response?.status)
  } finally {
    loading.value = false
  }
}
const applyFilters = () => {
  pagination.value.page = 1
}
const changePage = page => { pagination.value.page = page }
const changePageSize = pageSize => {
  pagination.value.page = 1
  pagination.value.pageSize = pageSize
}
const selectSpec = row => {
  selectedSpec.value = row
  baselineId.value = baselines.value[0]?.id || null
  operationResult.value = null
  operationError.value = ''
  operationPermissionDenied.value = false
  executeForm.value = { ...executeForm.value, job_id: null, base_url: '', headers_text: '{}', kinds: [...contractKinds] }
  activeOperation.value = 'coverage'
  runCoverage()
}
const openCreate = () => {
  editing.value = null
  form.value = { project: filters.value.project, name: '', version: '', document_text: '{\n  "openapi": "3.0.3",\n  "info": { "title": "API", "version": "1.0.0" },\n  "paths": {}\n}' }
  dialogVisible.value = true
}
const openEdit = row => {
  editing.value = row.id
  form.value = { project: row.project, name: row.name, version: row.version, document_text: JSON.stringify(row.document || {}, null, 2) }
  dialogVisible.value = true
}
const saveSpec = async () => {
  let document
  try {
    document = JSON.parse(form.value.document_text)
  } catch {
    ElMessage.error(t('execution.contracts.invalidJson'))
    return
  }
  if (!form.value.project || !form.value.name.trim() || !form.value.version.trim()) {
    ElMessage.warning(t('execution.contracts.requiredFields'))
    return
  }
  saving.value = true
  try {
    const payload = { name: form.value.name.trim(), version: form.value.version.trim(), document }
    if (editing.value) await updateOpenApiContractSpec(editing.value, payload)
    else await createOpenApiContractSpec({ ...payload, project: form.value.project })
    dialogVisible.value = false
    await loadSpecs()
    ElMessage.success(t('execution.contracts.saveSuccess'))
  } catch (error) {
    pageError.value = errorText(error)
    permissionDenied.value = [401, 403, 404].includes(error.response?.status)
  } finally {
    saving.value = false
  }
}
const removeSpec = async row => {
  try {
    await ElMessageBox.confirm(t('execution.contracts.deleteConfirm', { name: row.name, version: row.version }), t('common.confirm'), { type: 'warning' })
    await deleteOpenApiContractSpec(row.id)
    if (selectedSpec.value?.id === row.id) selectedSpec.value = null
    await loadSpecs()
    ElMessage.success(t('execution.contracts.deleteSuccess'))
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') pageError.value = errorText(error)
    permissionDenied.value = [401, 403, 404].includes(error.response?.status)
  }
}
const runOperation = async request => {
  if (!selectedSpec.value) return
  const requestId = ++operationRequestId
  const specId = selectedSpec.value.id
  operationLoading.value = true
  operationResult.value = null
  operationError.value = ''
  operationPermissionDenied.value = false
  try {
    const response = await request(specId)
    if (requestId !== operationRequestId) return
    operationResult.value = response.data
    return response
  } catch (error) {
    if (requestId !== operationRequestId) return
    operationError.value = errorText(error)
    operationPermissionDenied.value = [401, 403, 404].includes(error.response?.status)
  } finally {
    if (requestId === operationRequestId) operationLoading.value = false
  }
}
const clearOperationResult = () => {
  operationRequestId += 1
  operationLoading.value = false
  operationResult.value = null
  operationError.value = ''
  operationPermissionDenied.value = false
}
const invalidateContext = () => {
  clearOperationResult()
  jobsRequestId += 1
  createJobRequestId += 1
  jobsLoading.value = false
  creatingJob.value = false
  jobsError.value = ''
  jobs.value = []
  executeForm.value.job_id = null
}
watch(() => [userStore.user?.id, selectedSpec.value?.id, selectedSpec.value?.project, selectedSpec.value?.version, selectedSpec.value?.document_hash, activeOperation.value], (current, previous) => {
  if (current.some((value, index) => value !== previous[index])) invalidateContext()
}, { flush: 'sync' })
onBeforeUnmount(invalidateContext)
const runCoverage = () => runOperation(getOpenApiContractCoverage)
const runDiff = () => {
  if (!baselineId.value) return
  return runOperation(id => diffOpenApiContractSpecs(id, baselineId.value))
}
const previewCases = () => runOperation(listGeneratedOpenApiContractCases)
const ensureCaseKindsSelected = kinds => {
  if (kinds.length) return true
  ElMessage.warning(t('execution.contracts.selectAtLeastOneKind'))
  return false
}
const materializeCases = () => {
  if (!ensureCaseKindsSelected(caseKinds.value)) return
  return runOperation(id => materializeOpenApiContractCases(id, { kinds: caseKinds.value, limit: materializeLimit.value }))
}
const isEligibleJob = (job, project) => job.status === 'pending' && !job.final_attempt && Number(job.attempts_count) === 0 && Number(job.project) === Number(project)
const loadJobs = async () => {
  if (!selectedSpec.value || jobsLoading.value || creatingJob.value || operationLoading.value) return
  const requestId = ++jobsRequestId
  const project = selectedSpec.value.project
  jobsLoading.value = true
  jobsError.value = ''
  try {
    const params = { project, contract_eligible: true, status: 'pending', page_size: 100, ordering: '-created_at' }
    const firstPage = listRows(await listExecutionJobs({ ...params, page: 1 }))
    if (requestId !== jobsRequestId) return
    const rows = [...firstPage.rows]
    const lastPage = Math.ceil(firstPage.count / params.page_size)
    for (let page = 2; page <= lastPage; page += 1) {
      const nextPage = listRows(await listExecutionJobs({ ...params, page }))
      if (requestId !== jobsRequestId) return
      rows.push(...nextPage.rows)
    }
    jobs.value = rows.filter(job => isEligibleJob(job, project))
    if (!jobs.value.some(job => job.id === executeForm.value.job_id)) executeForm.value.job_id = null
  } catch (error) {
    if (requestId === jobsRequestId) jobsError.value = errorText(error)
  } finally {
    if (requestId === jobsRequestId) jobsLoading.value = false
  }
}
const createJob = async () => {
  if (!selectedSpec.value || creatingJob.value || jobsLoading.value || operationLoading.value || permissionDenied.value || operationPermissionDenied.value) return
  const requestId = ++createJobRequestId
  const spec = selectedSpec.value
  const actorId = userStore.user?.id
  if (!actorId) {
    jobsError.value = t('execution.contracts.authRequired')
    return
  }
  const storageKey = `testhub:contract-job:${actorId}:${spec.project}:${spec.id}`
  let storage
  let idempotencyKey
  try {
    storage = globalThis.sessionStorage
    idempotencyKey = storage.getItem(storageKey)
    if (!idempotencyKey) {
      const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`
      idempotencyKey = `openapi-contract:${actorId}:${spec.id}:${suffix}`
      // Persist before sending: a lost reply must remain retryable after reload.
      storage.setItem(storageKey, idempotencyKey)
    }
  } catch {
    jobsError.value = t('execution.contracts.storageUnavailable')
    return
  }
  creatingJob.value = true
  jobsError.value = ''
  try {
    // This job is executed by the contract endpoint, not a legacy source adapter.
    const response = await createExecutionJob({
      project: spec.project,
      source_type: 'manual',
      source_id: `openapi-contract:${spec.id}`,
      trigger: 'manual',
      idempotency_key: idempotencyKey,
    })
    if (requestId !== createJobRequestId) return
    try {
      if (storage.getItem(storageKey) === idempotencyKey) storage.removeItem(storageKey)
    } catch {
      // The job already exists. Preserve its selection even if local cleanup fails.
      jobsError.value = t('execution.contracts.storageUnavailable')
    }
    const job = response.data
    if (!isEligibleJob(job, spec.project)) {
      jobsError.value = t('execution.contracts.jobUnavailable')
      return
    }
    jobs.value = [job, ...jobs.value.filter(item => item.id !== job.id)]
    executeForm.value.job_id = job.id
    ElMessage.success(t('execution.contracts.jobCreated'))
  } catch (error) {
    if (requestId === createJobRequestId) jobsError.value = errorText(error)
  } finally {
    if (requestId === createJobRequestId) creatingJob.value = false
  }
}
const executeContract = async () => {
  if (operationLoading.value || creatingJob.value || jobsLoading.value || !selectedSpec.value) return
  const jobId = executeForm.value.job_id
  if (!jobs.value.some(job => job.id === jobId && isEligibleJob(job, selectedSpec.value.project))) {
    jobsError.value = t('execution.contracts.selectJob')
    return
  }
  if (!ensureCaseKindsSelected(executeForm.value.kinds)) return
  let headers
  try {
    headers = JSON.parse(executeForm.value.headers_text || '{}')
    if (!headers || Array.isArray(headers) || typeof headers !== 'object') throw new Error('invalid headers')
  } catch {
    ElMessage.error(t('execution.contracts.invalidHeaders'))
    return
  }
  const specId = selectedSpec.value.id
  const pending = runOperation(id => executeOpenApiContractSpec(id, {
    job_id: jobId,
    base_url: executeForm.value.base_url.trim(),
    headers,
    kinds: executeForm.value.kinds,
    limit: executeForm.value.limit,
    timeout: executeForm.value.timeout,
  }))
  const requestId = operationRequestId
  const response = await pending
  const isCurrent = () => requestId === operationRequestId && selectedSpec.value?.id === specId && activeOperation.value === 'execute'
  if (!isCurrent() || executeForm.value.job_id !== jobId) return
  // A lost response may still have consumed the job. Do not replay a stale snapshot.
  jobs.value = jobs.value.filter(job => job.id !== jobId)
  executeForm.value.job_id = null
  if (!response) {
    await loadJobs()
    if (isCurrent() && !jobsError.value && jobs.value.some(job => job.id === jobId)) executeForm.value.job_id = jobId
  }
}

onMounted(reloadPage)
</script>

<style scoped>
.contract-page { display: grid; gap: 16px; }
.page-header, .operation-header, .header-actions, .execute-options { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.page-header h1 { margin: 0; font-size: 22px; }
.page-header p { margin: 6px 0 0; color: var(--el-text-color-secondary); }
.header-actions { justify-content: flex-end; }
.filter-card { margin-bottom: 0; }
.page-alert { margin: 0 0 12px; }
.pagination { justify-content: flex-end; margin-top: 16px; }
.operation-card { min-width: 0; }
.execute-form { max-width: 840px; }
.job-picker { display: flex; gap: 10px; width: 100%; }
.job-picker .el-select { flex: 1; min-width: 0; }
.execute-options { justify-content: flex-start; flex-wrap: wrap; }
.result-output { max-height: 440px; overflow: auto; padding: 12px; background: var(--el-fill-color-light); border-radius: 4px; font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 768px) {
  .page-header { align-items: flex-start; flex-direction: column; }
  .header-actions { justify-content: flex-start; }
  .execute-options { align-items: flex-start; }
}
</style>
