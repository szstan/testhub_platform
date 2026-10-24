<template>
  <div class="dataset-view">
    <div class="page-header">
      <div><h1>{{ $t('execution.datasets.title') }}</h1><p>{{ $t('execution.datasets.subtitle') }}</p></div>
      <el-button type="primary" @click="dialogVisible = true">{{ $t('execution.datasets.create') }}</el-button>
    </div>
    <el-tabs v-model="tab">
      <el-tab-pane :label="$t('execution.datasets.datasets')" name="datasets">
        <el-card shadow="never"><el-table :data="datasets" v-loading="loading">
          <el-table-column prop="name" :label="$t('execution.datasets.name')" />
          <el-table-column prop="project" :label="$t('execution.datasets.project')" />
          <el-table-column prop="variants_count" :label="$t('execution.datasets.variants')" />
          <el-table-column prop="enabled" :label="$t('execution.datasets.enabled')" />
        </el-table><el-empty v-if="!loading && !datasets.length" :description="$t('execution.datasets.empty')" /></el-card>
      </el-tab-pane>
      <el-tab-pane :label="$t('execution.datasets.plugins')" name="plugins">
        <el-card shadow="never"><el-table :data="plugins" v-loading="loading">
          <el-table-column prop="name" :label="$t('execution.datasets.name')" />
          <el-table-column prop="version" label="Version" />
          <el-table-column prop="kind" :label="$t('execution.datasets.kind')" />
          <el-table-column prop="enabled" :label="$t('execution.datasets.enabled')" />
          <el-table-column prop="capabilities" :label="$t('execution.datasets.capabilities')" />
        </el-table></el-card>
      </el-tab-pane>
    </el-tabs>
    <el-dialog v-model="dialogVisible" :title="$t('execution.datasets.create')" width="560px">
      <el-form :model="form" label-width="120px">
        <el-form-item :label="$t('execution.datasets.name')"><el-input v-model="form.name" /></el-form-item>
        <el-form-item :label="$t('execution.datasets.projectId')"><el-input-number v-model="form.project" :min="1" :controls="false" /></el-form-item>
        <el-form-item :label="$t('execution.datasets.matrix')"><el-input v-model="form.matrix_text" type="textarea" :rows="5" placeholder='{"browser":["chrome","firefox"]}' /></el-form-item>
        <el-form-item :label="$t('execution.datasets.secretRefs')"><el-input v-model="form.secret_refs_text" placeholder="ENV_SECRET_NAME,OTHER_SECRET" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button><el-button type="primary" :loading="saving" @click="createDataset">{{ $t('common.save') }}</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createExecutionDataset, listExecutionDatasets, listExecutionPlugins } from '@/api/executions'

const tab = ref('datasets')
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const datasets = ref([])
const plugins = ref([])
const form = ref({ name: '', project: null, matrix_text: '{}', secret_refs_text: '' })

const load = async () => {
  loading.value = true
  try {
    const [datasetResponse, pluginResponse] = await Promise.all([listExecutionDatasets(), listExecutionPlugins()])
    datasets.value = datasetResponse.data?.results || datasetResponse.data || []
    plugins.value = pluginResponse.data?.results || pluginResponse.data || []
  } catch (error) { ElMessage.error(error.response?.data?.detail || '加载数据集失败') } finally { loading.value = false }
}
const createDataset = async () => {
  saving.value = true
  try {
    await createExecutionDataset({
      name: form.value.name,
      project: form.value.project,
      matrix: JSON.parse(form.value.matrix_text || '{}'),
      secret_refs: form.value.secret_refs_text.split(',').map(item => item.trim()).filter(Boolean),
    })
    dialogVisible.value = false
    form.value = { name: '', project: null, matrix_text: '{}', secret_refs_text: '' }
    await load()
    ElMessage.success('保存成功')
  } catch (error) { ElMessage.error(error.response?.data?.detail || '保存失败') } finally { saving.value = false }
}
onMounted(load)
</script>

<style scoped>
.dataset-view { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.page-header p { color: var(--el-text-color-secondary); }
</style>
