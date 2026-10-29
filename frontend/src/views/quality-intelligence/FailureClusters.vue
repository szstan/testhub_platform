<template>
  <div class="cluster-page">
    <div class="page-header">
      <div>
        <p class="eyebrow">{{ t('qualityIntelligence.clusters.eyebrow') }}</p>
        <h1>{{ t('qualityIntelligence.clusters.title') }}</h1>
        <p class="subtitle">{{ t('qualityIntelligence.clusters.subtitle') }}</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="load">{{ t('qualityIntelligence.actions.refresh') }}</el-button>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent="resetAndLoad">
        <el-form-item :label="t('qualityIntelligence.table.source')">
          <el-select v-model="filters.source_type" clearable :placeholder="t('qualityIntelligence.filters.allSources')">
            <el-option v-for="source in sourceOptions" :key="source" :label="source" :value="source" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.clusters.status')">
          <el-select v-model="filters.resolved" clearable :placeholder="t('qualityIntelligence.clusters.allStatuses')">
            <el-option :label="t('qualityIntelligence.clusters.open')" value="false" />
            <el-option :label="t('qualityIntelligence.clusters.resolved')" value="true" />
          </el-select>
        </el-form-item>
        <el-button type="primary" @click="resetAndLoad">{{ t('common.search') }}</el-button>
      </el-form>
    </el-card>

    <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" class="state-alert" />
    <el-card shadow="never">
      <el-table :data="clusters" v-loading="loading" row-key="id">
        <el-table-column prop="source_type" :label="t('qualityIntelligence.table.source')" width="110" />
        <el-table-column prop="test_key" :label="t('qualityIntelligence.table.testKey')" min-width="180" show-overflow-tooltip />
        <el-table-column prop="taxonomy_code" :label="t('qualityIntelligence.table.taxonomy')" width="130" />
        <el-table-column prop="signature" :label="t('qualityIntelligence.table.signature')" min-width="180" show-overflow-tooltip />
        <el-table-column prop="occurrence_count" :label="t('qualityIntelligence.clusters.occurrences')" width="100" />
        <el-table-column :label="t('qualityIntelligence.clusters.owner')" width="120">
          <template #default="scope">{{ scope.row.owner || '-' }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.clusters.defect')" width="110">
          <template #default="scope">{{ scope.row.defect || '-' }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.clusters.status')" width="110">
          <template #default="scope"><el-tag size="small" :type="scope.row.resolved_at ? 'success' : 'danger'">{{ scope.row.resolved_at ? t('qualityIntelligence.clusters.resolved') : t('qualityIntelligence.clusters.open') }}</el-tag></template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.actions')" width="170" fixed="right">
          <template #default="scope">
            <el-button text type="primary" @click="openAction(scope.row)">{{ t('qualityIntelligence.clusters.manage') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !clusters.length" :description="t('qualityIntelligence.clusters.empty')" />
      <div class="pagination-row"><el-pagination v-model:current-page="page" v-model:page-size="pageSize" layout="total, prev, pager, next" :total="total" @current-change="load" /></div>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="t('qualityIntelligence.clusters.manage')" width="480px" destroy-on-close>
      <el-form v-if="selected" label-position="top">
        <el-form-item :label="t('qualityIntelligence.clusters.projectId')"><el-input-number v-model="actionForm.project_id" :min="1" :controls="false" /></el-form-item>
        <el-form-item :label="t('qualityIntelligence.clusters.ownerId')"><el-input-number v-model="actionForm.owner_id" :min="1" :controls="false" /></el-form-item>
        <el-form-item :label="t('qualityIntelligence.clusters.defectId')"><el-input-number v-model="actionForm.defect_id" :min="1" :controls="false" /></el-form-item>
        <el-form-item :label="t('qualityIntelligence.clusters.defectTitle')"><el-input v-model="actionForm.title" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('qualityIntelligence.actions.cancel') }}</el-button>
        <el-button @click="mapProject" :loading="saving">{{ t('qualityIntelligence.clusters.mapProject') }}</el-button>
        <el-button @click="assignOwner" :loading="saving">{{ t('qualityIntelligence.clusters.assignOwner') }}</el-button>
        <el-button v-if="selected && !selected.defect" type="primary" @click="createDefect" :loading="saving">{{ t('qualityIntelligence.clusters.createDefect') }}</el-button>
        <el-button v-else type="primary" @click="linkDefect" :loading="saving">{{ t('qualityIntelligence.clusters.linkDefect') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { Refresh } from '@element-plus/icons-vue'
import {
  assignQualityFailureCluster,
  createQualityFailureDefect,
  getQualityFailureClusters,
  linkQualityFailureDefect,
  mapQualityFailureClusterProject,
} from '@/api/quality-intelligence'

const { t } = useI18n()
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const clusters = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const dialogVisible = ref(false)
const selected = ref(null)
const filters = reactive({ source_type: '', resolved: '' })
const actionForm = reactive({ project_id: null, owner_id: null, defect_id: null, title: '' })
const sourceOptions = ['API', 'UI', 'APP', 'PERF', 'EXECUTION']
const rowsFrom = payload => payload?.results || payload?.items || (Array.isArray(payload) ? payload : [])

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    const response = await getQualityFailureClusters({ page: page.value, page_size: pageSize.value, ...filters })
    clusters.value = rowsFrom(response.data)
    total.value = Number(response.data?.count ?? clusters.value.length)
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || t('qualityIntelligence.errors.loadFailures')
  } finally {
    loading.value = false
  }
}

