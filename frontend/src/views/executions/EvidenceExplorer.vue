<template>
  <div class="evidence-page">
    <div class="page-header">
      <div>
        <p class="eyebrow">{{ t('execution.evidence.eyebrow') }}</p>
        <h1>{{ t('execution.evidence.title') }}</h1>
        <p class="subtitle">{{ t('execution.evidence.subtitle') }}</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">{{ t('common.refresh') }}</el-button>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent="applyFilters">
        <el-form-item :label="t('execution.evidence.project')"><el-input-number v-model="filters.project" :min="1" :precision="0" :controls="false" /></el-form-item>
        <el-form-item :label="t('execution.evidence.job')"><el-input-number v-model="filters.job" :min="1" :precision="0" :controls="false" /></el-form-item>
        <el-form-item :label="t('execution.evidence.run')"><el-input-number v-model="filters.quality_run" :min="1" :precision="0" :controls="false" /></el-form-item>
        <el-form-item :label="t('execution.evidence.caseKey')"><el-input v-model="filters.case_key" clearable /></el-form-item>
        <el-form-item :label="t('execution.evidence.mediaType')"><el-input v-model="filters.media_type" clearable /></el-form-item>
        <el-form-item :label="t('execution.evidence.evidenceType')"><el-input v-model="filters.evidence_type" clearable /></el-form-item>
        <el-form-item :label="t('execution.evidence.qualityAvailability')"><el-input v-model="filters.availability" clearable /></el-form-item>
        <el-button type="primary" @click="applyFilters">{{ t('common.search') }}</el-button>
      </el-form>
    </el-card>

    <el-row :gutter="16" class="summary-row">
      <el-col :xs="12" :md="6"><el-card shadow="never"><div class="metric"><span>{{ t('execution.evidence.executionArtifacts') }}</span><strong>{{ artifactPage.total }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="6"><el-card shadow="never"><div class="metric"><span>{{ t('execution.evidence.qualityEvidence') }}</span><strong>{{ qualityPage.total }}</strong></div></el-card></el-col>
    </el-row>

    <el-card shadow="never" class="table-card">
      <template #header>{{ t('execution.evidence.executionArtifacts') }}</template>
      <el-table :data="data.execution_artifacts" v-loading="loading">
        <el-table-column prop="job" :label="t('execution.evidence.job')" width="80" />
        <el-table-column prop="attempt" :label="t('execution.evidence.attempt')" width="90" />
        <el-table-column prop="step_result" :label="t('execution.evidence.step')" width="100" />
        <el-table-column prop="media_type" :label="t('execution.evidence.mediaType')" width="180" />
        <el-table-column prop="uri" :label="t('execution.evidence.reference')" min-width="260" show-overflow-tooltip />
        <el-table-column prop="content_hash" :label="t('execution.evidence.hash')" min-width="150" show-overflow-tooltip />
        <el-table-column prop="retention_policy" :label="t('execution.evidence.retention')" width="110" />
        <template #empty><el-empty :description="t('execution.evidence.empty')" /></template>
      </el-table>
      <el-pagination
        v-model:current-page="artifactPage.page" v-model:page-size="artifactPage.pageSize"
        :total="artifactPage.total" :page-sizes="[20, 50, 100, 200]" :disabled="loading"
        layout="total, sizes, prev, pager, next" class="pagination"
        @current-change="load" @size-change="changePageSize(artifactPage)"
      />
    </el-card>

    <el-card shadow="never" class="table-card">
      <template #header>{{ t('execution.evidence.qualityEvidence') }}</template>
      <el-table :data="data.quality_evidence" v-loading="loading">
        <el-table-column prop="run" :label="t('execution.evidence.run')" width="80" />
        <el-table-column prop="item" :label="t('execution.evidence.item')" width="80" />
        <el-table-column prop="evidence_type" :label="t('execution.evidence.evidenceType')" width="150" />
        <el-table-column prop="availability" :label="t('execution.evidence.availability')" width="130" />
        <el-table-column prop="source_uri" :label="t('execution.evidence.reference')" min-width="240" show-overflow-tooltip />
        <el-table-column prop="sha256" :label="t('execution.evidence.hash')" min-width="150" show-overflow-tooltip />
        <template #empty><el-empty :description="t('execution.evidence.empty')" /></template>
      </el-table>
      <el-pagination
        v-model:current-page="qualityPage.page" v-model:page-size="qualityPage.pageSize"
        :total="qualityPage.total" :page-sizes="[20, 50, 100, 200]" :disabled="loading"
        layout="total, sizes, prev, pager, next" class="pagination"
        @current-change="load" @size-change="changePageSize(qualityPage)"
      />
    </el-card>
  </div>
</template>

<script setup>
import { onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Refresh } from '@element-plus/icons-vue'
import { getExecutionEvidenceExplorer } from '@/api/executions'

const { t } = useI18n()
const route = useRoute()
const loading = ref(false)
const filters = reactive({ project: null, job: null, quality_run: null, case_key: '', media_type: '', evidence_type: '', availability: '' })
const appliedFilters = ref({})
const artifactPage = reactive({ page: 1, pageSize: 20, total: 0 })
const qualityPage = reactive({ page: 1, pageSize: 20, total: 0 })
const data = ref({ execution_artifacts: [], quality_evidence: [] })
let requestSequence = 0

const load = async () => {
  const sequence = ++requestSequence
  loading.value = true
  data.value = { execution_artifacts: [], quality_evidence: [] }
  try {
    const response = await getExecutionEvidenceExplorer({
      ...appliedFilters.value,
      artifact_page: artifactPage.page,
      artifact_page_size: artifactPage.pageSize,
      quality_page: qualityPage.page,
      quality_page_size: qualityPage.pageSize,
    })
    if (sequence !== requestSequence) return
    const payload = response.data || {}
    artifactPage.total = payload.pagination?.execution_artifacts?.total ?? payload.counts?.execution_artifacts ?? 0
    qualityPage.total = payload.pagination?.quality_evidence?.total ?? payload.counts?.quality_evidence ?? 0
    const lastArtifactPage = Math.max(1, Math.ceil(artifactPage.total / artifactPage.pageSize))
    const lastQualityPage = Math.max(1, Math.ceil(qualityPage.total / qualityPage.pageSize))
    if (artifactPage.page > lastArtifactPage || qualityPage.page > lastQualityPage) {
      artifactPage.page = Math.min(artifactPage.page, lastArtifactPage)
      qualityPage.page = Math.min(qualityPage.page, lastQualityPage)
      await load()
      return
    }
    data.value = {
      execution_artifacts: payload.execution_artifacts || [],
      quality_evidence: payload.quality_evidence || [],
    }
  } catch (error) {
    if (sequence === requestSequence) ElMessage.error(error.response?.data?.detail || t('execution.evidence.loadFailed'))
  } finally {
    if (sequence === requestSequence) loading.value = false
  }
}

const applyFilters = () => {
  appliedFilters.value = Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== null && value !== undefined && value !== ''))
  artifactPage.page = qualityPage.page = 1
  artifactPage.total = qualityPage.total = 0
  return load()
}

const changePageSize = pagination => {
  pagination.page = 1
  return load()
}

const routeId = value => {
  const number = Number(Array.isArray(value) ? value[0] : value)
  return Number.isSafeInteger(number) && number > 0 ? number : null
}
watch(() => [route.query.project, route.query.job, route.query.quality_run], ([project, job, qualityRun]) => {
  filters.project = routeId(project)
  filters.job = routeId(job)
  filters.quality_run = routeId(qualityRun)
  applyFilters()
}, { immediate: true })
onBeforeUnmount(() => { requestSequence += 1 })
</script>

<style scoped>
.evidence-page { padding: 24px; color: #101828; }
.page-header { display: flex; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.eyebrow { margin: 0 0 6px; color: #2f6bff; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.page-header h1 { margin: 0; font-size: 24px; }
.subtitle { margin: 7px 0 0; color: #667085; }
.filter-card, .table-card { margin-bottom: 16px; }
.summary-row { margin-bottom: 16px; }
.metric { display: flex; justify-content: space-between; color: #667085; }
.metric strong { color: #101828; font-size: 26px; }
.pagination { margin-top: 16px; justify-content: flex-end; flex-wrap: wrap; }
@media (max-width: 768px) { .page-header { flex-direction: column; } }
</style>
