<template>
  <div class="quality-page baseline-page">
    <div class="quality-header">
      <div>
        <p class="eyebrow">{{ t('qualityIntelligence.baselines.eyebrow') }}</p>
        <h1>{{ t('qualityIntelligence.baselines.title') }}</h1>
        <p class="subtitle">{{ t('qualityIntelligence.baselines.subtitle') }}</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="baselineDialog = true">{{ t('qualityIntelligence.actions.createBaseline') }}</el-button>
    </div>

    <el-alert v-if="error" class="state-alert" type="error" :title="error" show-icon :closable="false" />
    <el-row :gutter="16">
      <el-col :xs="24" :lg="14">
        <el-card shadow="never" class="table-card">
          <template #header><div class="section-heading"><span>{{ t('qualityIntelligence.baselines.savedTitle') }}</span><el-button text :icon="Refresh" :loading="loading" @click="loadData">{{ t('qualityIntelligence.actions.refresh') }}</el-button></div></template>
          <el-table v-loading="loading" :data="baselines" row-key="id">
            <el-table-column prop="metric_key" :label="t('qualityIntelligence.table.metric')" min-width="150" />
            <el-table-column :label="t('qualityIntelligence.table.scope')" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.source_type }} · {{ row.project_type }} / {{ row.project_id }}</template>
            </el-table-column>
            <el-table-column :label="t('qualityIntelligence.table.value')" width="120">
              <template #default="{ row }">{{ row.value }} {{ row.unit }}</template>
            </el-table-column>
            <el-table-column :label="t('qualityIntelligence.table.tolerance')" width="110"><template #default="{ row }">{{ row.tolerance }}%</template></el-table-column>
            <el-table-column :label="t('qualityIntelligence.table.active')" width="100"><template #default="{ row }"><el-tag size="small" :type="row.active ? 'success' : 'info'">{{ row.active ? t('qualityIntelligence.labels.active') : t('qualityIntelligence.labels.inactive') }}</el-tag></template></el-table-column>
            <template #empty><el-empty :description="t('qualityIntelligence.states.noBaselines')" /></template>
          </el-table>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="10">
        <el-card shadow="never" class="table-card">
          <template #header>{{ t('qualityIntelligence.baselines.comparisonsTitle') }}</template>
          <el-table v-loading="comparisonLoading" :data="comparisons" size="small" row-key="id">
            <el-table-column prop="metric_key" :label="t('qualityIntelligence.table.metric')" min-width="130" />
            <el-table-column prop="actual_value" :label="t('qualityIntelligence.table.actual')" width="90" />
            <el-table-column :label="t('qualityIntelligence.table.verdict')" width="145"><template #default="{ row }"><el-tag size="small" :type="verdictType(row.verdict)">{{ verdictLabel(row.verdict) }}</el-tag></template></el-table-column>
            <template #empty><el-empty :description="t('qualityIntelligence.states.noComparisons')" /></template>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="baselineDialog" :title="t('qualityIntelligence.baselines.createTitle')" width="620px" destroy-on-close>
      <el-form ref="baselineFormRef" :model="form" label-position="top">
        <el-row :gutter="12">
          <el-col :span="12"><el-form-item :label="t('qualityIntelligence.form.source')" required><el-select v-model="form.source_type" class="full-width"><el-option v-for="source in sourceOptions" :key="source" :label="source" :value="source" /></el-select></el-form-item></el-col>
          <el-col :span="12"><el-form-item :label="t('qualityIntelligence.form.profile')"><el-select v-model="form.profile_id" clearable class="full-width" :placeholder="t('qualityIntelligence.form.projectBaseline')"><el-option v-for="profile in profiles" :key="profile.id" :label="`${profile.name || profile.test_key} (#${profile.id})`" :value="profile.id" /></el-select></el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12"><el-form-item :label="t('qualityIntelligence.form.projectType')" required><el-input v-model="form.project_type" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item :label="t('qualityIntelligence.form.projectId')" required><el-input v-model="form.project_id" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12"><el-form-item :label="t('qualityIntelligence.form.metric')" required><el-input v-model="form.metric_key" placeholder="response_time_ms" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item :label="t('qualityIntelligence.form.environment')"><el-input v-model="form.environment_fingerprint" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item :label="t('qualityIntelligence.form.value')" required><el-input-number v-model="form.value" :min="0" class="full-width" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item :label="t('qualityIntelligence.form.unit')"><el-input v-model="form.unit" placeholder="ms" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item :label="t('qualityIntelligence.form.tolerance')"><el-input-number v-model="form.tolerance" :min="0" :max="100" class="full-width" /></el-form-item></el-col>
        </el-row>
        <el-form-item :label="t('qualityIntelligence.form.direction')"><el-radio-group v-model="form.direction"><el-radio value="LOWER_IS_BETTER">{{ t('qualityIntelligence.form.lowerBetter') }}</el-radio><el-radio value="HIGHER_IS_BETTER">{{ t('qualityIntelligence.form.higherBetter') }}</el-radio><el-radio value="EQUAL">{{ t('qualityIntelligence.form.equal') }}</el-radio></el-radio-group></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="baselineDialog = false">{{ t('qualityIntelligence.actions.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="saveBaseline">{{ t('qualityIntelligence.actions.create') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { createQualityBaseline, getQualityBaselines, getQualityComparisons, getQualityProfiles } from '@/api/quality-intelligence'

const { t } = useI18n()
const loading = ref(false)
const comparisonLoading = ref(false)
const saving = ref(false)
const error = ref('')
const baselines = ref([])
const comparisons = ref([])
const profiles = ref([])
const baselineDialog = ref(false)
const sourceOptions = ['API', 'UI', 'APP', 'PERF']
const form = reactive({ source_type: 'API', project_type: 'API', project_id: '', profile_id: null, metric_key: '', environment_fingerprint: 'unknown', value: 0, unit: '', tolerance: 0, direction: 'LOWER_IS_BETTER' })
const rowsFrom = payload => payload?.results || payload?.items || (Array.isArray(payload) ? payload : [])
const verdictLabel = verdict => t(`qualityIntelligence.verdict.${verdict}`, verdict)
const verdictType = verdict => ({ WITHIN_TOLERANCE: 'success', REGRESSED: 'danger', NO_BASELINE: 'info', NOT_COMPARABLE: 'warning', ERROR: 'danger' }[verdict] || 'info')

const loadData = async () => {
  loading.value = true
  comparisonLoading.value = true
  error.value = ''
  try {
    const [baselineResponse, profileResponse, comparisonResponse] = await Promise.all([
      getQualityBaselines({ page_size: 200 }),
      getQualityProfiles({ page_size: 200 }),
      getQualityComparisons({ page_size: 200 })
    ])
    baselines.value = rowsFrom(baselineResponse.data)
    profiles.value = rowsFrom(profileResponse.data)
    comparisons.value = rowsFrom(comparisonResponse.data)
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || t('qualityIntelligence.errors.loadBaselines')
  } finally {
    loading.value = false
    comparisonLoading.value = false
  }
}

const saveBaseline = async () => {
  if (!form.project_id || !form.metric_key) {
    ElMessage.warning(t('qualityIntelligence.baselines.required'))
    return
  }
  saving.value = true
  try {
    await createQualityBaseline({ ...form })
    baselineDialog.value = false
    ElMessage.success(t('qualityIntelligence.messages.baselineCreated'))
    await loadData()
  } catch (requestError) {
    ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.baseline'))
  } finally {
    saving.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.quality-page { color: #101828; }
.quality-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.quality-header h1 { margin: 0; font-size: 24px; font-weight: 650; }
.eyebrow { margin: 0 0 6px; color: #2f6bff; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.subtitle { margin: 7px 0 0; color: #667085; }
.table-card { margin-bottom: 16px; border-color: #eaecf0; border-radius: 8px; }
.section-heading { display: flex; align-items: center; justify-content: space-between; }
.state-alert { margin-bottom: 16px; }
.full-width { width: 100%; }
@media (max-width: 768px) { .quality-header { flex-direction: column; } }
</style>
