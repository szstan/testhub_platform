<template>
  <div class="workbench-dashboard">
    <header class="workbench-header">
      <div class="header-copy">
        <h1>{{ t('uiAutomation.workbenchDashboard.title') }}</h1>
      </div>

      <div class="header-controls">
        <div class="project-picker">
          <label for="ui-workbench-project">{{ t('uiAutomation.workbenchDashboard.currentProject') }}</label>
          <el-select
            id="ui-workbench-project"
            v-model="projectId"
            :loading="projectsLoading"
            :disabled="projectsLoading || projects.length === 0"
            :placeholder="t('uiAutomation.workbenchDashboard.selectProject')"
            class="project-select"
          >
            <el-option
              v-for="project in projects"
              :key="project.id"
              :label="project.name"
              :value="project.id"
            />
          </el-select>
        </div>
        <el-button plain :aria-label="t('uiAutomation.workbenchDashboard.help.open')" @click="helpVisible = true">
          <el-icon><QuestionFilled /></el-icon>
          <span>{{ t('uiAutomation.workbenchDashboard.help.open') }}</span>
        </el-button>
      </div>
    </header>

    <div v-if="projectsLoading" class="project-state" role="status">
      <el-icon class="state-icon is-loading"><Loading /></el-icon>
      <span>{{ t('uiAutomation.workbenchDashboard.states.projectsLoading') }}</span>
    </div>
    <div v-else-if="projectsError" class="project-state is-error" role="alert">
      <span>{{ t('uiAutomation.workbenchDashboard.states.projectsError') }}</span>
      <el-button link type="primary" @click="reloadProjects">
        {{ t('uiAutomation.workbenchDashboard.actions.retry') }}
      </el-button>
    </div>
    <div v-else-if="invalidProject || (projects.length > 0 && !hasValidProject)" class="project-state" role="status">
      <span>{{ invalidProject ? t('uiAutomation.workbenchDashboard.states.invalidProject') : t('uiAutomation.workbenchDashboard.states.chooseProject') }}</span>
    </div>
    <div v-else-if="projects.length === 0" class="onboarding-state">
      <div>
        <h2>{{ t('uiAutomation.workbenchDashboard.states.noProjectsTitle') }}</h2>
        <p>{{ t('uiAutomation.workbenchDashboard.states.noProjectsDescription') }}</p>
      </div>
      <RouterLink :to="{ name: 'UiProjects', query: { setup: 'project' } }" class="el-button el-button--primary">
        <el-icon><Plus /></el-icon>
        <span>{{ t('uiAutomation.workbenchDashboard.actions.createProject') }}</span>
      </RouterLink>
    </div>

    <template v-if="hasValidProject">
      <section class="action-bar" :aria-label="t('uiAutomation.workbenchDashboard.actions.title')">
        <RouterLink :to="emptyCaseRoute" class="el-button el-button--primary el-button--large">
          <el-icon><component :is="canCreateFirstCase ? Plus : CaretRight" /></el-icon>
          <span>{{ t(canCreateFirstCase ? 'uiAutomation.workbenchDashboard.actions.createFirstCase' : 'uiAutomation.workbenchDashboard.actions.runCase') }}</span>
        </RouterLink>
        <RouterLink :to="projectRoute('UiSuites')" class="el-button el-button--large">
          <el-icon><VideoPlay /></el-icon>
          <span>{{ t('uiAutomation.workbenchDashboard.actions.runSuite') }}</span>
        </RouterLink>
      </section>

      <div class="workbench-grid">
        <section class="panel execution-panel" aria-labelledby="latest-executions-heading">
          <div class="panel-heading">
            <div>
              <h2 id="latest-executions-heading">{{ t('uiAutomation.workbenchDashboard.latestExecutions') }}</h2>
              <p>{{ t('uiAutomation.workbenchDashboard.latestExecutionsDescription') }}</p>
            </div>
            <el-radio-group
              v-model="executionFilter"
              size="small"
              :aria-label="t('uiAutomation.workbenchDashboard.executionFilter')"
            >
              <el-radio-button value="all">{{ t('uiAutomation.workbenchDashboard.filters.all') }}</el-radio-button>
              <el-radio-button value="failures">{{ t('uiAutomation.workbenchDashboard.filters.failures') }}</el-radio-button>
            </el-radio-group>
          </div>

          <div v-if="executionLoading" class="panel-state" role="status">
            <el-icon class="state-icon is-loading"><Loading /></el-icon>
            <span>{{ t('uiAutomation.workbenchDashboard.states.executionsLoading') }}</span>
          </div>
          <div v-else-if="executionError" class="panel-state is-error" role="alert">
            <span>{{ t('uiAutomation.workbenchDashboard.states.executionsError') }}</span>
            <el-button link type="primary" @click="loadExecutions">
              {{ t('uiAutomation.workbenchDashboard.actions.retry') }}
            </el-button>
          </div>
          <div v-else-if="executions.length === 0" class="panel-state empty-state">
            <div>
              <strong>{{ emptyExecutionTitle }}</strong>
              <p>{{ emptyExecutionDescription }}</p>
            </div>
            <RouterLink
              v-if="executionFilter === 'all'"
              :to="emptyCaseRoute"
              class="el-button el-button--primary el-button--small"
            >
              <el-icon><CaretRight /></el-icon>
              <span>{{ emptyExecutionAction }}</span>
            </RouterLink>
          </div>
          <ul v-else class="execution-list">
            <li v-for="execution in executions" :key="`${execution.kind}-${execution.id}`" class="execution-row">
              <div class="execution-main">
                <div class="execution-copy">
                  <RouterLink :to="executionRoute(execution)" class="execution-link">
                    {{ execution.name || t('uiAutomation.workbenchDashboard.unnamedExecution') }}
                  </RouterLink>
                  <span class="execution-kind">{{ kindLabel(execution.kind) }}</span>
                </div>
                <div class="execution-meta">
                  <span class="status-badge" :class="statusClass(execution.status)">
                    {{ statusLabel(execution.status) }}
                  </span>
                  <time :datetime="execution.created_at">{{ formatDate(execution.created_at) }}</time>
                </div>
              </div>
              <p
                v-if="isFailure(execution) && (execution.failure_message || hasFailedStep(execution))"
                class="failure-summary"
              >
                <span v-if="hasFailedStep(execution)" class="failed-step">
                  {{ t('uiAutomation.workbenchDashboard.failedStep', { number: execution.failed_step_number }) }}
                </span>
                <span v-if="execution.failure_message">{{ execution.failure_message }}</span>
              </p>
            </li>
          </ul>

          <div class="panel-footer">
            <RouterLink :to="projectRoute('UiExecutions')" class="text-link">
              {{ t('uiAutomation.workbenchDashboard.actions.viewAllExecutions') }}
              <el-icon><ArrowRight /></el-icon>
            </RouterLink>
            <RouterLink :to="projectRoute('UiReports')" class="text-link">
              {{ t('uiAutomation.workbenchDashboard.actions.viewSuiteReports') }}
              <el-icon><ArrowRight /></el-icon>
            </RouterLink>
          </div>
        </section>

        <section class="panel assets-panel" aria-labelledby="assets-heading">
          <div class="panel-heading">
            <h2 id="assets-heading">{{ t('uiAutomation.workbenchDashboard.assets.title') }}</h2>
          </div>
          <div v-if="assetsLoading" class="panel-state" role="status">
            <el-icon class="state-icon is-loading"><Loading /></el-icon>
            <span>{{ t('uiAutomation.workbenchDashboard.states.assetsLoading') }}</span>
          </div>
          <div v-else-if="assetError" class="panel-state is-error" role="alert">
            <span>{{ t('uiAutomation.workbenchDashboard.states.assetsError') }}</span>
            <el-button link type="primary" @click="loadAssets">
              {{ t('uiAutomation.workbenchDashboard.actions.retry') }}
            </el-button>
          </div>
          <div v-else class="asset-list">
            <RouterLink :to="projectRoute('UiTestCases')" class="asset-row">
              <span>{{ t('uiAutomation.workbenchDashboard.assets.cases') }}</span>
              <strong>{{ testCaseCount }}</strong>
              <el-icon><ArrowRight /></el-icon>
            </RouterLink>
            <RouterLink :to="projectRoute('UiSuites')" class="asset-row">
              <span>{{ t('uiAutomation.workbenchDashboard.assets.suites') }}</span>
              <strong>{{ suiteCount }}</strong>
              <el-icon><ArrowRight /></el-icon>
            </RouterLink>
            <RouterLink
              v-if="testCaseCount === 0"
              :to="createCaseRoute"
              class="asset-onboarding-link"
            >
              {{ t('uiAutomation.workbenchDashboard.actions.createFirstCase') }}
              <el-icon><ArrowRight /></el-icon>
            </RouterLink>
          </div>
        </section>
      </div>

      <details :key="validProjectId" class="activity-section" @toggle="onActivityToggle">
        <summary>
          <span>{{ t('uiAutomation.workbenchDashboard.activity.title') }}</span>
          <span class="activity-caption">{{ t('uiAutomation.workbenchDashboard.activity.crossProject') }}</span>
        </summary>
        <div class="activity-content">
          <div v-if="activityLoading" class="panel-state" role="status">
            <el-icon class="state-icon is-loading"><Loading /></el-icon>
            <span>{{ t('uiAutomation.workbenchDashboard.states.activityLoading') }}</span>
          </div>
          <div v-else-if="activityError" class="panel-state is-error" role="alert">
            <span>{{ t('uiAutomation.workbenchDashboard.states.activityError') }}</span>
            <el-button link type="primary" @click="loadActivity">
              {{ t('uiAutomation.workbenchDashboard.actions.retry') }}
            </el-button>
          </div>
          <div v-else-if="activityRecords.length === 0" class="panel-state">
            {{ t('uiAutomation.workbenchDashboard.states.noActivity') }}
          </div>
          <ul v-else class="activity-list">
            <li v-for="record in activityRecords" :key="record.id" class="activity-row">
              <p>
                <strong>{{ record.user_name || t('uiAutomation.workbenchDashboard.activity.unknownUser') }}</strong>
                <span>{{ record.operation_type_display }}</span>
                <span>{{ record.resource_type_display }}</span>
                <span v-if="record.resource_name">“{{ record.resource_name }}”</span>
              </p>
              <time :datetime="record.created_at">{{ formatDate(record.created_at) }}</time>
            </li>
          </ul>
        </div>
      </details>
    </template>

    <el-dialog
      v-model="helpVisible"
      :title="t('uiAutomation.workbenchDashboard.help.title')"
      width="min(560px, calc(100vw - 32px))"
      destroy-on-close
    >
      <div class="help-content">
        <p>{{ t('uiAutomation.workbenchDashboard.help.intro') }}</p>
        <h4>{{ t('uiAutomation.workbenchDashboard.help.browserTitle') }}</h4>
        <p>{{ t('uiAutomation.workbenchDashboard.help.browserSetup') }}</p>
        <h4>{{ t('uiAutomation.workbenchDashboard.help.exampleTitle') }}</h4>
        <p>{{ t('uiAutomation.workbenchDashboard.help.exampleDescription') }}</p>
        <ol>
          <li>{{ t('uiAutomation.workbenchDashboard.help.exampleElement') }}</li>
          <li>{{ t('uiAutomation.workbenchDashboard.help.exampleAssertion') }}</li>
          <li>{{ t('uiAutomation.workbenchDashboard.help.exampleRun') }}</li>
        </ol>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ArrowRight,
  CaretRight,
  Loading,
  Plus,
  QuestionFilled,
  VideoPlay
} from '@element-plus/icons-vue'
import { getDashboardRecentExecutions, getDashboardStats, getOperationRecords } from '@/api/ui_automation'
import { useUiProjectContext } from '@/composables/useUiProjectContext'
import { uiExecutionLocation } from '@/utils/ui-project-context.mjs'

