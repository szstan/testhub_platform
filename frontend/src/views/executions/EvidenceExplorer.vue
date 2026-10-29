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
      <el-form :inline="true" @submit.prevent="load">
        <el-form-item :label="t('execution.evidence.project')"><el-input-number v-model="filters.project" :min="1" :controls="false" /></el-form-item>
        <el-form-item :label="t('execution.evidence.job')"><el-input-number v-model="filters.job" :min="1" :controls="false" /></el-form-item>
        <el-form-item :label="t('execution.evidence.caseKey')"><el-input v-model="filters.case_key" clearable /></el-form-item>
        <el-form-item :label="t('execution.evidence.type')"><el-input v-model="filters.evidence_type" clearable /></el-form-item>
        <el-form-item :label="t('execution.evidence.availability')"><el-input v-model="filters.availability" clearable /></el-form-item>
        <el-button type="primary" @click="load">{{ t('common.search') }}</el-button>
      </el-form>
    </el-card>

    <el-row :gutter="16" class="summary-row">
      <el-col :xs="12" :md="6"><el-card shadow="never"><div class="metric"><span>{{ t('execution.evidence.executionArtifacts') }}</span><strong>{{ data.counts.execution_artifacts }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="6"><el-card shadow="never"><div class="metric"><span>{{ t('execution.evidence.qualityEvidence') }}</span><strong>{{ data.counts.quality_evidence }}</strong></div></el-card></el-col>
    </el-row>

    <el-card shadow="never" class="table-card">
      <template #header>{{ t('execution.evidence.executionArtifacts') }}</template>
      <el-table :data="data.execution_artifacts" v-loading="loading">
        <el-table-column prop="job" :label="t('execution.evidence.job')" width="80" />
        <el-table-column prop="attempt" :label="t('execution.evidence.attempt')" width="90" />
        <el-table-column prop="step_result" :label="t('execution.evidence.step')" width="100" />
        <el-table-column prop="media_type" :label="t('execution.evidence.type')" width="160" />
        <el-table-column prop="uri" :label="t('execution.evidence.reference')" min-width="260" show-overflow-tooltip />
        <el-table-column prop="content_hash" :label="t('execution.evidence.hash')" min-width="150" show-overflow-tooltip />
        <el-table-column prop="retention_policy" :label="t('execution.evidence.retention')" width="110" />
        <template #empty><el-empty :description="t('execution.evidence.empty')" /></template>
      </el-table>
    </el-card>

    <el-card shadow="never" class="table-card">
      <template #header>{{ t('execution.evidence.qualityEvidence') }}</template>
      <el-table :data="data.quality_evidence">
        <el-table-column prop="run" :label="t('execution.evidence.run')" width="80" />
        <el-table-column prop="item" :label="t('execution.evidence.item')" width="80" />
        <el-table-column prop="evidence_type" :label="t('execution.evidence.type')" width="150" />
        <el-table-column prop="availability" :label="t('execution.evidence.availability')" width="130" />
        <el-table-column prop="source_uri" :label="t('execution.evidence.reference')" min-width="240" show-overflow-tooltip />
        <el-table-column prop="sha256" :label="t('execution.evidence.hash')" min-width="150" show-overflow-tooltip />
        <template #empty><el-empty :description="t('execution.evidence.empty')" /></template>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Refresh } from '@element-plus/icons-vue'
import { getExecutionEvidenceExplorer } from '@/api/executions'

const { t } = useI18n()
const route = useRoute()
const loading = ref(false)
const filters = reactive({ project: null, job: null, case_key: '', evidence_type: '', availability: '', quality_run: '' })
const data = ref({ execution_artifacts: [], quality_evidence: [], counts: { execution_artifacts: 0, quality_evidence: 0 } })

const load = async () => {
  loading.value = true
  try {
    const response = await getExecutionEvidenceExplorer({
      ...filters,
      project: filters.project || undefined,
      job: filters.job || undefined,
      case_key: filters.case_key || undefined,
      evidence_type: filters.evidence_type || undefined,
      availability: filters.availability || undefined,
      quality_run: route.query.quality_run || filters.quality_run || undefined,
      limit: 100,
    })
    data.value = response.data || data.value
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || t('execution.evidence.loadFailed'))
  } finally {
    loading.value = false
  }
}

onMounted(load)
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
@media (max-width: 768px) { .page-header { flex-direction: column; } }
</style>