const resetAndLoad = () => { page.value = 1; load() }
const openAction = row => {
  selected.value = row
  actionForm.project_id = row.project || null
  actionForm.owner_id = row.owner || null
  actionForm.defect_id = row.defect || null
  actionForm.title = `Automated failure: ${row.signature || row.test_key || row.id}`
  dialogVisible.value = true
}
const mapProject = async () => {
  if (!selected.value || !actionForm.project_id) return
  saving.value = true
  try {
    await mapQualityFailureClusterProject(selected.value.id, { project_id: actionForm.project_id })
    ElMessage.success(t('qualityIntelligence.clusters.saved'))
    dialogVisible.value = false
    await load()
  } catch (requestError) { ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.review')) } finally { saving.value = false }
}
const assignOwner = async () => {
  if (!selected.value) return
  saving.value = true
  try {
    await assignQualityFailureCluster(selected.value.id, { owner_id: actionForm.owner_id || null })
    ElMessage.success(t('qualityIntelligence.clusters.saved'))
    dialogVisible.value = false
    await load()
  } catch (requestError) { ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.review')) } finally { saving.value = false }
}
const createDefect = async () => {
  if (!selected.value) return
  saving.value = true
  try {
    await createQualityFailureDefect(selected.value.id, { title: actionForm.title })
    ElMessage.success(t('qualityIntelligence.clusters.defectCreated'))
    dialogVisible.value = false
    await load()
  } catch (requestError) { ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.review')) } finally { saving.value = false }
}
const linkDefect = async () => {
  if (!selected.value || !actionForm.defect_id) return
  saving.value = true
  try {
    await linkQualityFailureDefect(selected.value.id, { defect_id: actionForm.defect_id })
    ElMessage.success(t('qualityIntelligence.clusters.saved'))
    dialogVisible.value = false
    await load()
  } catch (requestError) { ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.review')) } finally { saving.value = false }
}

onMounted(load)
</script>

<style scoped>
.cluster-page { color: #101828; padding: 24px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.eyebrow { margin: 0 0 6px; color: #2f6bff; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.subtitle { margin: 7px 0 0; color: #667085; }
.filter-card { margin-bottom: 16px; }
.state-alert { margin-bottom: 16px; }
.pagination-row { display: flex; justify-content: flex-end; padding-top: 18px; }
@media (max-width: 768px) { .page-header { flex-direction: column; } }
</style>