const { t, locale } = useI18n()
const {
  projects,
  projectId,
  projectsLoading,
  projectsError,
  invalidProject,
  reloadProjects
} = useUiProjectContext()

const executionFilter = ref('all')
const executions = ref([])
const executionLoading = ref(false)
const executionError = ref(false)
const testCaseCount = ref(0)
const suiteCount = ref(0)
const assetsLoading = ref(false)
const assetError = ref(false)
const activityRecords = ref([])
const activityLoading = ref(false)
const activityError = ref(false)
const helpVisible = ref(false)

let assetsRequestGeneration = 0
let executionsRequestGeneration = 0
let activityRequestGeneration = 0

const validProjectId = projectId
const hasValidProject = computed(() => Boolean(validProjectId.value))
const canCreateFirstCase = computed(() => !assetsLoading.value && !assetError.value && testCaseCount.value === 0)
const emptyExecutionTitle = computed(() => executionFilter.value === 'failures'
  ? t('uiAutomation.workbenchDashboard.states.noFailures')
  : t('uiAutomation.workbenchDashboard.states.noExecutions'))
const emptyExecutionDescription = computed(() => executionFilter.value === 'failures'
  ? t('uiAutomation.workbenchDashboard.states.noFailuresDescription')
  : t('uiAutomation.workbenchDashboard.states.noExecutionsDescription'))
