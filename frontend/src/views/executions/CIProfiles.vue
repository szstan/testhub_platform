<template>
  <div class="profile-view">
    <div class="page-header">
      <div><h1>{{ $t('execution.profiles.title') }}</h1><p>{{ $t('execution.profiles.subtitle') }}</p></div>
      <el-button type="primary" @click="openCreate">{{ $t('execution.profiles.create') }}</el-button>
    </div>
    <el-card shadow="never">
      <el-table :data="profiles" v-loading="loading">
        <el-table-column prop="name" :label="$t('execution.profiles.name')" />
        <el-table-column prop="provider" :label="$t('execution.profiles.provider')" />
        <el-table-column prop="source_type" :label="$t('execution.profiles.source')" />
        <el-table-column :label="$t('execution.profiles.webhook')">
          <template #default="scope"><el-tag :type="scope.row.webhook_enabled ? 'success' : 'info'">{{ scope.row.webhook_enabled ? 'ON' : 'OFF' }}</el-tag></template>
        </el-table-column>
        <el-table-column :label="$t('execution.profiles.secret')">
          <template #default="scope"><el-tag :type="scope.row.secret_configured ? 'success' : 'warning'">{{ scope.row.secret_configured ? 'configured' : 'missing' }}</el-tag></template>
        </el-table-column>
        <el-table-column :label="$t('execution.profiles.actions')" width="130">
          <template #default="scope"><el-button link type="primary" @click="openEdit(scope.row)">{{ $t('common.edit') }}</el-button></template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !profiles.length" :description="$t('execution.profiles.empty')" />
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editing ? $t('execution.profiles.edit') : $t('execution.profiles.create')" width="560px">
      <el-form :model="form" label-width="130px">
        <el-form-item :label="$t('execution.profiles.name')"><el-input v-model="form.name" /></el-form-item>
        <el-form-item :label="$t('execution.profiles.projectId')"><el-input-number v-model="form.project" :min="1" :controls="false" /></el-form-item>
        <el-form-item :label="$t('execution.profiles.provider')"><el-select v-model="form.provider"><el-option v-for="x in providers" :key="x" :label="x" :value="x" /></el-select></el-form-item>
        <el-form-item :label="$t('execution.profiles.source')"><el-select v-model="form.source_type"><el-option v-for="x in sources" :key="x" :label="x" :value="x" /></el-select></el-form-item>
        <el-form-item :label="$t('execution.profiles.secretRef')"><el-input v-model="form.webhook_secret_ref" :placeholder="$t('execution.profiles.secretHint')" /></el-form-item>
        <el-form-item :label="$t('execution.profiles.webhook')"><el-switch v-model="form.webhook_enabled" /></el-form-item>
        <el-form-item :label="$t('execution.profiles.qualityGate')"><el-input v-model="form.quality_gate_text" type="textarea" :rows="3" placeholder='{"pass_rate":{"min":95}}' /></el-form-item>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button><el-button type="primary" :loading="saving" @click="save">{{ $t('common.save') }}</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/utils/api'
import { createExecutionProfile, listExecutionProfiles, updateExecutionProfile } from '@/api/executions'

const profiles = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editing = ref(null)
const providers = ['github', 'gitlab', 'jenkins', 'generic']
const sources = ['api', 'ui', 'app', 'performance', 'ci', 'manual']
const form = ref({ name: '', project: null, provider: 'generic', source_type: 'manual', webhook_secret_ref: '', webhook_enabled: false, quality_gate_text: '{}' })

const load = async () => {
  loading.value = true
  try {
    const response = await listExecutionProfiles()
    profiles.value = response.data?.results || response.data || []
  } catch (error) { ElMessage.error(error.response?.data?.detail || '加载执行配置失败') } finally { loading.value = false }
}
const openCreate = () => { editing.value = null; form.value = { name: '', project: null, provider: 'generic', source_type: 'manual', webhook_secret_ref: '', webhook_enabled: false, quality_gate_text: '{}' }; dialogVisible.value = true }
const openEdit = row => { editing.value = row.id; form.value = { ...row, quality_gate_text: JSON.stringify(row.quality_gate || {}) }; dialogVisible.value = true }
const save = async () => {
  saving.value = true
  try {
    const payload = { ...form.value, quality_gate: JSON.parse(form.value.quality_gate_text || '{}') }
    delete payload.quality_gate_text
    if (editing.value) await updateExecutionProfile(editing.value, payload)
    else await createExecutionProfile(payload)
    dialogVisible.value = false
    await load()
    ElMessage.success('保存成功')
  } catch (error) { ElMessage.error(error.response?.data?.detail || '保存失败') } finally { saving.value = false }
}
onMounted(load)
</script>

<style scoped>
.profile-view { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.page-header p { color: var(--el-text-color-secondary); }
</style>
