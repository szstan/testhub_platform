<template>
  <el-card class="scene-data-panel" shadow="never">
    <template #header><strong>{{ t('appAutomation.sceneData.title') }}</strong></template>
    <el-alert v-if="!projectId" :title="t('appAutomation.sceneData.selectProject')" type="info" :closable="false" />
    <template v-else>
      <el-tabs v-model="activeTab">
        <el-tab-pane :label="t('appAutomation.sceneData.datasets')" name="datasets">
          <p>{{ t('appAutomation.sceneData.datasetHelp') }}</p>
          <div class="scene-data-form">
            <el-input v-model.trim="datasetName" :placeholder="t('appAutomation.sceneData.datasetName')" maxlength="100" />
            <el-input-number v-model="datasetVersion" :min="1" :max="2147483647" :precision="0" />
            <label class="file-label">
              {{ t('appAutomation.sceneData.chooseFile') }}
              <input type="file" accept=".csv,.xlsx" @change="selectFile" />
            </label>
            <span v-if="selectedFile">{{ selectedFile.name }}</span>
            <el-input v-model.trim="sensitiveColumns" :placeholder="t('appAutomation.sceneData.sensitiveColumns')" />
            <el-button type="primary" :loading="savingDataset" @click="uploadDataset">{{ t('appAutomation.sceneData.upload') }}</el-button>
          </div>
          <p class="hint">{{ t('appAutomation.sceneData.secretHelp') }}</p>
          <el-table v-loading="loading" :data="datasets" :empty-text="t('appAutomation.sceneData.emptyDatasets')" size="small">
            <el-table-column prop="name" :label="t('appAutomation.sceneData.name')" />
            <el-table-column prop="version" :label="t('appAutomation.sceneData.version')" width="100" />
            <el-table-column prop="row_count" :label="t('appAutomation.sceneData.rows')" width="100" />
          </el-table>
        </el-tab-pane>
        <el-tab-pane :label="t('appAutomation.sceneData.components')" name="components">
          <p>{{ t('appAutomation.sceneData.componentHelp') }}</p>
          <div class="scene-data-form">
            <el-input v-model.trim="componentName" :placeholder="t('appAutomation.sceneData.componentName')" maxlength="100" />
            <el-input v-model.trim="componentType" :placeholder="t('appAutomation.sceneData.componentType')" maxlength="50" />
            <el-input v-model.trim="componentVersion" :placeholder="t('appAutomation.sceneData.version')" maxlength="32" />
            <el-input v-model="parameterProperties" type="textarea" :rows="3" :placeholder="t('appAutomation.sceneData.parameterSchema')" />
            <el-input v-model.trim="requiredParameters" :placeholder="t('appAutomation.sceneData.requiredParameters')" />
            <el-button type="primary" :loading="savingComponent" @click="saveComponent">{{ t('appAutomation.sceneData.saveVersion') }}</el-button>
          </div>
          <p class="hint">{{ t('appAutomation.sceneData.parameterHelp') }}</p>
          <el-table v-loading="loading" :data="components" :empty-text="t('appAutomation.sceneData.emptyComponents')" size="small">
            <el-table-column prop="name" :label="t('appAutomation.sceneData.name')" />
            <el-table-column prop="type" :label="t('appAutomation.sceneData.type')" />
            <el-table-column prop="version" :label="t('appAutomation.sceneData.version')" width="100" />
            <el-table-column width="110">
              <template #default="{ row }">
                <el-button link type="primary" @click="emit('insert-component', row)">{{ t('appAutomation.sceneData.insert') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </template>
  </el-card>
</template>

<script setup>
import { ref, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useUserStore } from '@/stores/user'
import { createProjectComponent, getAllProjectComponents, getAllSceneDatasets, uploadSceneDataset } from '@/api/app-scene-data'

const props = defineProps({
  projectId: { type: [String, Number], default: null },
  getSteps: { type: Function, required: true },
})
const emit = defineEmits(['insert-component', 'components-loaded'])
const { t } = useI18n()
const userStore = useUserStore()
const activeTab = ref('datasets')
const datasets = ref([])
const components = ref([])
const loading = ref(false)
const savingDataset = ref(false)
const savingComponent = ref(false)
const datasetName = ref('')
const datasetVersion = ref(1)
const selectedFile = ref(null)
const sensitiveColumns = ref('')
const componentName = ref('')
const componentType = ref('')
const componentVersion = ref('1')
const parameterProperties = ref('{}')
const requiredParameters = ref('')
let loadGeneration = 0

const captureAccountIdentity = () => ({
  userId: userStore.user?.id,
  sessionGeneration: typeof userStore.getSessionGeneration === 'function'
    ? userStore.getSessionGeneration()
    : undefined,
})
const isAccountIdentityCurrent = identity =>
  String(userStore.user?.id) === String(identity.userId) &&
  (identity.sessionGeneration === undefined || (
    typeof userStore.getSessionGeneration === 'function' &&
    userStore.getSessionGeneration() === identity.sessionGeneration
  ))

const rowsFrom = response => {
  const body = response?.data ?? response
  if (Array.isArray(body)) return body
  if (Array.isArray(body?.results)) return body.results
  if (Array.isArray(body?.data)) return body.data
  return []
}

const load = async () => {
  const generation = ++loadGeneration
  const project = props.projectId
  const identity = captureAccountIdentity()
  const isCurrent = () => generation === loadGeneration && project === props.projectId && isAccountIdentityCurrent(identity)
  datasets.value = []
  components.value = []
  emit('components-loaded', [])
  if (!project) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    const [datasetResponse, componentResponse] = await Promise.all([
      getAllSceneDatasets(project), getAllProjectComponents(project),
    ])
    if (!isCurrent()) return
    datasets.value = rowsFrom(datasetResponse)
    components.value = rowsFrom(componentResponse)
    emit('components-loaded', components.value)
  } catch {
    if (isCurrent()) ElMessage.error(t('appAutomation.sceneData.loadFailed'))
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

const selectFile = event => { selectedFile.value = event.target.files?.[0] || null }
const commaList = text => text.split(',').map(value => value.trim()).filter(Boolean)

const uploadDataset = async () => {
  if (!props.projectId || !datasetName.value || !selectedFile.value || selectedFile.value.size > 5 * 1024 * 1024) {
    ElMessage.warning(t('appAutomation.sceneData.datasetRequired'))
    return
  }
  const project = props.projectId
  const body = new FormData()
  body.append('project', String(project))
  body.append('name', datasetName.value)
  body.append('version', String(datasetVersion.value))
  body.append('file', selectedFile.value)
  body.append('sensitive_columns', JSON.stringify(commaList(sensitiveColumns.value)))
  savingDataset.value = true
  try {
    await uploadSceneDataset(body)
    if (project !== props.projectId) return
    ElMessage.success(t('appAutomation.sceneData.datasetSaved'))
    selectedFile.value = null
    await load()
  } catch (error) {
    if (project === props.projectId) ElMessage.error(error?.response?.data?.detail || t('appAutomation.sceneData.saveFailed'))
  } finally {
    savingDataset.value = false
  }
}

const saveComponent = async () => {
  if (!props.projectId || !componentName.value || !componentType.value || !componentVersion.value) {
    ElMessage.warning(t('appAutomation.sceneData.componentRequired'))
    return
  }
  let properties
  let steps
  try {
    properties = JSON.parse(parameterProperties.value || '{}')
    if (!properties || typeof properties !== 'object' || Array.isArray(properties)) throw new Error('invalid schema')
    steps = props.getSteps()
    if (!Array.isArray(steps) || !steps.length) throw new Error('empty steps')
  } catch {
    ElMessage.error(t('appAutomation.sceneData.invalidComponent'))
    return
  }
  const project = props.projectId
  savingComponent.value = true
  try {
    await createProjectComponent({
      project, name: componentName.value, type: componentType.value, version: componentVersion.value,
      schema: { properties, required: commaList(requiredParameters.value) }, default_config: {}, steps,
    })
    if (project !== props.projectId) return
    ElMessage.success(t('appAutomation.sceneData.componentSaved'))
    await load()
  } catch (error) {
    if (project === props.projectId) ElMessage.error(error?.response?.data?.detail || t('appAutomation.sceneData.saveFailed'))
  } finally {
    savingComponent.value = false
  }
}

watch(() => [props.projectId, userStore.user], () => {
  selectedFile.value = null
  load()
}, { immediate: true })
onBeforeUnmount(() => { loadGeneration += 1 })
</script>

<style scoped>
.scene-data-panel { margin-top: 16px; }
.scene-data-form { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.scene-data-form :deep(.el-input) { width: min(100%, 230px); }
.scene-data-form :deep(.el-textarea) { width: min(100%, 360px); }
.file-label { display: flex; align-items: center; gap: 8px; }
.hint { color: var(--el-text-color-secondary); font-size: 13px; }
</style>