const emptyExecutionAction = computed(() => canCreateFirstCase.value
  ? t('uiAutomation.workbenchDashboard.actions.createFirstCase')
  : t('uiAutomation.workbenchDashboard.actions.openCases'))
const createCaseRoute = computed(() => projectRoute('UiTestCases', { setup: 'case' }))
const emptyCaseRoute = computed(() => canCreateFirstCase.value ? createCaseRoute.value : projectRoute('UiTestCases'))

function projectRoute(name, extraQuery = {}) {
  return { name, query: { project: String(validProjectId.value), ...extraQuery } }
}

function executionRoute(execution) {
  const name = execution.kind === 'suite' ? 'UiReports' : 'UiExecutions'
  return uiExecutionLocation(name, validProjectId.value, execution.id)
}

function unwrapResults(response) {
  const data = response?.data
  if (Array.isArray(data)) return data
  if (Array.isArray(data?.results)) return data.results
  throw new Error('Invalid UI workbench response')
}

async function loadAssets() {
  const generation = ++assetsRequestGeneration
  const requestedProject = validProjectId.value
  if (!requestedProject) {
    testCaseCount.value = 0
    suiteCount.value = 0
    assetsLoading.value = false
    assetError.value = false
    return
  }

  assetsLoading.value = true
  assetError.value = false
  try {
    const response = await getDashboardStats({ project: requestedProject })
    if (generation !== assetsRequestGeneration || validProjectId.value !== requestedProject) return
    const { test_case_count: cases, suite_entity_count: suites } = response.data
    if (![cases, suites].every(count => Number.isSafeInteger(count) && count >= 0)) {
      throw new Error('Invalid UI asset counts')
    }
    testCaseCount.value = cases
    suiteCount.value = suites
  } catch {
    if (generation !== assetsRequestGeneration || validProjectId.value !== requestedProject) return
    testCaseCount.value = 0
    suiteCount.value = 0
    assetError.value = true
  } finally {
    if (generation === assetsRequestGeneration && validProjectId.value === requestedProject) assetsLoading.value = false
  }
}

