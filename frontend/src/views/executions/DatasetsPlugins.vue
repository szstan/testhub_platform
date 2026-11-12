<template>
  <div class="dataset-view">
    <div class="page-header">
      <div>
        <h1>{{ $t('execution.datasets.title') }}</h1>
        <p>{{ $t('execution.datasets.subtitle') }}</p>
      </div>
      <el-button v-if="tab === 'datasets'" type="primary" @click="dialogVisible = true">{{ $t('execution.datasets.create') }}</el-button>
      <el-button v-else-if="tab === 'matrices'" type="primary" :loading="datasetOptionsLoading" :disabled="matrixPermissionDenied || !datasetOptions.length" @click="openMatrixCreate">{{ $t('execution.datasets.createMatrix') }}</el-button>
    </div>
    <el-tabs v-model="tab" @tab-change="handleTabChange">
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
          <el-table-column prop="version" :label="$t('execution.datasets.version')" />
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
      <el-tab-pane :label="$t('execution.datasets.dataMatrices')" name="matrices">
        <el-alert v-if="matrixError" :title="matrixError" :type="matrixPermissionDenied ? 'warning' : 'error'" show-icon :closable="false" class="matrix-alert">
          <template #default><el-button v-if="!matrixPermissionDenied" link type="primary" @click="loadMatrices">{{ $t('execution.datasets.retry') }}</el-button></template>
        </el-alert>
        <el-alert v-if="datasetOptionsError" :title="datasetOptionsError" :type="datasetOptionsPermissionDenied ? 'warning' : 'error'" show-icon :closable="false" class="matrix-alert">
          <template #default><el-button v-if="!datasetOptionsPermissionDenied" link type="primary" @click="retryDatasetOptions">{{ $t('execution.datasets.retry') }}</el-button></template>
        </el-alert>
        <el-alert v-if="!datasetOptions.length && !datasetOptionsLoading && !datasetOptionsError && !matrixLoading && !matrixError" :title="$t('execution.datasets.noDatasetOptions')" type="info" :closable="false" class="matrix-alert" />
        <el-form :inline="true" class="matrix-filter" @submit.prevent="loadMatrices">
          <el-form-item :label="$t('execution.datasets.dataset')">
            <el-select v-model="matrixDatasetFilter" clearable filterable :loading="datasetOptionsLoading" :placeholder="$t('execution.datasets.allDatasets')" @change="changeMatrixDataset">
              <el-option v-for="item in datasetOptions" :key="item.id" :label="`${item.name} (#${item.id})`" :value="item.id" />
            </el-select>
          </el-form-item>
          <el-button @click="loadMatrices">{{ $t('common.refresh') }}</el-button>
        </el-form>
        <el-table :data="matrices" v-loading="matrixLoading || datasetOptionsLoading" row-key="id">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="name" :label="$t('execution.datasets.name')" min-width="180" />
          <el-table-column :label="$t('execution.datasets.dataset')" min-width="170"><template #default="{ row }">{{ datasetName(row.dataset) }}</template></el-table-column>
          <el-table-column :label="$t('execution.datasets.columns')" width="100"><template #default="{ row }">{{ row.columns?.length || 0 }}</template></el-table-column>
          <el-table-column :label="$t('execution.datasets.rows')" width="100"><template #default="{ row }">{{ row.rows?.length || 0 }}</template></el-table-column>
          <el-table-column :label="$t('execution.datasets.enabled')" width="100"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? $t('execution.datasets.enabledState') : $t('execution.datasets.disabledState') }}</el-tag></template></el-table-column>
          <el-table-column :label="$t('execution.datasets.actions')" width="150" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" :disabled="matrixPermissionDenied" @click="openMatrixEdit(row)">{{ $t('common.edit') }}</el-button>
              <el-button link type="danger" :disabled="matrixPermissionDenied" @click="removeMatrix(row)">{{ $t('common.delete') }}</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty :description="matrixLoading || datasetOptionsLoading || datasetOptionsError ? '' : $t('execution.datasets.noMatrices')" /></template>
        </el-table>
        <div class="pagination">
          <el-pagination
            v-model:current-page="matrixPagination.page"
            v-model:page-size="matrixPagination.pageSize"
            :page-sizes="[10, 20, 50, 100]"
            :total="matrixPagination.total"
            layout="total, sizes, prev, pager, next, jumper"
            @current-change="loadMatrices"
            @size-change="changeMatrixPageSize"
          />
        </div>
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
    <el-dialog v-model="matrixDialogVisible" :title="editingMatrix ? $t('execution.datasets.editMatrix') : $t('execution.datasets.createMatrix')" width="680px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item :label="$t('execution.datasets.dataset')" required>
          <el-select v-model="matrixForm.dataset" filterable :loading="datasetOptionsLoading" :placeholder="$t('execution.datasets.selectDataset')">
            <el-option v-for="item in datasetOptions" :key="item.id" :label="`${item.name} (#${item.id})`" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('execution.datasets.name')" required><el-input v-model="matrixForm.name" maxlength="160" /></el-form-item>
        <el-form-item :label="$t('execution.datasets.columns')"><el-input v-model="matrixForm.columnsText" type="textarea" :rows="4" spellcheck="false" placeholder='["browser", "region"]' /></el-form-item>
        <el-form-item :label="$t('execution.datasets.rows')"><el-input v-model="matrixForm.rowsText" type="textarea" :rows="8" spellcheck="false" placeholder='[{"browser":"chrome","region":"us-east"}]' /></el-form-item>
        <el-form-item :label="$t('execution.datasets.enabled')"><el-switch v-model="matrixForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="matrixDialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="matrixSaving" :disabled="matrixPermissionDenied" @click="saveMatrix">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import {
  createExecutionDataMatrix,
  createExecutionDataset,
  deleteExecutionDataMatrix,
  listExecutionDataMatrices,
  listExecutionDatasets,
  listExecutionPlugins,
  updateExecutionDataMatrix,
} from '@/api/executions'

