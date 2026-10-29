<template>
  <div class="source-bindings-page">
    <div class="page-header">
      <div>
        <h1>{{ $t('execution.sourceBindings.title') }}</h1>
        <p>{{ $t('execution.sourceBindings.subtitle') }}</p>
      </div>
      <div class="header-actions">
        <el-button @click="load">{{ $t('common.refresh') }}</el-button>
        <el-button type="primary" @click="openCreate">{{ $t('execution.sourceBindings.create') }}</el-button>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent="load">
        <el-form-item :label="$t('execution.sourceBindings.projectId')">
          <el-input-number v-model="projectFilter" :min="1" :controls="false" clearable />
        </el-form-item>
        <el-form-item :label="$t('execution.sourceBindings.sourceType')">
          <el-select v-model="sourceFilter" clearable :placeholder="$t('execution.sourceBindings.allSources')">
            <el-option v-for="item in sourceTypes" :key="item" :label="item.toUpperCase()" :value="item" />
          </el-select>
        </el-form-item>
        <el-button type="primary" @click="load">{{ $t('common.search') }}</el-button>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <el-table :data="bindings" v-loading="loading" row-key="id">
        <el-table-column prop="project" :label="$t('execution.sourceBindings.projectId')" width="95" />
        <el-table-column prop="source_type" :label="$t('execution.sourceBindings.sourceType')" width="110" />
        <el-table-column prop="source_id" :label="$t('execution.sourceBindings.sourceId')" min-width="220" show-overflow-tooltip />
        <el-table-column prop="source_project_id" :label="$t('execution.sourceBindings.sourceProject')" min-width="150" show-overflow-tooltip />
        <el-table-column :label="$t('execution.sourceBindings.enabled')" width="110">
          <template #default="scope">
            <el-tag :type="scope.row.enabled ? 'success' : 'info'">{{ scope.row.enabled ? $t('execution.sourceBindings.enabledState') : $t('execution.sourceBindings.disabledState') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('execution.sourceBindings.readiness')" width="150">
          <template #default="scope">
            <el-tag :type="statusType(scope.row.executor_status)">{{ statusLabel(scope.row.executor_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('execution.sourceBindings.route')" width="105">
          <template #default="scope">{{ scope.row.route_configured ? $t('execution.sourceBindings.configured') : $t('execution.sourceBindings.missing') }}</template>
        </el-table-column>
        <el-table-column :label="$t('execution.sourceBindings.actions')" width="170" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click="openEdit(scope.row)">{{ $t('common.edit') }}</el-button>
            <el-button v-if="scope.row.enabled" link type="warning" @click="disable(scope.row)">{{ $t('execution.sourceBindings.disable') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && bindings.length === 0" :description="$t('execution.sourceBindings.empty')" />
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editing ? $t('execution.sourceBindings.edit') : $t('execution.sourceBindings.create')" width="620px" destroy-on-close>
      <el-form :model="form" label-width="150px">
        <el-form-item :label="$t('execution.sourceBindings.projectId')" required>
          <el-input-number v-model="form.project" :min="1" :controls="false" />
        </el-form-item>
        <el-form-item :label="$t('execution.sourceBindings.sourceType')" required>
          <el-select v-model="form.source_type">
            <el-option v-for="item in sourceTypes" :key="item" :label="item.toUpperCase()" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('execution.sourceBindings.sourceId')" required>
          <el-input v-model="form.source_id" placeholder="api:suite:42" />
        </el-form-item>
        <el-form-item :label="$t('execution.sourceBindings.sourceProject')" required>
          <el-input v-model="form.source_project_id" />
        </el-form-item>
        <el-form-item :label="$t('execution.sourceBindings.metadata')">
          <el-input v-model="form.metadataText" type="textarea" :rows="5" placeholder='{"environment":"staging"}' />
        </el-form-item>
        <el-form-item :label="$t('execution.sourceBindings.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createExecutionSourceBinding,
  disableExecutionSourceBinding,
  listExecutionSourceBindings,
  updateExecutionSourceBinding,
} from '@/api/executions'

const sourceTypes = ['api', 'ui', 'app', 'performance']
const bindings = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editing = ref(null)
const projectFilter = ref(null)
const sourceFilter = ref('')
const form = ref({
  project: null,
  source_type: 'api',
  source_id: '',
  source_project_id: '',
  metadataText: '{}',
  enabled: true,
})

const rowsFrom = payload => payload?.results || (Array.isArray(payload) ? payload : [])
const statusLabel = value => ({ ready: 'Ready', unsupported: 'Unsupported', disabled_binding: 'Disabled', missing_binding: 'Missing binding', invalid_binding: 'Invalid source' }[value] || value || '-')
const statusType = value => ({ ready: 'success', unsupported: 'warning', disabled_binding: 'info', missing_binding: 'danger', invalid_binding: 'danger' }[value] || 'info')

const load = async () => {
  loading.value = true
  try {
    const response = await listExecutionSourceBindings({
      project: projectFilter.value || undefined,
      source_type: sourceFilter.value || undefined,
      page_size: 100,
    })
    bindings.value = rowsFrom(response.data)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载 source binding 失败')
  } finally {
    loading.value = false
  }
}

const resetForm = () => {
  form.value = {
    project: projectFilter.value || null,
    source_type: 'api',
    source_id: '',
    source_project_id: '',
    metadataText: '{}',
    enabled: true,
  }
}

const openCreate = () => {
  editing.value = null
  resetForm()
  dialogVisible.value = true
}

const openEdit = row => {
  editing.value = row.id
  form.value = {
    project: row.project,
    source_type: row.source_type,
    source_id: row.source_id,
    source_project_id: row.source_project_id || '',
    metadataText: JSON.stringify(row.metadata || {}, null, 2),
    enabled: row.enabled,
  }
  dialogVisible.value = true
}

const save = async () => {
  let metadata
  try {
    metadata = JSON.parse(form.value.metadataText || '{}')
  } catch {
    ElMessage.warning('metadata 必须是合法 JSON 对象')
    return
  }
  if (!metadata || Array.isArray(metadata) || typeof metadata !== 'object') {
    ElMessage.warning('metadata 必须是 JSON 对象')
    return
  }
  saving.value = true
  try {
    const payload = { ...form.value, metadata }
    delete payload.metadataText
    if (editing.value) await updateExecutionSourceBinding(editing.value, payload)
    else await createExecutionSourceBinding(payload)
    dialogVisible.value = false
    ElMessage.success('保存成功')
    await load()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '保存 source binding 失败')
  } finally {
    saving.value = false
  }
}

const disable = async row => {
  try {
    await ElMessageBox.confirm('禁用后该 source 不会被统一执行使用，确定继续吗？', '确认操作', { type: 'warning' })
    await disableExecutionSourceBinding(row.id)
    ElMessage.success('已禁用')
    await load()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.response?.data?.detail || '禁用失败')
  }
}

onMounted(load)
</script>

<style scoped>
.source-bindings-page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.page-header p { margin: 8px 0 0; color: var(--el-text-color-secondary); }
.header-actions { display: flex; gap: 10px; }
.filter-card { margin-bottom: 16px; }
</style>