async function loadExecutions() {
  const generation = ++executionsRequestGeneration
  const requestedProject = validProjectId.value
  if (!requestedProject) {
    executions.value = []
    executionLoading.value = false
    executionError.value = false
    return
  }

  const params = { project: requestedProject }
  const requestedFilter = executionFilter.value
  if (requestedFilter === 'failures') params.status = 'failed'
  const isCurrent = () => generation === executionsRequestGeneration && validProjectId.value === requestedProject && executionFilter.value === requestedFilter
  executions.value = []
  executionLoading.value = true
  executionError.value = false
  try {
    const response = await getDashboardRecentExecutions(params)
    if (isCurrent()) executions.value = unwrapResults(response).slice(0, 5)
  } catch {
    if (isCurrent()) executionError.value = true
  } finally {
    if (isCurrent()) executionLoading.value = false
  }
}

function onActivityToggle(event) {
  if (event.currentTarget.open) void loadActivity()
}

async function loadActivity() {
  const generation = ++activityRequestGeneration
  activityLoading.value = true
  activityError.value = false
  try {
    const response = await getOperationRecords({ limit: 5 })
    if (generation !== activityRequestGeneration) return
    activityRecords.value = unwrapResults(response).slice(0, 5)
  } catch {
    if (generation !== activityRequestGeneration) return
    activityRecords.value = []
    activityError.value = true
  } finally {
    if (generation === activityRequestGeneration) activityLoading.value = false
  }
}