const { t } = useI18n()
const tab = ref('datasets')
const datasetsLoading = ref(false)
const pluginsLoading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const datasets = ref([])
const plugins = ref([])
const datasetPagination = ref({ page: 1, pageSize: 20, total: 0 })
const pluginPagination = ref({ page: 1, pageSize: 20, total: 0 })
const matrixPagination = ref({ page: 1, pageSize: 20, total: 0 })
const datasetOptions = ref([])
const datasetOptionsLoading = ref(false)
const datasetOptionsError = ref('')
const datasetOptionsPermissionDenied = ref(false)
const matrices = ref([])
const matrixLoading = ref(false)
const matrixSaving = ref(false)
const matrixDialogVisible = ref(false)
const matrixError = ref('')
const matrixPermissionDenied = ref(false)
const matrixDatasetFilter = ref(null)
const editingMatrix = ref(null)
const matrixForm = ref({ dataset: null, name: '', columnsText: '[]', rowsText: '[]', enabled: true })
let matrixRequestId = 0
let datasetRequestId = 0
let datasetOptionsRequestId = 0
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
const datasetName = id => datasetOptions.value.find(item => Number(item.id) === Number(id))?.name || `#${id}`
const ensureDatasetOption = id => {
  if (id == null || datasetOptions.value.some(item => Number(item.id) === Number(id))) return
  datasetOptions.value = [...datasetOptions.value, { id, name: t('execution.datasets.dataset') }]
}
const retainSelectedDatasetOptions = options => {
  const selectedIds = [matrixDatasetFilter.value]
  if (matrixDialogVisible.value && matrixForm.value.dataset != null) selectedIds.push(matrixForm.value.dataset)
  for (const id of selectedIds) {
    if (id == null || options.some(item => Number(item.id) === Number(id))) continue
    const existing = datasetOptions.value.find(item => Number(item.id) === Number(id))
    options.push(existing || { id, name: t('execution.datasets.dataset') })
  }
  return options
}
const loadDatasetOptions = async () => {
  const requestId = ++datasetOptionsRequestId
  const pageSize = 100
  const pageConcurrency = 4
  datasetOptionsLoading.value = true
  datasetOptionsError.value = ''
  datasetOptionsPermissionDenied.value = false
  try {
    const fetchPage = page => listExecutionDatasets({ page, page_size: pageSize, ordering: 'project,name,id' })
    const response = await fetchPage(1)
    if (requestId !== datasetOptionsRequestId) return false
    const { results, total } = pageData(response)
    const options = [...results]
    const pageCount = Math.max(1, Math.ceil(total / pageSize))
    for (let firstPage = 2; firstPage <= pageCount; firstPage += pageConcurrency) {
      const pageNumbers = Array.from(
        { length: Math.min(pageConcurrency, pageCount - firstPage + 1) },
        (_, index) => firstPage + index,
      )
      const pages = await Promise.all(pageNumbers.map(fetchPage))
      if (requestId !== datasetOptionsRequestId) return false
      for (const pageResponse of pages) options.push(...pageData(pageResponse).results)
    }
    if (requestId !== datasetOptionsRequestId) return false
    datasetOptions.value = retainSelectedDatasetOptions(options)
    return true
  } catch (error) {
    if (requestId !== datasetOptionsRequestId) return false
    datasetOptionsError.value = [401, 403, 404].includes(error.response?.status)
      ? t('execution.datasets.permissionDenied')
      : error.response?.data?.detail || error.message || t('execution.datasets.loadFailed')
    datasetOptionsPermissionDenied.value = [401, 403, 404].includes(error.response?.status)
    return false
  } finally {
    if (requestId === datasetOptionsRequestId) datasetOptionsLoading.value = false
  }
}
const retryDatasetOptions = async () => {
  const loaded = await loadDatasetOptions()
  if (loaded) await loadMatrices()
  return loaded
}
const loadMatrices = async () => {
  const requestId = ++matrixRequestId
  matrixLoading.value = true
  matrixError.value = ''
  matrixPermissionDenied.value = false
  try {
    const pageSize = matrixPagination.value.pageSize
    let page = matrixPagination.value.page
    const params = { page, page_size: pageSize, ordering: 'dataset,name,id' }
    if (matrixDatasetFilter.value) params.dataset = matrixDatasetFilter.value
    let response = await listExecutionDataMatrices(params)
    if (requestId !== matrixRequestId) return
    let data = response.data || {}
    let rows = data.results || (Array.isArray(data) ? data : [])
    let total = Number(data.count ?? rows.length)
    const lastPage = Math.max(1, Math.ceil(total / pageSize))
    if (page > lastPage) {
      page = lastPage
      matrixPagination.value.page = page
      response = await listExecutionDataMatrices({ ...params, page })
      if (requestId !== matrixRequestId) return
      data = response.data || {}
      rows = data.results || (Array.isArray(data) ? data : [])
      total = Number(data.count ?? rows.length)
    }
    matrices.value = rows
    matrixPagination.value.total = total
  } catch (error) {
    if (requestId === matrixRequestId) {
      matrixError.value = [401, 403, 404].includes(error.response?.status)
        ? t('execution.datasets.permissionDenied')
        : error.response?.data?.detail || error.message || t('execution.datasets.loadFailed')
      matrixPermissionDenied.value = [401, 403, 404].includes(error.response?.status)
    }
  } finally {
    if (requestId === matrixRequestId) matrixLoading.value = false
  }
}
const changeMatrixDataset = () => { matrixPagination.value.page = 1; loadMatrices() }
const changeMatrixPageSize = pageSize => { matrixPagination.value.pageSize = pageSize; matrixPagination.value.page = 1; loadMatrices() }
const openMatrixCreate = () => {
  editingMatrix.value = null
  matrixForm.value = { dataset: matrixDatasetFilter.value, name: '', columnsText: '[]', rowsText: '[]', enabled: true }
  matrixDialogVisible.value = true
}
const openMatrixEdit = row => {
  editingMatrix.value = row.id
  ensureDatasetOption(row.dataset)
  matrixForm.value = { dataset: row.dataset, name: row.name, columnsText: JSON.stringify(row.columns || [], null, 2), rowsText: JSON.stringify(row.rows || [], null, 2), enabled: row.enabled }
  matrixDialogVisible.value = true
}
const saveMatrix = async () => {
  let columns
  let rows
  try {
    columns = JSON.parse(matrixForm.value.columnsText || '[]')
    rows = JSON.parse(matrixForm.value.rowsText || '[]')
    if (!Array.isArray(columns) || !Array.isArray(rows)) throw new Error('Both values must be arrays')
  } catch (error) {
    ElMessage.error(error.message === 'Both values must be arrays' ? t('execution.datasets.jsonArrayRequired') : t('execution.datasets.invalidMatrixJson'))
    return
  }
  if (!matrixForm.value.dataset || !matrixForm.value.name.trim()) {
    ElMessage.warning(t('execution.datasets.matrixRequired'))
    return
  }
  matrixSaving.value = true
  matrixError.value = ''
  try {
    const payload = { dataset: matrixForm.value.dataset, name: matrixForm.value.name.trim(), columns, rows, enabled: matrixForm.value.enabled }
    if (editingMatrix.value) await updateExecutionDataMatrix(editingMatrix.value, payload)
    else await createExecutionDataMatrix(payload)
    matrixDialogVisible.value = false
    await loadMatrices()
    ElMessage.success(t('execution.datasets.matrixSaved'))
  } catch (error) {
    matrixError.value = [401, 403, 404].includes(error.response?.status)
      ? t('execution.datasets.permissionDenied')
      : error.response?.data?.detail || error.message || t('execution.datasets.saveMatrixFailed')
    matrixPermissionDenied.value = [401, 403, 404].includes(error.response?.status)
  } finally {
    matrixSaving.value = false
  }
}
const removeMatrix = async row => {
  try {
    await ElMessageBox.confirm(t('execution.datasets.deleteMatrixConfirm', { name: row.name }), t('common.confirm'), { type: 'warning' })
    await deleteExecutionDataMatrix(row.id)
    matrixPagination.value.page = 1
    await loadMatrices()
    ElMessage.success(t('execution.datasets.matrixDeleted'))
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      matrixError.value = [401, 403, 404].includes(error.response?.status)
        ? t('execution.datasets.permissionDenied')
        : error.response?.data?.detail || error.message || t('execution.datasets.deleteMatrixFailed')
      matrixPermissionDenied.value = [401, 403, 404].includes(error.response?.status)
    }
  }
}
const handleTabChange = name => {
  if (name === 'matrices') loadDatasetOptions().then(loaded => loaded && loadMatrices())
}
const load = () => Promise.all([loadDatasets(), loadPlugins(), loadDatasetOptions()])
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
.matrix-filter { margin-bottom: 8px; }
.matrix-alert { margin-bottom: 12px; }
@media (max-width: 768px) { .page-header { gap: 12px; flex-direction: column; } }
</style>
