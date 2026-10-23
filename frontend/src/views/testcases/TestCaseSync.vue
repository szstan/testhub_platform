<template>
  <div class="sync-page">
    <div class="page-header">
      <div>
        <h1>{{ $t('testcase.sync.title') }}</h1>
        <p>{{ $t('testcase.sync.subtitle') }}</p>
      </div>
      <el-button type="primary" @click="openCreate">{{ $t('testcase.sync.create') }}</el-button>
    </div>

    <el-card shadow="never">
      <el-table :data="profiles" v-loading="loading">
        <el-table-column prop="name" :label="$t('testcase.sync.name')" />
        <el-table-column prop="provider" :label="$t('testcase.sync.provider')" />
        <el-table-column prop="repository_url" :label="$t('testcase.sync.repository')" show-overflow-tooltip />
        <el-table-column prop="branch" :label="$t('testcase.sync.branch')" />
        <el-table-column prop="last_commit_sha" :label="$t('testcase.sync.lastCommit')" />
        <el-table-column :label="$t('testcase.sync.actions')" width="220">
          <template #default="scope">
            <el-button link type="primary" :loading="syncing === scope.row.id" @click="sync(scope.row)">{{ $t('testcase.sync.sync') }}</el-button>
            <el-button link @click="edit(scope.row)">{{ $t('common.edit') }}</el-button>
            <el-button link type="danger" @click="remove(scope.row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && profiles.length === 0" :description="$t('testcase.sync.empty')" />
    </el-card>

    <el-dialog v-model="visible" :title="editing ? $t('testcase.sync.edit') : $t('testcase.sync.create')" width="620px">
      <el-form :model="form" label-width="150px">
        <el-form-item :label="$t('testcase.sync.name')"><el-input v-model="form.name" /></el-form-item>
        <el-form-item :label="$t('testcase.sync.projectId')"><el-input-number v-model="form.project" :min="1" :controls="false" /></el-form-item>
        <el-form-item :label="$t('testcase.sync.provider')"><el-select v-model="form.provider"><el-option label="GitHub" value="github" /><el-option label="GitLab" value="gitlab" /></el-select></el-form-item>
        <el-form-item :label="$t('testcase.sync.repository')"><el-input v-model="form.repository_url" placeholder="https://github.com/org/repo" /></el-form-item>
        <el-form-item :label="$t('testcase.sync.apiBase')"><el-input v-model="form.api_base_url" placeholder="GitLab self-hosted API base" /></el-form-item>
        <el-form-item :label="$t('testcase.sync.branch')"><el-input v-model="form.branch" /></el-form-item>
        <el-form-item :label="$t('testcase.sync.path')"><el-input v-model="form.path" placeholder="tests/" /></el-form-item>
        <el-form-item :label="$t('testcase.sync.tokenRef')"><el-input v-model="form.token_env_ref" placeholder="TESTHUB_GIT_TOKEN" /></el-form-item>
        <el-form-item :label="$t('testcase.sync.enabled')"><el-switch v-model="form.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="visible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resultVisible" :title="$t('testcase.sync.result')" width="720px">
      <el-descriptions v-if="result" :column="2" border>
        <el-descriptions-item :label="$t('testcase.sync.commit')">{{ result.commit || '-' }}</el-descriptions-item>
        <el-descriptions-item :label="$t('testcase.sync.files')">{{ result.files }}</el-descriptions-item>
        <el-descriptions-item :label="$t('testcase.sync.created')">{{ result.created }}</el-descriptions-item>
        <el-descriptions-item :label="$t('testcase.sync.updated')">{{ result.updated }}</el-descriptions-item>
        <el-descriptions-item :label="$t('testcase.sync.skipped')">{{ result.skipped }}</el-descriptions-item>
        <el-descriptions-item :label="$t('testcase.sync.conflicts')">{{ result.conflicts }}</el-descriptions-item>
      </el-descriptions>
      <el-alert v-if="result?.warnings?.length" class="warnings" type="warning" :closable="false">
        <div v-for="warning in result.warnings" :key="warning">{{ warning }}</div>
      </el-alert>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createTestCaseSyncProfile,
  deleteTestCaseSyncProfile,
  listTestCaseSyncProfiles,
  syncTestCaseSyncProfile,
  updateTestCaseSyncProfile,
} from '@/api/testcase-sync'

const profiles = ref([])
const loading = ref(false)
const saving = ref(false)
const syncing = ref(null)
const visible = ref(false)
const resultVisible = ref(false)
const editing = ref(null)
const result = ref(null)
const form = ref({ name: '', project: null, provider: 'github', repository_url: '', api_base_url: '', branch: 'main', path: '', token_env_ref: '', enabled: true })

const load = async () => {
  loading.value = true
  try {
    const response = await listTestCaseSyncProfiles()
    profiles.value = response.data?.results || response.data || []
  } catch (error) { ElMessage.error(error.response?.data?.detail || '加载同步配置失败') } finally { loading.value = false }
}
const openCreate = () => { editing.value = null; form.value = { name: '', project: null, provider: 'github', repository_url: '', api_base_url: '', branch: 'main', path: '', token_env_ref: '', enabled: true }; visible.value = true }
const edit = row => { editing.value = row.id; form.value = { ...row, token_env_ref: '' }; visible.value = true }
const save = async () => {
  saving.value = true
  try {
    const payload = { ...form.value }
    if (!payload.token_env_ref) delete payload.token_env_ref
    if (editing.value) await updateTestCaseSyncProfile(editing.value, payload)
    else await createTestCaseSyncProfile(payload)
    visible.value = false
    await load()
    ElMessage.success('保存成功')
  } catch (error) { ElMessage.error(error.response?.data?.detail || '保存失败') } finally { saving.value = false }
}
const sync = async row => {
  syncing.value = row.id
  try {
    const response = await syncTestCaseSyncProfile(row.id)
    result.value = response.data
    resultVisible.value = true
    await load()
  } catch (error) { ElMessage.error(error.response?.data?.error || '同步失败') } finally { syncing.value = null }
}
const remove = async row => {
  await ElMessageBox.confirm(`确定删除同步配置 ${row.name} 吗？`, '确认')
  await deleteTestCaseSyncProfile(row.id)
  await load()
}
onMounted(load)
</script>

<style scoped>
.sync-page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.page-header p { color: var(--el-text-color-secondary); }
.warnings { margin-top: 18px; }
</style>
