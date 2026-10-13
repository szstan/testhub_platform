<template>
  <div class="quality-page failure-page">
    <div class="quality-header">
      <div>
        <p class="eyebrow">{{ t('qualityIntelligence.failures.eyebrow') }}</p>
        <h1>{{ t('qualityIntelligence.failures.title') }}</h1>
        <p class="subtitle">{{ t('qualityIntelligence.failures.subtitle') }}</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadFailures">{{ t('qualityIntelligence.actions.refresh') }}</el-button>
    </div>

    <el-card shadow="never" class="filter-panel">
      <el-form :inline="true" :model="filters" @submit.prevent>
        <el-form-item :label="t('qualityIntelligence.filters.source')">
          <el-select v-model="filters.source_type" clearable :placeholder="t('qualityIntelligence.filters.allSources')" @change="resetAndLoad">
            <el-option v-for="source in sourceOptions" :key="source" :label="source" :value="source" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.filters.taxonomy')">
          <el-select v-model="filters.taxonomy_code" clearable :placeholder="t('qualityIntelligence.filters.allTaxonomies')" @change="resetAndLoad">
            <el-option v-for="taxonomy in taxonomyOptions" :key="taxonomy" :label="taxonomyLabel(taxonomy)" :value="taxonomy" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.filters.search')">
          <el-input v-model="filters.search" clearable :placeholder="t('qualityIntelligence.filters.searchPlaceholder')" @keyup.enter="resetAndLoad" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-alert v-if="error" class="state-alert" type="error" :title="error" show-icon :closable="false" />
    <el-card shadow="never" class="table-card">
      <el-table v-loading="loading" :data="failures" row-key="id">
        <el-table-column :label="t('qualityIntelligence.table.taxonomy')" width="160">
          <template #default="{ row }"><el-tag size="small" :type="taxonomyType(row.taxonomy_code)">{{ taxonomyLabel(row.taxonomy_code) }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="subtype" :label="t('qualityIntelligence.table.subtype')" width="150" show-overflow-tooltip />
        <el-table-column :label="t('qualityIntelligence.table.message')" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ row.message || t('qualityIntelligence.labels.noMessage') }}</template>
        </el-table-column>
        <el-table-column prop="classifier" :label="t('qualityIntelligence.table.classifier')" width="110" />
        <el-table-column :label="t('qualityIntelligence.table.confidence')" width="110">
          <template #default="{ row }">{{ formatConfidence(row.confidence) }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.reviewed')" width="110">
          <template #default="{ row }"><el-tag size="small" :type="row.reviewed_at ? 'success' : 'info'">{{ row.reviewed_at ? t('qualityIntelligence.labels.reviewed') : t('qualityIntelligence.labels.unreviewed') }}</el-tag></template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.actions')" width="110" fixed="right">
          <template #default="{ row }"><el-button text type="primary" @click="openReview(row)">{{ t('qualityIntelligence.actions.review') }}</el-button></template>
        </el-table-column>
        <template #empty><el-empty :description="t('qualityIntelligence.states.noFailures')" /></template>
      </el-table>
      <div v-if="total" class="pagination-row">
        <el-pagination v-model:current-page="page" v-model:page-size="pageSize" layout="total, sizes, prev, pager, next" :total="total" :page-sizes="[20, 50, 100]" @current-change="loadFailures" @size-change="resetAndLoad" />
      </div>
    </el-card>

    <el-dialog v-model="reviewDialog" :title="t('qualityIntelligence.review.title')" width="480px" destroy-on-close>
      <el-form :model="reviewForm" label-position="top">
        <el-form-item :label="t('qualityIntelligence.review.taxonomy')" required>
          <el-select v-model="reviewForm.taxonomy_code" class="full-width">
            <el-option v-for="taxonomy in taxonomyOptions" :key="taxonomy" :label="taxonomyLabel(taxonomy)" :value="taxonomy" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.review.subtype')">
          <el-input v-model="reviewForm.subtype" maxlength="64" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.review.note')">
          <el-input v-model="reviewForm.review_note" type="textarea" :rows="4" maxlength="4096" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reviewDialog = false">{{ t('qualityIntelligence.actions.cancel') }}</el-button>
        <el-button type="primary" :loading="reviewing" @click="submitReview">{{ t('qualityIntelligence.actions.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { Refresh } from '@element-plus/icons-vue'
import { getQualityFailures, reviewQualityFailure } from '@/api/quality-intelligence'

const { t } = useI18n()
const loading = ref(false)
const reviewing = ref(false)
const error = ref('')
const failures = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const reviewDialog = ref(false)
const selectedFailure = ref(null)
const filters = reactive({ source_type: '', taxonomy_code: '', search: '' })
const reviewForm = reactive({ taxonomy_code: 'UNKNOWN', subtype: '', review_note: '' })

const sourceOptions = ['API', 'UI', 'APP', 'PERF']
const taxonomyOptions = ['PRODUCT', 'TEST', 'ENVIRONMENT', 'INFRASTRUCTURE', 'DATA', 'PERFORMANCE', 'TIMEOUT', 'UNKNOWN']
const rowsFrom = payload => payload?.results || payload?.items || (Array.isArray(payload) ? payload : [])
const taxonomyLabel = taxonomy => t(`qualityIntelligence.taxonomy.${taxonomy}`, taxonomy)
const taxonomyType = taxonomy => ({ PRODUCT: 'danger', PERFORMANCE: 'warning', TIMEOUT: 'warning', TEST: 'info', ENVIRONMENT: 'info', INFRASTRUCTURE: 'danger', DATA: 'warning', UNKNOWN: 'info' }[taxonomy] || 'info')
const formatConfidence = value => value == null ? '-' : `${Math.round(Number(value) * 100)}%`

const loadFailures = async () => {
  loading.value = true
  error.value = ''
  try {
    const response = await getQualityFailures({ page: page.value, page_size: pageSize.value, ...filters })
    failures.value = rowsFrom(response.data)
    total.value = Number(response.data?.count ?? failures.value.length)
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || t('qualityIntelligence.errors.loadFailures')
  } finally {
    loading.value = false
  }
}

const resetAndLoad = () => {
  page.value = 1
  loadFailures()
}

const openReview = row => {
  selectedFailure.value = row
  reviewForm.taxonomy_code = row.taxonomy_code || 'UNKNOWN'
  reviewForm.subtype = row.subtype || ''
  reviewForm.review_note = row.review_note || ''
  reviewDialog.value = true
}

const submitReview = async () => {
  if (!selectedFailure.value) return
  reviewing.value = true
  try {
    const response = await reviewQualityFailure(selectedFailure.value.id, { ...reviewForm })
    const index = failures.value.findIndex(item => item.id === selectedFailure.value.id)
    if (index >= 0) failures.value[index] = response.data
    reviewDialog.value = false
    ElMessage.success(t('qualityIntelligence.messages.reviewSaved'))
  } catch (requestError) {
    ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.review'))
  } finally {
    reviewing.value = false
  }
}

onMounted(loadFailures)
</script>

<style scoped>
.quality-page { color: #101828; }
.quality-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.quality-header h1 { margin: 0; font-size: 24px; font-weight: 650; }
.eyebrow { margin: 0 0 6px; color: #2f6bff; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.subtitle { margin: 7px 0 0; color: #667085; }
.filter-panel, .table-card { border-color: #eaecf0; border-radius: 8px; }
.filter-panel { margin-bottom: 16px; }
.filter-panel :deep(.el-form-item) { margin-bottom: 0; }
.state-alert { margin-bottom: 16px; }
.pagination-row { display: flex; justify-content: flex-end; padding-top: 18px; }
.full-width { width: 100%; }
@media (max-width: 768px) {
  .quality-header { flex-direction: column; }
  .filter-panel :deep(.el-form) { display: grid; gap: 10px; }
  .filter-panel :deep(.el-form-item) { margin-right: 0; }
  .filter-panel :deep(.el-select), .filter-panel :deep(.el-input) { width: 100%; }
  .pagination-row { justify-content: center; overflow-x: auto; }
}
</style>
