<template>
  <div class="quality-page run-detail-page">
    <div class="quality-header">
      <div class="title-row">
        <el-button text :icon="ArrowLeft" @click="router.push('/quality-intelligence/runs')" />
        <div>
          <p class="eyebrow">{{ t('qualityIntelligence.runDetail.eyebrow') }}</p>
          <h1>{{ t('qualityIntelligence.runDetail.title') }} #{{ run?.id || route.params.id }}</h1>
          <p class="subtitle">{{ run?.source_type || '-' }} · {{ run?.source_execution_id || '-' }}</p>
        </div>
      </div>
      <div class="header-actions">
        <el-tag v-if="run" :type="statusType(run.quality_status)" size="large">{{ statusLabel(run.quality_status) }}</el-tag>
        <el-button :icon="Refresh" :loading="loading" @click="loadDetail">{{ t('qualityIntelligence.actions.refresh') }}</el-button>
        <el-button type="primary" :icon="RefreshRight" :loading="recomputing" @click="recompute">{{ t('qualityIntelligence.actions.recompute') }}</el-button>
      </div>
    </div>

    <el-alert v-if="error" class="state-alert" type="error" :title="error" show-icon :closable="false" />
    <el-alert v-if="run?.quality_status === 'PARTIAL'" class="state-alert" type="warning" :title="t('qualityIntelligence.runDetail.partialNotice')" show-icon :closable="false" />

    <el-skeleton v-if="loading && !run" :rows="8" animated />
    <template v-else-if="run">
      <el-row :gutter="16" class="detail-grid">
        <el-col :xs="24" :lg="15">
          <el-card shadow="never" class="section-card overview-card">
            <template #header>{{ t('qualityIntelligence.runDetail.overview') }}</template>
            <div class="metadata-grid">
              <div><span>{{ t('qualityIntelligence.table.source') }}</span><strong>{{ run.source_type }} / {{ run.source_scope }}</strong></div>
              <div><span>{{ t('qualityIntelligence.table.project') }}</span><strong>{{ run.project_type }} / {{ run.project_id || '-' }}</strong></div>
              <div><span>{{ t('qualityIntelligence.runDetail.sourceAsset') }}</span><strong>{{ run.source_asset_name || run.source_asset_id || '-' }}</strong></div>
              <div><span>{{ t('qualityIntelligence.runDetail.environment') }}</span><strong>{{ run.environment_fingerprint || '-' }}</strong></div>
              <div><span>{{ t('qualityIntelligence.runDetail.startedAt') }}</span><strong>{{ formatDate(run.started_at) }}</strong></div>
              <div><span>{{ t('qualityIntelligence.runDetail.finishedAt') }}</span><strong>{{ formatDate(run.finished_at) }}</strong></div>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="9">
          <el-card shadow="never" class="section-card provenance-card">
            <template #header>{{ t('qualityIntelligence.runDetail.provenance') }}</template>
            <div class="provenance-list">
              <div><span>{{ t('qualityIntelligence.runDetail.sourceModel') }}</span><strong>{{ run.provenance?.source_model || '-' }}</strong></div>
              <div><span>{{ t('qualityIntelligence.runDetail.adapterVersion') }}</span><strong>{{ run.adapter_version || '-' }}</strong></div>
              <div><span>{{ t('qualityIntelligence.runDetail.redactionVersion') }}</span><strong>{{ run.redaction_version || '-' }}</strong></div>
              <div><span>{{ t('qualityIntelligence.runDetail.classification') }}</span><el-tag size="small" effect="plain">{{ t('qualityIntelligence.labels.deterministic') }}</el-tag></div>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-card shadow="never" class="section-card table-card">
        <template #header><div class="section-heading"><span>{{ t('qualityIntelligence.runDetail.items') }}</span><el-tag size="small" effect="plain">{{ items.length }}</el-tag></div></template>
        <el-table v-loading="loadingChildren" :data="items" row-key="id">
          <el-table-column prop="test_key" :label="t('qualityIntelligence.table.testKey')" min-width="180" show-overflow-tooltip />
          <el-table-column prop="name" :label="t('qualityIntelligence.table.name')" min-width="160" show-overflow-tooltip />
          <el-table-column :label="t('qualityIntelligence.table.outcome')" width="130">
            <template #default="{ row }"><el-tag size="small" :type="outcomeType(row.outcome)">{{ outcomeLabel(row.outcome) }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="attempt" :label="t('qualityIntelligence.table.attempt')" width="90" />
          <el-table-column :label="t('qualityIntelligence.table.duration')" width="120">
            <template #default="{ row }">{{ row.duration_ms == null ? '-' : `${row.duration_ms} ms` }}</template>
          </el-table-column>
          <template #empty><el-empty :description="t('qualityIntelligence.states.noItems')" /></template>
        </el-table>
      </el-card>

      <el-row :gutter="16" class="detail-grid">
        <el-col :xs="24" :lg="12">
          <el-card shadow="never" class="section-card">
            <template #header>{{ t('qualityIntelligence.runDetail.comparisons') }}</template>
            <el-table :data="comparisons" size="small">
              <el-table-column prop="metric_key" :label="t('qualityIntelligence.table.metric')" min-width="150" />
              <el-table-column prop="actual_value" :label="t('qualityIntelligence.table.actual')" width="100" />
              <el-table-column prop="baseline_value" :label="t('qualityIntelligence.table.baseline')" width="100" />
              <el-table-column :label="t('qualityIntelligence.table.verdict')" width="150">
                <template #default="{ row }"><el-tag size="small" :type="verdictType(row.verdict)">{{ verdictLabel(row.verdict) }}</el-tag></template>
              </el-table-column>
              <template #empty><el-empty :description="t('qualityIntelligence.states.noComparisons')" /></template>
            </el-table>
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="12">
          <el-card shadow="never" class="section-card">
            <template #header>{{ t('qualityIntelligence.runDetail.evidence') }}</template>
            <div v-if="evidence.length" class="evidence-list">
              <div v-for="entry in evidence" :key="entry.id" class="evidence-row">
                <div class="evidence-meta"><el-tag size="small" effect="plain">{{ entry.evidence_type }}</el-tag><span>{{ entry.availability }}</span></div>
                <p>{{ entry.snippet || entry.source_field || t('qualityIntelligence.runDetail.noSnippet') }}</p>
              </div>
            </div>
            <el-empty v-else :description="t('qualityIntelligence.states.noEvidence')" />
          </el-card>
        </el-col>
      </el-row>

      <el-card shadow="never" class="section-card failure-card">
        <template #header>{{ t('qualityIntelligence.runDetail.failures') }}</template>
        <el-table :data="failures" size="small">
          <el-table-column prop="taxonomy_code" :label="t('qualityIntelligence.table.taxonomy')" width="150" />
          <el-table-column prop="subtype" :label="t('qualityIntelligence.table.subtype')" width="150" />
          <el-table-column prop="signature" :label="t('qualityIntelligence.table.signature')" min-width="180" show-overflow-tooltip />
          <el-table-column prop="classifier" :label="t('qualityIntelligence.table.classifier')" width="120" />
          <template #empty><el-empty :description="t('qualityIntelligence.states.noFailures')" /></template>
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { ArrowLeft, Refresh, RefreshRight } from '@element-plus/icons-vue'
import {
  getQualityComparisons,
  getQualityFailures,
  getQualityRun,
  getQualityRunEvidence,
  getQualityRunItems,
  recomputeQualityRun
} from '@/api/quality-intelligence'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const run = ref(null)
const items = ref([])
const evidence = ref([])
const comparisons = ref([])
const failures = ref([])
const loading = ref(false)
const loadingChildren = ref(false)
const recomputing = ref(false)
const error = ref('')
const rowsFrom = payload => payload?.results || payload?.items || (Array.isArray(payload) ? payload : [])
const statusLabel = status => t(`qualityIntelligence.status.${status}`, status)
const statusType = status => ({ INGESTED: 'success', PARTIAL: 'warning', FAILED: 'danger', SOURCE_MISSING: 'info' }[status] || 'info')
const outcomeLabel = outcome => t(`qualityIntelligence.outcome.${outcome}`, outcome)
const outcomeType = outcome => ({ PASS: 'success', FAIL: 'danger', ERROR: 'danger', SKIP: 'info', INCONCLUSIVE: 'warning', NOT_RUN: 'info' }[outcome] || 'info')
const verdictLabel = verdict => t(`qualityIntelligence.verdict.${verdict}`, verdict)
const verdictType = verdict => ({ WITHIN_TOLERANCE: 'success', REGRESSED: 'danger', NO_BASELINE: 'info', NOT_COMPARABLE: 'warning', ERROR: 'danger' }[verdict] || 'info')
const formatDate = value => value ? new Date(value).toLocaleString() : '-'

