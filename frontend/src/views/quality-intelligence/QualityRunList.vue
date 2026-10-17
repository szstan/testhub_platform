<template>
  <div class="quality-page run-list-page">
    <div class="quality-header">
      <div>
        <p class="eyebrow">{{ t('qualityIntelligence.runs.eyebrow') }}</p>
        <h1>{{ t('qualityIntelligence.runs.title') }}</h1>
        <p class="subtitle">{{ t('qualityIntelligence.runs.subtitle') }}</p>
      </div>
      <div class="header-actions">
        <el-button :icon="Refresh" :loading="loading" @click="loadRuns">{{ t('qualityIntelligence.actions.refresh') }}</el-button>
        <el-button type="primary" :icon="Upload" @click="ingestDialog = true">{{ t('qualityIntelligence.actions.ingest') }}</el-button>
      </div>
    </div>

    <el-card shadow="never" class="filter-panel">
      <el-form :inline="true" :model="filters" @submit.prevent>
        <el-form-item :label="t('qualityIntelligence.filters.source')">
          <el-select v-model="filters.source_type" clearable :placeholder="t('qualityIntelligence.filters.allSources')" @change="resetAndLoad">
            <el-option v-for="source in sourceOptions" :key="source" :label="source" :value="source" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.filters.status')">
          <el-select v-model="filters.quality_status" clearable :placeholder="t('qualityIntelligence.filters.allStatuses')" @change="resetAndLoad">
            <el-option v-for="status in statusOptions" :key="status" :label="statusLabel(status)" :value="status" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.filters.project')">
          <el-input v-model="filters.project_id" clearable :placeholder="t('qualityIntelligence.filters.projectPlaceholder')" @change="resetAndLoad" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-alert v-if="error" class="state-alert" type="error" :title="error" show-icon :closable="false" />
    <el-card shadow="never" class="table-card">
      <el-table v-loading="loading" :data="runs" row-key="id" @row-click="openRun">
        <el-table-column prop="id" label="#" width="78" />
        <el-table-column :label="t('qualityIntelligence.table.source')" width="110">
          <template #default="{ row }">
            <span class="source-chip" :class="`source-${String(row.source_type).toLowerCase()}`">{{ row.source_type }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.execution')" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ row.source_execution_id || '-' }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.project')" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.project_type }} / {{ row.project_id || '-' }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.status')" width="140">
          <template #default="{ row }">
            <el-tag :type="statusType(row.quality_status)" size="small">{{ statusLabel(row.quality_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.sourceStatus')" width="130">
          <template #default="{ row }">{{ row.source_status || '-' }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.createdAt')" width="170">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.actions')" width="100" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" @click.stop="openRun(row)">{{ t('qualityIntelligence.actions.view') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="t('qualityIntelligence.states.noRuns')" />
        </template>
      </el-table>
      <div v-if="total" class="pagination-row">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          layout="total, sizes, prev, pager, next"
          :total="total"
          :page-sizes="[20, 50, 100]"
          @current-change="loadRuns"
          @size-change="resetAndLoad"
        />
      </div>
    </el-card>

    <el-dialog v-model="ingestDialog" :title="t('qualityIntelligence.ingest.title')" width="440px" destroy-on-close>
      <el-form ref="ingestFormRef" :model="ingestForm" label-position="top" @submit.prevent>
        <el-form-item :label="t('qualityIntelligence.ingest.source')" required>
          <el-select v-model="ingestForm.source_type" class="full-width">
            <el-option v-for="source in sourceOptions" :key="source" :label="source" :value="source" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.ingest.executionId')" required>
          <el-input v-model="ingestForm.source_execution_id" :placeholder="t('qualityIntelligence.ingest.executionPlaceholder')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ingestDialog = false">{{ t('qualityIntelligence.actions.cancel') }}</el-button>
        <el-button type="primary" :loading="ingesting" @click="submitIngest">{{ t('qualityIntelligence.actions.ingest') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { Refresh, Upload } from '@element-plus/icons-vue'
import { getQualityRuns, ingestQuality } from '@/api/quality-intelligence'

const { t } = useI18n()
const router = useRouter()
const loading = ref(false)
const ingesting = ref(false)
const error = ref('')
const runs = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const ingestDialog = ref(false)
const filters = reactive({ source_type: '', quality_status: '', project_id: '' })
const ingestForm = reactive({ source_type: 'API', source_execution_id: '' })

const sourceOptions = ['API', 'UI', 'APP', 'PERF']
const statusOptions = ['INGESTED', 'PARTIAL', 'FAILED', 'SOURCE_MISSING']
const rowsFrom = (payload) => payload?.results || payload?.items || (Array.isArray(payload) ? payload : [])
const statusLabel = status => t(`qualityIntelligence.status.${status}`, status)
const statusType = status => ({ INGESTED: 'success', PARTIAL: 'warning', FAILED: 'danger', SOURCE_MISSING: 'info' }[status] || 'info')
const formatDate = value => value ? new Date(value).toLocaleString() : '-'

const loadRuns = async () => {
  loading.value = true
  error.value = ''
  try {
    const response = await getQualityRuns({ page: page.value, page_size: pageSize.value, ...filters })
    runs.value = rowsFrom(response.data)
    total.value = Number(response.data?.count ?? runs.value.length)
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || t('qualityIntelligence.errors.loadRuns')
  } finally {
    loading.value = false
  }
}

const resetAndLoad = () => {
  page.value = 1
  loadRuns()
}

const openRun = row => router.push(`/quality-intelligence/runs/${row.id}`)

const submitIngest = async () => {
  if (!ingestForm.source_execution_id.trim()) {
    ElMessage.warning(t('qualityIntelligence.ingest.executionRequired'))
    return
  }
  ingesting.value = true
  try {
    const response = await ingestQuality({ ...ingestForm, source_execution_id: ingestForm.source_execution_id.trim() })
    ElMessage.success(response.data?.ingestion?.reused ? t('qualityIntelligence.ingest.reused') : t('qualityIntelligence.ingest.created'))
    ingestDialog.value = false
    ingestForm.source_execution_id = ''
    await loadRuns()
  } catch (requestError) {
    const code = requestError?.response?.data?.error_code
    ElMessage.error(code === 'SOURCE_RUNNING' ? t('qualityIntelligence.errors.sourceRunning') : requestError?.response?.data?.detail || t('qualityIntelligence.errors.ingest'))
  } finally {
    ingesting.value = false
  }
}

onMounted(loadRuns)
</script>

<style scoped>
.quality-page { color: #101828; }
.quality-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; margin-bottom: 18px; }
.quality-header h1 { margin: 0; font-size: 24px; font-weight: 650; }
.eyebrow { margin: 0 0 6px; color: #2f6bff; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.subtitle { margin: 7px 0 0; color: #667085; }
.header-actions { display: flex; gap: 10px; }
.filter-panel, .table-card { border-color: #eaecf0; border-radius: 8px; }
.filter-panel { margin-bottom: 16px; }
.filter-panel :deep(.el-form-item) { margin-bottom: 0; }
.table-card :deep(.el-table__row) { cursor: pointer; }
.source-chip { display: inline-flex; padding: 3px 8px; border-radius: 4px; color: #fff; font-size: 11px; font-weight: 700; }
.source-api { background: #2f6bff; } .source-ui { background: #12b3a8; } .source-app { background: #7a5af8; } .source-perf { background: #f79009; }
.pagination-row { display: flex; justify-content: flex-end; padding-top: 18px; }
.full-width { width: 100%; }
.state-alert { margin-bottom: 16px; }
@media (max-width: 768px) {
  .quality-header { flex-direction: column; }
  .header-actions { flex-wrap: wrap; }
  .filter-panel :deep(.el-form) { display: grid; gap: 10px; }
  .filter-panel :deep(.el-form-item) { margin-right: 0; }
  .filter-panel :deep(.el-select), .filter-panel :deep(.el-input) { width: 100%; }
  .pagination-row { justify-content: center; overflow-x: auto; }
}
</style>