function isFailure(execution) {
  return execution.status === 'failed' || execution.status === 'error'
}

function hasFailedStep(execution) {
  return execution.failed_step_number !== null && execution.failed_step_number !== undefined
}

const knownStatuses = new Set(['pending', 'running', 'passed', 'failed', 'error', 'aborted'])

function statusClass(status) {
  return `status-${knownStatuses.has(status) ? status : 'unknown'}`
}

function statusLabel(status) {
  return t(`uiAutomation.workbenchDashboard.status.${knownStatuses.has(status) ? status : 'unknown'}`)
}

function kindLabel(kind) {
  return kind === 'suite'
    ? t('uiAutomation.workbenchDashboard.executionKind.suite')
    : t('uiAutomation.workbenchDashboard.executionKind.case')
}

function formatDate(value) {
  if (!value) return t('uiAutomation.workbenchDashboard.unknownDate')
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

watch(validProjectId, () => {
  activityRequestGeneration += 1
  activityRecords.value = []
  activityLoading.value = false
  activityError.value = false
  void loadAssets()
}, { immediate: true })

watch([validProjectId, executionFilter], () => {
  void loadExecutions()
}, { immediate: true })

onBeforeUnmount(() => {
  assetsRequestGeneration += 1
  executionsRequestGeneration += 1
  activityRequestGeneration += 1
})
</script>

<style scoped>
.workbench-dashboard {
  width: 100%;
  color: var(--el-text-color-primary);
}

.workbench-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 18px;
}

.header-copy h1 {
  margin: 0;
  font-size: 26px;
  line-height: 1.25;
  font-weight: 600;
}

.header-controls {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex: 0 0 auto;
}

.project-picker {
  display: grid;
  gap: 5px;
  min-width: 220px;
}

.project-picker label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.project-select {
  width: 240px;
}

.project-state,
.onboarding-state {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-blank);
}

.project-state.is-error,
.panel-state.is-error {
  color: var(--el-color-danger);
}

.onboarding-state {
  justify-content: space-between;
  padding: 20px;
}

.onboarding-state h2,
.onboarding-state p {
  margin: 0;
}

.onboarding-state h2 {
  font-size: 16px;
  font-weight: 600;
}

.onboarding-state p {
  margin-top: 5px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.project-state .el-button {
  margin-left: auto;
}

.state-icon {
  color: var(--el-color-primary);
}

.action-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 16px;
}

.action-bar .el-button--large {
  min-height: 40px;
  padding: 0 18px;
}

.workbench-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(250px, 0.85fr);
  align-items: start;
  gap: 16px;
}

.panel {
  min-width: 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-bg-color);
}

.execution-panel,
.assets-panel {
  padding: 16px 18px;
}

.panel-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  min-height: 40px;
  margin-bottom: 10px;
}

.panel-heading h2 {
  margin: 0;
  font-size: 16px;
  line-height: 1.3;
  font-weight: 600;
}

.panel-heading p {
  margin: 4px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.panel-state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  min-height: 116px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  text-align: center;
}

.empty-state {
  flex-direction: column;
  padding: 12px 0;
}

.empty-state strong {
  display: block;
  color: var(--el-text-color-primary);
  font-weight: 600;
}

.empty-state p {
  margin: 5px 0 12px;
}

.execution-list,
.activity-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.execution-row {
  min-width: 0;
  padding: 11px 0;
  border-top: 1px solid var(--el-border-color-lighter);
}

.execution-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}

.execution-copy,
.execution-meta {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 10px;
}

