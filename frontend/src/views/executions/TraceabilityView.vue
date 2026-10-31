<template>
  <div class="traceability-view">
    <div class="page-header">
      <div>
        <h1>{{ $t('execution.traceability.title') }}</h1>
        <p class="page-subtitle">{{ $t('execution.traceability.subtitle') }}</p>
      </div>
      <el-button type="primary" @click="applyFilters">{{ $t('common.search') }}</el-button>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent="applyFilters">
        <el-form-item :label="$t('execution.traceability.projectId')">
          <el-input-number v-model="projectId" :min="1" :controls="false" />
        </el-form-item>
        <el-form-item :label="$t('execution.traceability.versionId')">
          <el-input-number v-model="versionId" :min="1" :controls="false" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-row :gutter="16" v-loading="coverageLoading" class="summary-row">
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.totalRequirements') }}</span><strong>{{ coverage.total_requirements }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.linkedRequirements') }}</span><strong>{{ coverage.linked_requirements }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.executedRequirements') }}</span><strong>{{ coverage.executed_requirements }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.passedRequirements') }}</span><strong>{{ coverage.passed_requirements }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.unexecutedRequirements') }}</span><strong>{{ coverage.unexecuted_requirements?.length || 0 }}</strong></div></el-card></el-col>
    </el-row>

    <el-card shadow="never">
      <template #header>{{ $t('execution.traceability.builds') }}</template>
      <el-table :data="builds" v-loading="buildLoading">
        <el-table-column prop="name" :label="$t('execution.traceability.buildName')" />
        <el-table-column prop="build_number" :label="$t('execution.traceability.buildNumber')" />
        <el-table-column prop="branch" :label="$t('execution.traceability.branch')" />
        <el-table-column prop="commit_sha" label="SHA" show-overflow-tooltip />
        <el-table-column prop="environment" :label="$t('execution.traceability.environment')" />
        <el-table-column prop="status" :label="$t('execution.taskCenter.status')" />
        <el-table-column prop="created_at" :label="$t('execution.traceability.createdAt')" />
      </el-table>
      <div class="pagination">
        <el-pagination
          :current-page="pagination.page"
          :page-size="pagination.pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="pagination.total"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="changeBuildPage"
          @size-change="changeBuildPageSize"
        />
      </div>
      <el-empty v-if="!buildLoading && builds.length === 0" :description="$t('execution.traceability.noBuilds')" />
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getExecutionCoverage, listExecutionBuilds } from '@/api/executions'

const projectId = ref(null)
const versionId = ref(null)
const appliedFilters = ref({ project: null, version: null })
const buildLoading = ref(false)
const coverageLoading = ref(false)
const builds = ref([])
const pagination = ref({ page: 1, pageSize: 20, total: 0 })
let buildRequestId = 0
let coverageRequestId = 0
const coverage = ref({ total_requirements: 0, covered_requirements: 0, linked_requirements: 0, executed_requirements: 0, passed_requirements: 0, unexecuted_requirements: [], coverage_rate: 0 })

const pageData = response => {
  const data = response.data || {}
  const results = data.results || (Array.isArray(data) ? data : [])
  return { results, total: Number(data.count ?? results.length) }
}

const loadBuilds = async () => {
  const requestId = ++buildRequestId
  const { project, version } = appliedFilters.value
  if (!project) {
    builds.value = []
    pagination.value.total = 0
    buildLoading.value = false
    return
  }
  const pageSize = pagination.value.pageSize
  let page = pagination.value.page
  const filters = { project, version: version || undefined, ordering: '-created_at,-build_number' }
  buildLoading.value = true
  try {
    const fetchPage = currentPage => listExecutionBuilds({ ...filters, page: currentPage, page_size: pageSize })
    let response = await fetchPage(page)
    if (requestId !== buildRequestId) return
    let { results, total } = pageData(response)
    const lastPage = Math.max(1, Math.ceil(total / pageSize))
    if (page > lastPage) {
      page = lastPage
      pagination.value.page = page
      response = await fetchPage(page)
      if (requestId !== buildRequestId) return
      const nextPageData = pageData(response)
      results = nextPageData.results
      total = nextPageData.total
    }
    builds.value = results
    pagination.value.total = total
  } catch (error) {
    if (requestId === buildRequestId) ElMessage.error(error.response?.data?.detail || '加载追踪构建失败')
  } finally {
    if (requestId === buildRequestId) buildLoading.value = false
  }
}

const loadCoverage = async () => {
  const requestId = ++coverageRequestId
  const { project, version } = appliedFilters.value
  if (!project) return
  coverageLoading.value = true
  try {
    const response = await getExecutionCoverage(project, version)
    if (requestId === coverageRequestId) coverage.value = response.data || coverage.value
  } catch (error) {
    if (requestId === coverageRequestId) ElMessage.error(error.response?.data?.detail || '加载覆盖率失败')
  } finally {
    if (requestId === coverageRequestId) coverageLoading.value = false
  }
}

const loadTraceability = () => Promise.all([loadBuilds(), loadCoverage()])
const applyFilters = () => {
  if (!projectId.value) {
    ElMessage.warning('请输入项目 ID')
    return
  }
  appliedFilters.value = { project: projectId.value, version: versionId.value }
  pagination.value.page = 1
  loadTraceability()
}
const changeBuildPage = page => { if (page === pagination.value.page) return; pagination.value.page = page; loadBuilds() }
const changeBuildPageSize = pageSize => { pagination.value.pageSize = pageSize; pagination.value.page = 1; loadBuilds() }
</script>

<style scoped>
.traceability-view { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.page-subtitle { color: var(--el-text-color-secondary); }
.filter-card { margin-bottom: 16px; }
.summary-row { margin-bottom: 16px; }
.pagination { display: flex; justify-content: flex-end; margin-top: 16px; }
.metric { display: flex; align-items: center; justify-content: space-between; color: var(--el-text-color-secondary); }
.metric strong { font-size: 28px; color: var(--el-text-color-primary); }
</style>