const loadDetail = async () => {
  loading.value = true
  loadingChildren.value = true
  error.value = ''
  try {
    const id = route.params.id
    const [runResponse, itemResponse, evidenceResponse, comparisonResponse, failureResponse] = await Promise.all([
      getQualityRun(id),
      getQualityRunItems(id, { page_size: 200 }),
      getQualityRunEvidence(id, { page_size: 200 }),
      getQualityComparisons({ run_id: id, page_size: 200 }),
      getQualityFailures({ run_id: id, page_size: 200 })
    ])
    run.value = runResponse.data
    items.value = rowsFrom(itemResponse.data)
    evidence.value = rowsFrom(evidenceResponse.data)
    comparisons.value = rowsFrom(comparisonResponse.data)
    failures.value = rowsFrom(failureResponse.data)
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || t('qualityIntelligence.errors.loadRun')
  } finally {
    loading.value = false
    loadingChildren.value = false
  }
}

const recompute = async () => {
  recomputing.value = true
  try {
    const response = await recomputeQualityRun(route.params.id)
    comparisons.value = rowsFrom({ results: response.data?.comparisons })
    ElMessage.success(t('qualityIntelligence.messages.recomputed'))
  } catch (requestError) {
    ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.recompute'))
  } finally {
    recomputing.value = false
  }
}

