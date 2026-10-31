<template>
  <div class="dataset-view">
    <div class="page-header">
      <div><h1>{{ $t('execution.datasets.title') }}</h1><p>{{ $t('execution.datasets.subtitle') }}</p></div>
      <el-button type="primary" @click="dialogVisible = true">{{ $t('execution.datasets.create') }}</el-button>
    </div>
    <el-tabs v-model="tab">
      <el-tab-pane :label="$t('execution.datasets.datasets')" name="datasets">
        <el-card shadow="never"><el-table :data="datasets" v-loading="datasetsLoading">
          <el-table-column prop="name" :label="$t('execution.datasets.name')" />
          <el-table-column prop="project" :label="$t('execution.datasets.project')" />
          <el-table-column prop="variants_count" :label="$t('execution.datasets.variants')" />
          <el-table-column prop="enabled" :label="$t('execution.datasets.enabled')" />
        </el-table>
        <div class="pagination">
          <el-pagination
            :current-page="datasetPagination.page"
            :page-size="datasetPagination.pageSize"
            :page-sizes="[10, 20, 50, 100]"
            :total="datasetPagination.total"
            layout="total, sizes, prev, pager, next, jumper"
            @current-change="changeDatasetPage"
            @size-change="changeDatasetPageSize"
          />
        </div>
        <el-empty v-if="!datasetsLoading && !datasets.length" :description="$t('execution.datasets.empty')" />
      </el-card>
      </el-tab-pane>
      <el-tab-pane :label="$t('execution.datasets.plugins')" name="plugins">
        <el-card shadow="never"><el-table :data="plugins" v-loading="pluginsLoading">
          <el-table-column prop="name" :label="$t('execution.datasets.name')" />
          <el-table-column prop="version" label="Version" />
          <el-table-column prop="kind" :label="$t('execution.datasets.kind')" />
          <el-table-column prop="enabled" :label="$t('execution.datasets.enabled')" />
          <el-table-column prop="capabilities" :label="$t('execution.datasets.capabilities')" />
        </el-table>
        <div class="pagination">
          <el-pagination
            :current-page="pluginPagination.page"
            :page-size="pluginPagination.pageSize"
            :page-sizes="[10, 20, 50, 100]"
            :total="pluginPagination.total"
            layout="total, sizes, prev, pager, next, jumper"
            @current-change="changePluginPage"
            @size-change="changePluginPageSize"
          />
        </div></el-card>
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
const datasetsLoading = ref(false)
const pluginsLoading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const datasets = ref([])
const plugins = ref([])
const datasetPagination = ref({ page: 1, pageSize: 20, total: 0 })
const pluginPagination = ref({ page: 1, pageSize: 20, total: 0 })
let datasetRequestId = 0
let pluginRequestId = 0
const form = ref({ name: '', project: null, matrix_text: '{}', secret_refs_text: '' })

const pageData = response => {
  const data = response.data || {}
  const results = data.results || (Array.isArray(data) ? data : [])
  return { results, total: Number(data.count ?? results.length) }
}

const loadDatasets = async () => {
  const requestId = ++datasetRequestId
  const pageSize = datasetPagination.value.pageSize
  let page = datasetPagination.value.page
  datasetsLoading.value = true
  try {
    const fetchPage = currentPage => listExecutionDatasets({ page: currentPage, page_size: pageSize, ordering: 'project,name' })
    let response = await fetchPage(page)
    if (requestId !== datasetRequestId) return
    let { results, total } = pageData(response)
    const lastPage = Math.max(1, Math.ceil(total / pageSize))
    if (page > lastPage) {
      page = lastPage
      datasetPagination.value.page = page
      response = await fetchPage(page)
      if (requestId !== datasetRequestId) return
      const nextPageData = pageData(response)
      results = nextPageData.results
      total = nextPageData.total
    }
    datasets.value = results
    datasetPagination.value.total = total
  } catch (error) {
    if (requestId === datasetRequestId) ElMessage.error(error.response?.data?.detail || '加载数据集失败')
  } finally {
    if (requestId === datasetRequestId) datasetsLoading.value = false
  }
}

const loadPlugins = async () => {
  const requestId = ++pluginRequestId
  const pageSize = pluginPagination.value.pageSize
  let page = pluginPagination.value.page
  pluginsLoading.value = true
  try {
    const fetchPage = currentPage => listExecutionPlugins({ page: currentPage, page_size: pageSize, ordering: 'name,version,id' })
    let response = await fetchPage(page)
    if (requestId !== pluginRequestId) return
    let { results, total } = pageData(response)
    const lastPage = Math.max(1, Math.ceil(total / pageSize))
    if (page > lastPage) {
      page = lastPage
      pluginPagination.value.page = page
      response = await fetchPage(page)
      if (requestId !== pluginRequestId) return
      const nextPageData = pageData(response)
      results = nextPageData.results
      total = nextPageData.total
    }
    plugins.value = results
    pluginPagination.value.total = total
  } catch (error) {
    if (requestId === pluginRequestId) ElMessage.error(error.response?.data?.detail || '加载插件失败')
  } finally {
    if (requestId === pluginRequestId) pluginsLoading.value = false
  }
}

const changeDatasetPage = page => { if (page === datasetPagination.value.page) return; datasetPagination.value.page = page; loadDatasets() }
const changeDatasetPageSize = pageSize => { datasetPagination.value.pageSize = pageSize; datasetPagination.value.page = 1; loadDatasets() }
const changePluginPage = page => { if (page === pluginPagination.value.page) return; pluginPagination.value.page = page; loadPlugins() }
const changePluginPageSize = pageSize => { pluginPagination.value.pageSize = pageSize; pluginPagination.value.page = 1; loadPlugins() }
const load = () => Promise.all([loadDatasets(), loadPlugins()])
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
    await loadDatasets()
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
.pagination { display: flex; justify-content: flex-end; margin-top: 16px; }
</style>