.execution-link {
  overflow: hidden;
  color: var(--el-text-color-primary);
  font-size: 14px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-decoration: none;
}

.execution-link:hover,
.text-link:hover,
.asset-row:hover,
.asset-onboarding-link:hover {
  color: var(--el-color-primary);
}

.execution-kind {
  flex: 0 0 auto;
  padding: 2px 6px;
  border-radius: 3px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 11px;
}

.execution-meta {
  flex: 0 0 auto;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.status-badge {
  padding: 2px 7px;
  border-radius: 3px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-weight: 500;
}

.status-passed {
  background: var(--el-color-success-light-9);
  color: var(--el-color-success-dark-2);
}

.status-failed,
.status-error {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}

.status-running {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}

.status-pending,
.status-aborted,
.status-unknown {
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
}

.failure-summary {
  display: flex;
  gap: 7px;
  margin: 6px 0 0;
  overflow-wrap: anywhere;
  color: var(--el-color-danger);
  font-size: 12px;
  line-height: 1.45;
}

.failed-step {
  flex: 0 0 auto;
  font-weight: 600;
}

.panel-footer {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px 18px;
  padding-top: 10px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.text-link,
.asset-row,
.asset-onboarding-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--el-color-primary);
  font-size: 13px;
  text-decoration: none;
}

.asset-list {
  display: grid;
}

.asset-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto 16px;
  align-items: center;
  gap: 10px;
  min-height: 60px;
  border-top: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-regular);
}

.asset-row:first-child {
  border-top: 0;
}

.asset-row strong {
  color: var(--el-text-color-primary);
  font-size: 24px;
  font-weight: 600;
}

.asset-row .el-icon {
  color: var(--el-text-color-placeholder);
}

.asset-onboarding-link {
  justify-content: space-between;
  padding: 10px 0 0;
  border-top: 1px solid var(--el-border-color-lighter);
}

.activity-section {
  margin-top: 16px;
  border-top: 1px solid var(--el-border-color);
  border-bottom: 1px solid var(--el-border-color);
}

.activity-section summary {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 44px;
  color: var(--el-text-color-primary);
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  list-style-position: inside;
}

.activity-caption {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 400;
}

.activity-content {
  padding: 0 6px 10px 20px;
}

.activity-list {
  border-top: 1px solid var(--el-border-color-lighter);
}

.activity-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 9px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-regular);
  font-size: 13px;
}

.activity-row p {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}

.activity-row time {
  flex: 0 0 auto;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.help-content h4 {
  margin: 18px 0 6px;
  font-size: 14px;
}

.help-content p {
  margin: 0;
  color: var(--el-text-color-regular);
  font-size: 14px;
  line-height: 1.55;
}

.help-content ol {
  margin-block: 8px 0;
  padding-inline-start: 22px;
  line-height: 1.65;
}

@media (max-width: 900px) {
  .workbench-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .header-controls {
    width: 100%;
    justify-content: space-between;
  }

  .workbench-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 600px) {
  .workbench-header {
    gap: 14px;
  }

  .header-copy h2 {
    font-size: 21px;
  }

  .header-controls {
    align-items: stretch;
    flex-direction: column;
  }

  .project-picker,
  .project-select {
    width: 100%;
  }

  .header-controls > .el-button {
    align-self: flex-start;
  }

  .action-bar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }

  .action-bar .el-button {
    min-width: 0;
    margin: 0;
    padding: 0 8px;
  }

  .execution-panel,
  .assets-panel {
    padding: 14px;
  }

  .panel-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 9px;
  }

  .execution-main,
  .activity-row {
    align-items: flex-start;
    flex-direction: column;
    gap: 7px;
  }

  .execution-copy {
    max-width: 100%;
  }

  .execution-meta {
    flex-wrap: wrap;
  }

  .failure-summary {
    flex-direction: column;
    gap: 2px;
  }

  .onboarding-state {
    align-items: flex-start;
    flex-direction: column;
  }

  .activity-section summary {
    flex-wrap: wrap;
    gap: 4px 9px;
    padding: 6px 0;
  }
}
</style>