onMounted(loadDetail)
</script>

<style scoped>
.quality-page { color: #101828; }
.quality-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.title-row { display: flex; align-items: flex-start; gap: 8px; }
.title-row .el-button { margin-top: 4px; }
.quality-header h1 { margin: 0; font-size: 24px; font-weight: 650; }
.eyebrow { margin: 0 0 6px; color: #2f6bff; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.subtitle { margin: 7px 0 0; color: #667085; }
.header-actions { display: flex; align-items: center; gap: 10px; }
.section-card { height: 100%; margin-bottom: 16px; border-color: #eaecf0; border-radius: 8px; }
.state-alert { margin-bottom: 16px; }
.detail-grid { margin-bottom: 0; }
.metadata-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 26px; }
.metadata-grid div, .provenance-list div { display: grid; gap: 5px; min-width: 0; }
.metadata-grid span, .provenance-list span { color: #667085; font-size: 12px; }
.metadata-grid strong, .provenance-list strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.provenance-list { display: grid; gap: 16px; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.table-card :deep(.el-table__row) { cursor: default; }
.source-chip { padding: 3px 8px; border-radius: 4px; color: #fff; font-size: 11px; font-weight: 700; }
.evidence-list { display: grid; gap: 12px; max-height: 270px; overflow-y: auto; }
.evidence-row { padding: 10px 0; border-bottom: 1px solid #f2f4f7; }
.evidence-row:last-child { border-bottom: 0; }
.evidence-meta { display: flex; align-items: center; gap: 8px; color: #667085; font-size: 12px; }
.evidence-row p { margin: 8px 0 0; color: #475467; line-height: 1.45; overflow-wrap: anywhere; }
.failure-card { margin-bottom: 0; }
@media (max-width: 768px) {
  .quality-header { flex-direction: column; }
  .header-actions { flex-wrap: wrap; }
  .metadata-grid { grid-template-columns: 1fr; }
}
</style>
