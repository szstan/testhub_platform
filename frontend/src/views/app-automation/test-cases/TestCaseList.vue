<template>
  <div class="ui-flow-case-list">
    <!-- 页面标题 -->
    <div class="page-header">
      <h3>{{ $t('appAutomation.testCase.title') }}</h3>
      <div class="header-actions">
        <el-button
          type="primary"
          size="small"
          :icon="Refresh"
          :loading="loading"
          @click="loadTestCases"
        >
          {{ $t('appAutomation.common.refresh') }}
        </el-button>
      </div>
    </div>

    <!-- 设备和应用选择 -->
    <el-card class="device-card">
      <el-form :model="form" label-width="100px" size="small">
        <el-row :gutter="16">
          <el-col :span="5">
            <el-form-item :label="$t('appAutomation.testCase.belongProject')">
              <el-select
                v-model="form.projectId"
                :placeholder="$t('appAutomation.testCase.allProjects')"
                clearable
                filterable
                style="width: 100%"
                @change="loadTestCases"
              >
                <el-option
                  v-for="proj in projectList"
                  :key="proj.id"
                  :label="proj.name"
                  :value="proj.id"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="5">
            <el-form-item :label="$t('appAutomation.testCase.selectDevice')" required>
              <el-select
                v-model="form.deviceId"
                :placeholder="$t('appAutomation.testCase.selectDevicePlaceholder')"
                filterable
                style="width: 100%"
                :loading="devicesLoading"
                :disabled="executionSubmitting"
              >
                <el-option
                  v-for="device in availableDevices"
                  :key="device.id"
                  :label="`${device.name || device.device_id} (${device.device_id})` + (device.lease_requires_token ? ` - ${$t('appAutomation.device.messages.executionTokenLeaseBadge')}` : '')"
                  :value="device.id"
                  :disabled="!canSelectAppExecutionDevice(device, userStore.user?.id)"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="5">
            <el-form-item :label="$t('appAutomation.testCase.selectApp')">
              <el-select
                v-model="form.packageId"
                :placeholder="$t('appAutomation.testCase.selectAppPlaceholder')"
                clearable
                filterable
                style="width: 100%"
                :disabled="executionSubmitting"
              >
                <el-option
                  v-for="pkg in appPackages"
                  :key="pkg.id"
                  :label="`${pkg.name} (${pkg.package_name})`"
                  :value="pkg.id"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="9">
            <el-form-item :label="$t('appAutomation.testCase.searchCase')">
              <el-input
                v-model="searchQuery"
                :placeholder="$t('appAutomation.testCase.searchCasePlaceholder')"
                clearable
                @clear="loadTestCases"
                @keyup.enter="loadTestCases"
              >
                <template #prefix>
                  <el-icon><Search /></el-icon>
                </template>
                <template #append>
                  <el-button :icon="Search" @click="loadTestCases">{{ $t('appAutomation.common.search') }}</el-button>
                </template>
              </el-input>
            </el-form-item>
          </el-col>
        </el-row>
        <el-alert
          v-if="selectedExecutionDevice?.lease_requires_token"
          class="device-lease-warning"
          type="warning"
          :closable="false"
          show-icon
          :title="$t('appAutomation.device.messages.executionTokenLeaseUnavailable')"
        />
      </el-form>
    </el-card>

    <!-- 批量操作栏 -->
    <div v-if="selectedCases.length > 0" class="batch-bar">
      <span>{{ $t('appAutomation.testCase.selectedCasesCount', { count: selectedCases.length }) }}</span>
      <el-button type="success" size="small" :disabled="executionSubmitting" @click="batchRun">
        {{ $t('appAutomation.testCase.batchRun') }}
      </el-button>
      <el-button size="small" :disabled="executionSubmitting" @click="clearSelection">
        {{ $t('appAutomation.testCase.cancelSelection') }}
      </el-button>
    </div>

    <!-- 测试用例列表 -->
    <el-table
      ref="tableRef"
      v-loading="loading"
      :data="testCases"
      style="width: 100%; margin-top: 16px"
      :empty-text="$t('appAutomation.testCase.noTestCases')"
      @selection-change="handleSelectionChange"
    >
      <el-table-column type="selection" width="50" :selectable="() => !executionSubmitting" />
      <el-table-column prop="name" :label="$t('appAutomation.testCase.caseName')" min-width="200" />
      <el-table-column :label="$t('appAutomation.testCase.sceneDescription')" min-width="250">
        <template #default="{ row }">
          {{ row.description || '-' }}
        </template>
      </el-table-column>
      <el-table-column prop="updated_at" :label="$t('appAutomation.common.updateTime')" width="180">
        <template #default="{ row }">
          {{ formatDateTime(row.updated_at) }}
        </template>
      </el-table-column>
      <el-table-column :label="$t('appAutomation.common.operation')" width="200">
        <template #default="{ row }">
          <el-button link type="success" size="small" :disabled="executionSubmitting" @click="runCase(row)">
            {{ $t('appAutomation.common.run') }}
          </el-button>
          <el-button link type="primary" size="small" :disabled="executionSubmitting" @click="openDatasetDialog(row)">
            {{ $t('appAutomation.sceneData.runWithData') }}
          </el-button>
          <el-button link type="primary" size="small" @click="editCase(row)">
            {{ $t('appAutomation.common.edit') }}
          </el-button>
          <el-button link type="danger" size="small" @click="deleteCase(row)">
            {{ $t('appAutomation.common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <el-pagination
      v-show="caseTotal > 0"
      v-model:current-page="caseCurrentPage"
      v-model:page-size="casePageSize"
      :page-sizes="[10, 20, 30, 50]"
      :total="caseTotal"
      layout="total, sizes, prev, pager, next, jumper"
      style="margin-top: 16px; text-align: right"
      @size-change="handleCaseSizeChange"
      @current-change="handleCasePageChange"
    />

    <!-- 测试执行记录 -->
    <el-card class="execution-card" style="margin-top: 20px">
      <template #header>
        <div class="card-header">
          <span>{{ $t('appAutomation.testCase.recentExecutions') }}</span>
          <div class="card-actions">
            <el-button link type="primary" @click="refreshExecutions">
              {{ $t('appAutomation.common.refresh') }}
            </el-button>
            <el-button link type="primary" @click="viewAllExecutions">
              {{ $t('appAutomation.testCase.viewAll') }}
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        :title="$t('appAutomation.common.verificationHint')"
        type="info"
        :closable="false"
        show-icon
      />
      <el-table
        v-loading="executionsLoading"
        :data="executionData.results"
        style="width: 100%"
      >
        <el-table-column prop="case_name" :label="$t('appAutomation.testCase.testCaseColumn')" width="200" />
        <el-table-column prop="device_name" :label="$t('appAutomation.testCase.device')" width="150" />
        <el-table-column prop="user_name" :label="$t('appAutomation.testCase.tester')" width="120" />
        <el-table-column prop="status" :label="$t('appAutomation.common.status')" width="100">
          <template #default="{ row }">
            <el-tag :type="getDisplayStatus(row.status, row.result).type" size="small">
              {{ getDisplayStatus(row.status, row.result).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('appAutomation.testCase.executionProgress')" width="280">
          <template #default="{ row }">
            <div class="progress-wrapper">
              <el-progress
                :percentage="calculateProgress(row)"
                :status="getProgressStatus(row)"
                :stroke-width="8"
                :show-text="false"
                style="flex: 1"
              />
              <span class="progress-text">{{ calculateProgress(row) }}%</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="started_at" :label="$t('appAutomation.testCase.startTime')" width="180">
          <template #default="{ row }">
            {{ formatDateTime(row.started_at) }}
          </template>
        </el-table-column>
        <el-table-column prop="finished_at" :label="$t('appAutomation.testCase.endTime')" width="180">
          <template #default="{ row }">
            {{ row.finished_at ? formatDateTime(row.finished_at) : '-' }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('appAutomation.common.operation')" width="150">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'completed' || row.status === 'error'"
              link
              type="primary"
              size="small"
              @click="viewReport(row)"
            >
              {{ $t('appAutomation.testCase.viewReport') }}
            </el-button>
            <el-button
              v-if="(row.status === 'pending' || row.status === 'running') && !row.stop_requested"
              link
              type="danger"
              size="small"
              :loading="stoppingExecutionIds.has(row.id)"
              :disabled="stoppingExecutionIds.has(row.id)"
              @click="stopTest(row)"
            >
              {{ $t('appAutomation.common.stop') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
    <el-dialog v-model="datasetDialogOpen" :title="$t('appAutomation.sceneData.runWithData')" width="520px"
               :close-on-click-modal="!executionSubmitting">
      <el-form label-position="top">
        <el-form-item :label="$t('appAutomation.sceneData.datasets')" required>
          <el-select v-model="selectedDatasetId" :loading="datasetLoading" style="width: 100%">
            <el-option v-for="dataset in availableDatasets" :key="dataset.id" :value="dataset.id"
                       :label="`${dataset.name} · v${dataset.version} · ${dataset.row_count}`" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('appAutomation.sceneData.runMode')">
          <el-radio-group v-model="datasetRunMode">
            <el-radio value="single">{{ $t('appAutomation.sceneData.singleRow') }}</el-radio>
            <el-radio value="all">{{ $t('appAutomation.sceneData.allRows') }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="datasetRunMode === 'single'" :label="$t('appAutomation.sceneData.rowNumber')">
          <el-input-number v-model="datasetRowNumber" :min="1" :max="Math.max(1, selectedDataset?.row_count || 1)" :precision="0" />
        </el-form-item>
        <el-alert :title="$t('appAutomation.sceneData.sequentialHint')" type="info" :closable="false" />
      </el-form>
      <template #footer>
        <el-button :disabled="executionSubmitting" @click="datasetDialogOpen = false">{{ $t('appAutomation.common.cancel') }}</el-button>
        <el-button type="primary" :loading="executionSubmitting" :disabled="!selectedDatasetId" @click="runDatasetCase">
          {{ $t('appAutomation.common.run') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  getTestCaseList,
  deleteTestCase as apiDeleteTestCase,
  executeTestCase as apiExecuteTestCase,
  getExecutionList,
  getExecutionDetail,
  stopExecution as apiStopExecution,
  getPackageList,
  getAppProjects,
  getWsStatus,
  getAppDevice,
  batchExecuteTestCases,
} from '@/api/app-automation'
import { getAllAppDevices } from '@/api/app-device-options'
import { getExecutionStatusType, getExecutionStatusText, getDisplayStatus, formatDateTime } from '@/utils/app-automation-helpers'
import { openAppExecutionReport } from '@/utils/app-automation-report.mjs'
import { buildWebSocketUrl, issueWebSocketTicket } from '@/utils/websocket'
import {
  canSelectAppExecutionDevice,
  getAppExecutionErrorMessage,
  normalizeAppDevice,
  normalizeAppDeviceList,
  prepareAppDeviceForExecution,
} from '@/utils/app-automation-device-execution.mjs'
import { getAllSceneDatasets, executeSceneDataset } from '@/api/app-scene-data'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()
const { t } = useI18n()

// 响应式数据
const loading = ref(false)
const devicesLoading = ref(false)
const executionsLoading = ref(false)
const executionSubmitting = ref(false)
const stoppingExecutionIds = ref(new Set())
const availableDevices = ref([])
const appPackages = ref([])
const searchQuery = ref('')
const datasetDialogOpen = ref(false)
const datasetLoading = ref(false)
const datasetLoadGeneration = ref(0)
const availableDatasets = ref([])
const selectedDatasetId = ref(null)
const datasetRunMode = ref('single')
const datasetRowNumber = ref(1)
const datasetRunContext = ref(null)
const selectedDataset = computed(() => availableDatasets.value.find(item => String(item.id) === String(selectedDatasetId.value)))

const projectList = ref([])
const form = ref({
  projectId: null,
  deviceId: null,
  packageId: null
})
const selectedExecutionDevice = computed(() =>
  availableDevices.value.find(device => String(device.id) === String(form.value.deviceId))
)

// 用例列表数据
const testCases = ref([])
const caseCurrentPage = ref(1)
const casePageSize = ref(20)
const caseTotal = ref(0)

// 批量选择
const tableRef = ref(null)
const selectedCases = ref([])

// 执行记录数据
const executionData = ref({
  count: 0,
  results: []
})
const websockets = ref({})
const websocketConnecting = new Set()
let unmounted = false
const lastStatusMessages = ref({})

// 定时刷新执行记录
let refreshTimer = null

// 加载项目列表
const loadProjectList = async () => {
  try {
    const res = await getAppProjects({ page_size: 100 })
    projectList.value = res.data.results || res.data || []
  } catch { /* ignore */ }
}

// 加载设备列表
const loadDevices = async () => {
  devicesLoading.value = true
  try {
    availableDevices.value = normalizeAppDeviceList(await getAllAppDevices())
  } catch {
    availableDevices.value = []
  } finally {
    devicesLoading.value = false
  }
}

const refreshExecutionDevice = async deviceId => {
  const res = await getAppDevice(deviceId)
  return normalizeAppDevice(res.data)
}

const updateExecutionDevice = device => {
  if (!device?.id) return
  const index = availableDevices.value.findIndex(item => String(item.id) === String(device.id))
  if (index >= 0) availableDevices.value[index] = device
  else availableDevices.value.push(device)
}

const captureExecutionIdentity = () => ({
  userId: userStore.user?.id,
  sessionGeneration: typeof userStore.getSessionGeneration === 'function'
    ? userStore.getSessionGeneration()
    : undefined,
})

const isExecutionIdentityCurrent = identity =>
  String(userStore.user?.id) === String(identity.userId) &&
  (identity.sessionGeneration === undefined || (
    typeof userStore.getSessionGeneration === 'function' &&
    userStore.getSessionGeneration() === identity.sessionGeneration
  ))

const verifyExecutionUser = identity => {
  if (!isExecutionIdentityCurrent(identity)) {
    throw new Error(t('appAutomation.device.messages.executionUserChanged'))
  }
}

const loadPackages = async () => {
  try {
    const res = await getPackageList({ page_size: 200 })
    const data = res.data
    if (data.success !== undefined) {
      appPackages.value = data.data?.results || data.data || []
    } else {
      appPackages.value = data.results || data || []
    }
  } catch (error) {
    console.error('加载应用包名失败')
    appPackages.value = []
  }
}

// 加载测试用例列表
const loadTestCases = async () => {
  loading.value = true
  try {
    const params = {
      page: caseCurrentPage.value,
      page_size: casePageSize.value,
      search: searchQuery.value
    }
    if (form.value.projectId) params.project = form.value.projectId
    const res = await getTestCaseList(params)
    const data = res.data
    
    if (data.success !== undefined) {
      testCases.value = data.data?.results || data.data || []
      caseTotal.value = data.data?.count || 0
    } else {
      testCases.value = data.results || data || []
      caseTotal.value = data.count || 0
    }
  } catch (error) {
    console.error('加载测试用例失败')
    testCases.value = []
    caseTotal.value = 0
  } finally {
    loading.value = false
  }
}

// 加载执行记录
const loadExecutions = async () => {
  executionsLoading.value = true
  try {
    const params = {
      page: 1,
      page_size: 5,
      ordering: '-start_time'
    }
    const res = await getExecutionList(params)
    const data = res.data
    
    if (data.success !== undefined) {
      executionData.value = {
        count: data.data?.count || 0,
        results: data.data?.results || data.data || []
      }
    } else {
      executionData.value = {
        count: data.count || 0,
        results: data.results || data || []
      }
    }

    executionData.value.results.forEach(execution => {
      if ((execution.status === 'pending' || execution.status === 'running') && execution.id) {
        trackExecution(execution.id)
      }
    })
  } catch (error) {
    console.error('加载执行记录失败')
    executionData.value = { count: 0, results: [] }
  } finally {
    executionsLoading.value = false
  }
}

// 刷新执行记录
const refreshExecutions = () => {
  loadExecutions()
}

const viewAllExecutions = () => {
  router.push({ path: '/app-automation/executions' })
}

const viewReport = async (execution) => {
  const result = await openAppExecutionReport(execution?.id, { fetchExecution: getExecutionDetail })
  if (!result.opened) {
    const key = result.reason === 'blocked' ? 'reportPopupBlocked' : result.reason === 'failed' ? 'reportOpenFailed' : 'reportUnavailable'
    ElMessage.warning(t(`appAutomation.common.${key}`))
  }
}

// 停止测试
const stopTest = async (execution) => {
  if (!execution?.id || !['pending', 'running'].includes(execution.status) || execution.stop_requested) return
  if (stoppingExecutionIds.value.has(execution.id)) return
  stoppingExecutionIds.value.add(execution.id)
  try {
    await ElMessageBox.confirm(
      t('appAutomation.testCase.stopConfirm'),
      t('appAutomation.testCase.confirmStop'),
      {
        confirmButtonText: t('appAutomation.common.confirm'),
        cancelButtonText: t('appAutomation.common.cancel'),
        type: 'warning'
      }
    )

    const res = await apiStopExecution(execution.id)
    if (res.data.success) {
      ElMessage.success(t(res.data.stopping
        ? 'appAutomation.testCase.stoppingExecution'
        : 'appAutomation.testCase.stoppedExecution'))
      loadExecutions()
    } else {
      ElMessage.error(res.data.message || t('appAutomation.testCase.stopFailed'))
    }
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error(getAppExecutionErrorMessage(error, t('appAutomation.testCase.stopFailed')))
    }
  } finally {
    stoppingExecutionIds.value.delete(execution.id)
  }
}

// Dataset execution uses the same device lease and account-switch fences as ordinary execution.
const openDatasetDialog = async (testCase) => {
  if (executionSubmitting.value) return
  const identity = captureExecutionIdentity()
  const deviceId = form.value.deviceId
  const projectId = typeof testCase.project === 'object' ? testCase.project?.id : testCase.project
  if (!deviceId || !projectId) {
    ElMessage.warning(t(!deviceId ? 'appAutomation.testCase.pleaseSelectDevice' : 'appAutomation.sceneData.selectProject'))
    return
  }
  datasetRunContext.value = { caseId: testCase.id, projectId, deviceId, identity,
    packageName: appPackages.value.find(pkg => pkg.id === form.value.packageId)?.package_name }
  datasetDialogOpen.value = true
  const generation = ++datasetLoadGeneration.value
  datasetLoading.value = true
  availableDatasets.value = []
  selectedDatasetId.value = null
  datasetRunMode.value = 'single'
  datasetRowNumber.value = 1
  try {
    const datasets = await getAllSceneDatasets(projectId)
    const context = datasetRunContext.value
    if (generation !== datasetLoadGeneration.value || !isExecutionIdentityCurrent(identity) ||
        context?.caseId !== testCase.id || String(context?.projectId) !== String(projectId)) return
    if (!Array.isArray(datasets)) throw new Error('Invalid scene dataset list')
    availableDatasets.value = datasets
  } catch {
    const context = datasetRunContext.value
    if (generation === datasetLoadGeneration.value && isExecutionIdentityCurrent(identity) &&
        context?.caseId === testCase.id && String(context?.projectId) === String(projectId)) {
      availableDatasets.value = []
      ElMessage.error(t('appAutomation.sceneData.loadFailed'))
    }
  } finally {
    if (generation === datasetLoadGeneration.value) datasetLoading.value = false
  }
}

const runDatasetCase = async () => {
  const context = datasetRunContext.value
  if (executionSubmitting.value || !context || !selectedDatasetId.value) return
  if (!isExecutionIdentityCurrent(context.identity) || String(form.value.deviceId) !== String(context.deviceId)) {
    ElMessage.error(t('appAutomation.device.messages.executionUserChanged'))
    datasetDialogOpen.value = false
    return
  }
  executionSubmitting.value = true
  try {
    const device = await prepareAppDeviceForExecution({
      selectedId: context.deviceId, userId: context.identity.userId,
      sessionGeneration: context.identity.sessionGeneration,
      getCurrentUserId: () => userStore.user?.id,
      getCurrentSessionGeneration: () => userStore.getSessionGeneration?.(),
      getDevice: refreshExecutionDevice, onDeviceRefreshed: updateExecutionDevice, t,
    })
    verifyExecutionUser(context.identity)
    const payload = {
      dataset_id: selectedDatasetId.value, device_id: device.device_id, auto_lock: true,
      expected_user_id: context.identity.userId, mode: datasetRunMode.value,
      row_index: datasetRowNumber.value - 1,
    }
    if (context.packageName) payload.package_name = context.packageName
    const response = await executeSceneDataset(context.caseId, payload)
    verifyExecutionUser(context.identity)
    if (response?.data?.success === false) throw new Error('dataset execution rejected')
    const ids = response?.data?.data?.execution_ids || []
    if (ids.length) trackExecution(ids[0])
    datasetDialogOpen.value = false
    ElMessage.success(t('appAutomation.testCase.submitted'))
    await loadExecutions()
  } catch (error) {
    const message = isExecutionIdentityCurrent(context.identity)
      ? getAppExecutionErrorMessage(error, t('appAutomation.testCase.unknownError'))
      : t('appAutomation.device.messages.executionUserChanged')
    ElMessage.error(t('appAutomation.testCase.executionFailed', { message }))
  } finally {
    executionSubmitting.value = false
  }
}

// 运行测试用例
const runCase = async (testCase) => {
  if (executionSubmitting.value) return
  const selectedDeviceId = form.value.deviceId
  const executionIdentity = captureExecutionIdentity()
  const executionUserId = executionIdentity.userId
  const selectedCaseId = testCase.id
  const selectedPackageName = appPackages.value.find(pkg => pkg.id === form.value.packageId)?.package_name
  if (!selectedDeviceId) {
    ElMessage.warning(t('appAutomation.testCase.pleaseSelectDevice'))
    return
  }

  executionSubmitting.value = true
  try {
    const device = await prepareAppDeviceForExecution({
      selectedId: selectedDeviceId,
      userId: executionUserId,
      sessionGeneration: executionIdentity.sessionGeneration,
      getCurrentUserId: () => userStore.user?.id,
      getCurrentSessionGeneration: () => userStore.getSessionGeneration?.(),
      getDevice: refreshExecutionDevice,
      onDeviceRefreshed: updateExecutionDevice,
      t,
    })
    verifyExecutionUser(executionIdentity)
    const params = {
      device_id: device.device_id,
      auto_lock: true,
      expected_user_id: executionUserId,
    }
    if (selectedPackageName) params.package_name = selectedPackageName

    const res = await apiExecuteTestCase(selectedCaseId, params)
    verifyExecutionUser(executionIdentity)
    const data = res.data || {}
    const result = data.data || data
    const executionId = data.execution?.id || data.execution_id || result.execution?.id || result.execution_id

    if (data.success === false || result.success === false) {
      ElMessage.error(t('appAutomation.testCase.executionFailed', { message: data.message || result.message || t('appAutomation.testCase.unknownError') }))
    } else if (data.success || result.success || executionId) {
      updateExecutionDevice(data.device)
      ElMessage.success(t('appAutomation.testCase.submitted'))
      if (executionId) {
        trackExecution(executionId)
        checkExecutionStatus(executionId)
      }
      setTimeout(() => {
        if (isExecutionIdentityCurrent(executionIdentity)) loadExecutions()
      }, 1000)
    } else {
      ElMessage.error(t('appAutomation.testCase.executionFailed', { message: data.message || t('appAutomation.testCase.unknownError') }))
    }
  } catch (error) {
    const message = isExecutionIdentityCurrent(executionIdentity)
      ? getAppExecutionErrorMessage(error, t('appAutomation.testCase.unknownError'))
      : t('appAutomation.device.messages.executionUserChanged')
    ElMessage.error(t('appAutomation.testCase.executionFailed', { message }))
  } finally {
    executionSubmitting.value = false
  }
}

const checkExecutionStatus = (executionId) => {
  setTimeout(async () => {
    try {
      const res = await getExecutionDetail(executionId)
      const data = res.data
      const status = data.status || data.data?.status
      if (status === 'pending') {
        ElMessage.warning(t('appAutomation.testCase.taskNotStarted'))
      }
    } catch (error) {
      console.error('检查执行状态失败')
    }
  }, 3000)
}

// 编辑测试用例
const editCase = (testCase) => {
  router.push({
    path: '/app-automation/scene-builder',
    query: { case_id: testCase.id }
  })
}

// 删除测试用例
const deleteCase = async (testCase) => {
  try {
    await ElMessageBox.confirm(
      t('appAutomation.testCase.deleteConfirm', { name: testCase.name }),
      t('appAutomation.common.confirmDelete'),
      {
        confirmButtonText: t('appAutomation.common.confirm'),
        cancelButtonText: t('appAutomation.common.cancel'),
        type: 'warning'
      }
    )

    await apiDeleteTestCase(testCase.id)
    ElMessage.success(t('appAutomation.common.deleteSuccess'))
    loadTestCases()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error(t('appAutomation.testCase.deleteFailedMsg', { message: error.message || t('appAutomation.testCase.unknownError') }))
    }
  }
}

// 查看测试报告

const updateExecutionData = (updates) => {
  if (!updates || !updates.execution_id) {
    return
  }
  const target = executionData.value.results.find(item => item.id === updates.execution_id)
  if (!target) {
    loadExecutions()
    return
  }
  if (updates.status) target.status = updates.status
  if (updates.result !== undefined) target.result = updates.result
  if (updates.progress !== null && updates.progress !== undefined) target.progress = updates.progress
  if (updates.report_path !== undefined) target.report_path = updates.report_path
  if (updates.finished_at) target.finished_at = updates.finished_at
}

// ===== 执行状态推送：WebSocket 模式 / 轮询模式（由 ws_status 接口决定） =====
const wsDisabled = ref(false)
const pollingTimers = ref({})
const wsRetryCount = ref({})  // WebSocket 重试计数
const WS_MAX_RETRY = 3       // 最大重试次数

// --- 轮询模式：每 3 秒查一次执行状态 ---
const startPolling = (executionId) => {
  if (pollingTimers.value[executionId]) return
  pollingTimers.value[executionId] = setInterval(async () => {
    try {
      const res = await getExecutionDetail(executionId)
      if (res.data) {
        updateExecutionData({
          execution_id: res.data.id,
          status: res.data.status,
          result: res.data.result,
          progress: res.data.progress,
          report_path: res.data.report_path,
          finished_at: res.data.finished_at,
        })
        if (['completed', 'error', 'stopped'].includes(res.data.status)) {
          stopPolling(executionId)
          if (res.data.result === 'passed') ElMessage.success(t('appAutomation.testCase.executionPassed'))
          else if (res.data.result === 'failed') ElMessage.error(t('appAutomation.testCase.caseFailed'))
          else if (res.data.status === 'error') ElMessage.error(t('appAutomation.testCase.executionException'))
        }
      }
    } catch (e) {
      console.error('轮询执行状态失败')
    }
  }, 3000)
}

const stopPolling = (executionId) => {
  if (pollingTimers.value[executionId]) {
    clearInterval(pollingTimers.value[executionId])
    delete pollingTimers.value[executionId]
  }
}

const stopAllPolling = () => {
  Object.keys(pollingTimers.value).forEach(id => stopPolling(id))
}

const scheduleWebSocketRetry = (executionId) => {
  const retries = (wsRetryCount.value[executionId] || 0) + 1
  wsRetryCount.value[executionId] = retries
  if (retries <= WS_MAX_RETRY) {
    console.warn(`WebSocket 连接异常 (${retries}/${WS_MAX_RETRY})，${retries}秒后重试`)
    setTimeout(() => {
      const target = executionData.value.results.find(e => e.id === executionId)
      if (!unmounted && target && ['pending', 'running'].includes(target.status)) {
        connectWebSocket(executionId)
      }
    }, retries * 1000)
  } else {
    console.warn(`WebSocket 重试超限，execution_id=${executionId} 切换为轮询`)
    delete wsRetryCount.value[executionId]
    startPolling(executionId)
  }
}

const connectWebSocket = async (executionId) => {
  if (websockets.value[executionId] || websocketConnecting.has(executionId)) return
  websocketConnecting.add(executionId)

  let ticket
  try {
    const response = await issueWebSocketTicket('app-automation', executionId)
    if (!response.ticket) throw new Error('WebSocket ticket missing')
    ticket = response.ticket
  } catch (error) {
    websocketConnecting.delete(executionId)
    scheduleWebSocketRetry(executionId)
    return
  }

  websocketConnecting.delete(executionId)
  if (unmounted) return
  const wsUrl = buildWebSocketUrl(
    `/ws/app-automation/executions/${executionId}/`,
    ticket,
  )
  let ws
  try {
    ws = new WebSocket(wsUrl)
  } catch (error) {
    scheduleWebSocketRetry(executionId)
    return
  }
  websockets.value[executionId] = ws

  ws.onopen = () => {
    wsRetryCount.value[executionId] = 0
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      updateExecutionData(data)
      if (data.status && lastStatusMessages.value[executionId] !== data.status) {
        lastStatusMessages.value[executionId] = data.status
        if (data.result === 'passed') ElMessage.success(t('appAutomation.testCase.executionPassed'))
        else if (data.result === 'failed') ElMessage.error(t('appAutomation.testCase.caseFailed'))
        else if (data.status === 'error') ElMessage.error(t('appAutomation.testCase.executionException'))
      }
      if (['completed', 'error', 'stopped'].includes(data.status)) {
        closeWebSocket(executionId)
      }
    } catch (error) {
      console.error('处理 WebSocket 消息失败')
    }
  }

  ws.onclose = () => {
    delete websockets.value[executionId]
  }

  ws.onerror = () => {
    closeWebSocket(executionId)
    scheduleWebSocketRetry(executionId)
  }
}

// --- 统一入口：根据模式选择推送方式 ---
const trackExecution = (executionId) => {
  if (wsDisabled.value) {
    startPolling(executionId)
  } else {
    connectWebSocket(executionId)
  }
}

const closeWebSocket = (executionId) => {
  const ws = websockets.value[executionId]
  if (ws) {
    ws.close()
    delete websockets.value[executionId]
  }
}

const closeAllWebSockets = () => {
  Object.keys(websockets.value).forEach(id => closeWebSocket(id))
}

// 批量选择与执行
const handleSelectionChange = (selection) => {
  selectedCases.value = selection
}

const clearSelection = () => {
  tableRef.value?.clearSelection()
  selectedCases.value = []
}

const batchRun = async () => {
  if (executionSubmitting.value) return
  const selectedDeviceId = form.value.deviceId
  const executionIdentity = captureExecutionIdentity()
  const executionUserId = executionIdentity.userId
  const selectedCaseIds = selectedCases.value.map(testCase => testCase.id)
  const selectedPackageName = appPackages.value.find(pkg => pkg.id === form.value.packageId)?.package_name
  if (!selectedDeviceId) {
    ElMessage.warning(t('appAutomation.testCase.pleaseSelectDevice'))
    return
  }
  if (selectedCaseIds.length === 0) {
    ElMessage.warning(t('appAutomation.testCase.pleaseSelectAtLeastOneCase'))
    return
  }

  const submittedSelectionIsCurrent = () =>
    selectedCases.value.length === selectedCaseIds.length &&
    selectedCases.value.every((testCase, index) => String(testCase.id) === String(selectedCaseIds[index]))

  executionSubmitting.value = true
  try {
    await ElMessageBox.confirm(
      t('appAutomation.testCase.batchRunConfirm', { count: selectedCaseIds.length }),
      t('appAutomation.testCase.confirmBatchRun'),
      { confirmButtonText: t('appAutomation.common.execute'), cancelButtonText: t('appAutomation.common.cancel'), type: 'info' }
    )
    verifyExecutionUser(executionIdentity)

    const device = await prepareAppDeviceForExecution({
      selectedId: selectedDeviceId,
      userId: executionUserId,
      sessionGeneration: executionIdentity.sessionGeneration,
      getCurrentUserId: () => userStore.user?.id,
      getCurrentSessionGeneration: () => userStore.getSessionGeneration?.(),
      getDevice: refreshExecutionDevice,
      onDeviceRefreshed: updateExecutionDevice,
      t,
    })
    verifyExecutionUser(executionIdentity)

    const params = {
      case_ids: selectedCaseIds,
      device_id: device.device_id,
      auto_lock: true,
      expected_user_id: executionUserId,
    }
    if (selectedPackageName) params.package_name = selectedPackageName

    const res = await batchExecuteTestCases(params)
    verifyExecutionUser(executionIdentity)
    const data = res.data || {}
    const result = data.data || {}
    if (data.success === false || (!data.success && !Array.isArray(result.execution_ids))) {
      throw new Error(data.message || t('appAutomation.testCase.unknownError'))
    }

    updateExecutionDevice(data.device)
    const submittedCount = result.test_case_count ?? selectedCaseIds.length
    ElMessage.success(t('appAutomation.testCase.submittedCount', { count: submittedCount }))
    if (submittedSelectionIsCurrent()) clearSelection()
    setTimeout(() => {
      if (isExecutionIdentityCurrent(executionIdentity)) loadExecutions()
    }, 1500)
  } catch (error) {
    if (error !== 'cancel') {
      const message = isExecutionIdentityCurrent(executionIdentity)
        ? getAppExecutionErrorMessage(error, t('appAutomation.testCase.unknownError'))
        : t('appAutomation.device.messages.executionUserChanged')
      ElMessage.error(t('appAutomation.testCase.executionFailed', { message }))
    }
  } finally {
    executionSubmitting.value = false
  }
}

// 分页处理
const handleCaseSizeChange = () => {
  caseCurrentPage.value = 1  // 切换每页条数时回到第1页
  loadTestCases()
}

const handleCasePageChange = () => {
  loadTestCases()
}


// 计算执行进度
const calculateProgress = (execution) => {
  if (execution.status === 'completed') return 100
  if (execution.status === 'error' || execution.status === 'stopped') return execution.progress || 0
  if (execution.status === 'running') return execution.progress || 0
  return 0
}

// 获取进度条状态（基于测试结果）
const getProgressStatus = (row) => {
  if (row.status === 'completed') {
    if (row.result === 'failed') return 'exception'
    return row.result === 'passed' ? 'success' : undefined
  }
  if (row.status === 'error') return 'exception'
  return undefined
}

// formatDateTime 已从 app-automation-helpers 导入

// 组件挂载
onMounted(async () => {
  // 先检测 WebSocket 是否可用
  try {
    const res = await getWsStatus()
    wsDisabled.value = !(res.data?.websocket)
  } catch {
    wsDisabled.value = true
  }
  if (wsDisabled.value) {
    console.info('WebSocket 不可用，将使用轮询模式')
  }

  loadProjectList()
  loadDevices()
  loadPackages()
  loadTestCases()
  loadExecutions()
  
  // WebSocket 模式下，每10秒刷新执行列表（补充 WS 推送）
  // 轮询模式下不需要（trackExecution 已有 3 秒轮询）
  if (!wsDisabled.value) {
    refreshTimer = setInterval(() => {
      const hasRunning = executionData.value.results.some(e => e.status === 'running')
      if (hasRunning) {
        loadExecutions()
      }
    }, 10000)
  }
})

// 组件卸载
onBeforeUnmount(() => {
  unmounted = true
  datasetLoadGeneration.value += 1
  if (refreshTimer) {
    clearInterval(refreshTimer)
  }
  closeAllWebSockets()
  stopAllPolling()
})
</script>

<style scoped lang="scss">
.ui-flow-case-list {
  padding: 0;
}

.batch-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  margin-top: 12px;
  background: #ecf5ff;
  border: 1px solid #b3d8ff;
  border-radius: 4px;
  font-size: 14px;

  strong {
    color: #409eff;
  }
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  background: white;
  padding: 16px 20px;
  border-radius: 4px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);

  h3 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: #303133;
  }

  .header-actions {
    display: flex;
    gap: 12px;
  }
}

.device-card {
  margin-bottom: 16px;

  :deep(.el-card__body) {
    padding: 20px;
  }

  :deep(.el-form-item) {
    margin-bottom: 0;
  }
}

.progress-wrapper {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 8px;
  background-color: #f5f7fa;
  border-radius: 4px;

  .progress-text {
    min-width: 45px;
    text-align: right;
    font-size: 12px;
    color: #606266;
    font-weight: 500;
  }
}

.execution-card {
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .card-actions {
    display: flex;
    gap: 12px;
  }

  :deep(.el-card__header) {
    padding: 16px 20px;
    background-color: #fafafa;
    border-bottom: 1px solid #e8e8e8;
  }

  :deep(.el-card__body) {
    padding: 20px;
  }
}

// 表格样式优化
:deep(.el-table) {
  .el-table__header {
    th {
      background-color: #fafafa;
      color: #606266;
      font-weight: 600;
    }
  }

  .el-table__body {
    tr:hover {
      background-color: #f5f7fa;
    }
  }
}

// 响应式设计
@media screen and (max-width: 1366px) {
  .page-header h3 {
    font-size: 16px;
  }

  .device-card :deep(.el-form-item__label) {
    font-size: 13px;
  }
}
</style>
