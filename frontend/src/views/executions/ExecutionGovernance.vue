<template>
  <div class="governance-page">
    <div class="page-header">
      <div>
        <h1>{{ t('execution.governance.title') }}</h1>
        <p>{{ t('execution.governance.subtitle') }}</p>
      </div>
      <div class="header-actions">
        <el-select v-if="activeTab !== 'gates'" v-model="projectFilter" clearable filterable :placeholder="t('execution.governance.allProjects')" @change="applyProjectFilter">
          <el-option v-for="project in projects" :key="project.id" :label="`${project.name} (#${project.id})`" :value="project.id" />
        </el-select>
        <el-button :loading="loadingActive" @click="reloadActive">{{ t('common.refresh') }}</el-button>
      </div>
    </div>

    <el-alert v-if="pageError" :title="pageError" :type="permissionDenied ? 'warning' : 'error'" show-icon :closable="false" class="page-alert">
      <template #default><el-button v-if="!permissionDenied" link type="primary" @click="reloadActive">{{ t('execution.governance.retryLoad') }}</el-button></template>
    </el-alert>

    <el-tabs v-model="activeTab" @tab-change="reloadActive">
      <el-tab-pane :label="t('execution.governance.retryPolicies')" name="retry">
        <div class="section-toolbar">
          <span>{{ t('execution.governance.retryHint') }}</span>
          <el-button type="primary" :disabled="permissionDenied || !projects.length" @click="openPolicyCreate">{{ t('execution.governance.addPolicy') }}</el-button>
        </div>
        <el-table :data="retryPolicies" v-loading="retryLoading" row-key="id">
          <el-table-column prop="name" :label="t('execution.governance.name')" min-width="150" />
          <el-table-column prop="project" :label="t('execution.governance.project')" width="95" />
          <el-table-column prop="profile" :label="t('execution.governance.profile')" width="95" />
          <el-table-column prop="case_key" :label="t('execution.governance.caseKey')" min-width="145" />
          <el-table-column prop="max_attempts" :label="t('execution.governance.maxAttempts')" width="110" />
          <el-table-column prop="backoff_seconds" :label="t('execution.governance.backoff')" width="100" />
          <el-table-column :label="t('execution.governance.enabled')" width="100"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? t('execution.governance.on') : t('execution.governance.off') }}</el-tag></template></el-table-column>
          <el-table-column :label="t('execution.governance.actions')" width="145" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" :disabled="permissionDenied" @click="openPolicyEdit(row)">{{ t('common.edit') }}</el-button>
              <el-button link type="danger" :disabled="permissionDenied" @click="removePolicy(row)">{{ t('common.delete') }}</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty :description="retryLoading ? '' : t('execution.governance.noPolicies')" /></template>
        </el-table>
        <el-pagination v-model:current-page="retryPage.page" v-model:page-size="retryPage.pageSize" :page-sizes="[10, 20, 50, 100]" :total="retryTotal" :disabled="retryLoading" layout="total, sizes, prev, pager, next, jumper" class="pagination" @current-change="loadRetryPolicies" @size-change="changeRetryPageSize" />
      </el-tab-pane>

      <el-tab-pane :label="t('execution.governance.notifications')" name="notifications">
        <div class="section-toolbar">
          <span>{{ t('execution.governance.notificationHint') }}</span>
          <el-button type="primary" :disabled="permissionDenied || !projects.length || !channels.length" @click="openSubscriptionCreate">{{ t('execution.governance.addSubscription') }}</el-button>
        </div>
        <el-table :data="subscriptions" v-loading="subscriptionLoading" row-key="id">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="project" :label="t('execution.governance.project')" width="95" />
          <el-table-column :label="t('execution.governance.channel')" min-width="160"><template #default="{ row }">{{ channelName(row.channel) }}</template></el-table-column>
          <el-table-column prop="event_type" :label="t('execution.governance.eventType')" min-width="180" />
          <el-table-column :label="t('execution.governance.enabled')" width="100"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? t('execution.governance.on') : t('execution.governance.off') }}</el-tag></template></el-table-column>
          <el-table-column :label="t('execution.governance.actions')" width="145" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" :disabled="permissionDenied" @click="openSubscriptionEdit(row)">{{ t('common.edit') }}</el-button>
              <el-button link :type="row.enabled ? 'warning' : 'success'" :disabled="permissionDenied" @click="toggleSubscription(row)">{{ row.enabled ? t('execution.governance.disable') : t('execution.governance.enable') }}</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty :description="subscriptionLoading ? '' : t('execution.governance.noSubscriptions')" /></template>
        </el-table>
        <el-pagination v-model:current-page="subscriptionPage.page" v-model:page-size="subscriptionPage.pageSize" :page-sizes="[10, 20, 50, 100]" :total="subscriptionTotal" :disabled="subscriptionLoading" layout="total, sizes, prev, pager, next, jumper" class="pagination" @current-change="loadSubscriptions" @size-change="changeSubscriptionPageSize" />
        <el-divider />
        <div class="section-heading">
          <div>
            <h2>{{ t('execution.governance.deliveryHistory') }}</h2>
            <p>{{ t('execution.governance.deliveryHint') }}</p>
          </div>
          <el-select v-model="deliveryStatus" clearable :placeholder="t('execution.governance.allStatuses')" @change="resetDeliveryPage">
            <el-option v-for="status in deliveryStatuses" :key="status" :label="t(`execution.governance.statuses.${status}`)" :value="status" />
          </el-select>
        </div>
        <el-table :data="notificationEvents" v-loading="eventLoading" row-key="id">
          <el-table-column type="expand"><template #default="{ row }"><pre class="json-detail">{{ formatJson({ payload: row.payload, error: row.error_message }) }}</pre></template></el-table-column>
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="project" :label="t('execution.governance.project')" width="95" />
          <el-table-column prop="event_type" :label="t('execution.governance.eventType')" min-width="170" />
          <el-table-column prop="status" :label="t('execution.governance.status')" width="130"><template #default="{ row }">{{ t(`execution.governance.statuses.${row.status}`) }}</template></el-table-column>
          <el-table-column prop="delivery_attempts" :label="t('execution.governance.attempts')" width="95" />
          <el-table-column prop="created_at" :label="t('execution.governance.createdAt')" min-width="180" />
          <el-table-column prop="sent_at" :label="t('execution.governance.sentAt')" min-width="180" />
          <template #empty><el-empty :description="eventLoading ? '' : t('execution.governance.noDeliveries')" /></template>
        </el-table>
        <el-pagination v-model:current-page="eventPage.page" v-model:page-size="eventPage.pageSize" :page-sizes="[10, 20, 50, 100]" :total="eventTotal" :disabled="eventLoading" layout="total, sizes, prev, pager, next, jumper" class="pagination" @current-change="loadNotificationEvents" @size-change="changeEventPageSize" />
      </el-tab-pane>

      <el-tab-pane :label="t('execution.governance.aiSuggestions')" name="suggestions">
        <div class="section-toolbar">
          <span>{{ t('execution.governance.aiHint') }}</span>
          <el-select v-model="suggestionStatus" clearable :placeholder="t('execution.governance.allStatuses')" @change="applySuggestionFilter">
            <el-option v-for="status in suggestionStatuses" :key="status" :label="t(`execution.governance.suggestionStatuses.${status}`)" :value="status" />
          </el-select>
        </div>
        <el-table :data="suggestions" v-loading="suggestionLoading" row-key="id">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="project" :label="t('execution.governance.project')" width="95" />
          <el-table-column prop="job" :label="t('execution.governance.job')" width="85" />
          <el-table-column prop="kind" :label="t('execution.governance.kind')" min-width="170" />
          <el-table-column prop="status" :label="t('execution.governance.status')" width="110" />
          <el-table-column prop="model_name" :label="t('execution.governance.model')" min-width="130" />
          <el-table-column prop="suggestion_hash" :label="t('execution.governance.hash')" min-width="180" show-overflow-tooltip />
          <el-table-column :label="t('execution.governance.actions')" width="230" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="showSuggestion(row)">{{ t('execution.governance.audit') }}</el-button>
              <el-button v-if="row.status === 'proposed'" link type="success" :disabled="permissionDenied" @click="openReview(row, 'approve')">{{ t('execution.governance.approve') }}</el-button>
              <el-button v-if="row.status === 'proposed'" link type="danger" :disabled="permissionDenied" @click="openReview(row, 'reject')">{{ t('execution.governance.reject') }}</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty :description="suggestionLoading ? '' : t('execution.governance.noSuggestions')" /></template>
        </el-table>
        <el-pagination v-model:current-page="suggestionPage.page" v-model:page-size="suggestionPage.pageSize" :page-sizes="[10, 20, 50, 100]" :total="suggestionTotal" :disabled="suggestionLoading" layout="total, sizes, prev, pager, next, jumper" class="pagination" @current-change="loadSuggestions" @size-change="changeSuggestionPageSize" />
        <el-drawer v-model="auditVisible" :title="t('execution.governance.auditTitle', { id: selectedSuggestion?.id || '' })" size="680px">
          <el-skeleton v-if="auditLoading" :rows="5" animated />
          <template v-else>
            <el-descriptions v-if="selectedSuggestion" :column="1" border>
              <el-descriptions-item :label="t('execution.governance.suggestion')">{{ formatJson(selectedSuggestion.output_redacted) }}</el-descriptions-item>
              <el-descriptions-item :label="t('execution.governance.reviewNote')">{{ selectedSuggestion.review_note || '-' }}</el-descriptions-item>
            </el-descriptions>
            <h3>{{ t('execution.governance.auditHistory') }}</h3>
            <el-table :data="audits" size="small" row-key="id">
              <el-table-column prop="action" :label="t('execution.governance.action')" width="120" />
              <el-table-column prop="previous_status" :label="t('execution.governance.previousStatus')" width="120" />
              <el-table-column prop="new_status" :label="t('execution.governance.newStatus')" width="120" />
              <el-table-column prop="actor" :label="t('execution.governance.actor')" width="100" />
              <el-table-column prop="note" :label="t('execution.governance.reviewNote')" min-width="140" show-overflow-tooltip />
              <el-table-column prop="created_at" :label="t('execution.governance.createdAt')" min-width="170" />
              <template #empty><el-empty :description="t('execution.governance.noAudit')" /></template>
            </el-table>
          </template>
        </el-drawer>
      </el-tab-pane>

      <el-tab-pane :label="t('execution.governance.gateHistory')" name="gates">
        <div class="filter-row">
          <el-input-number v-model="gateJobFilter" :min="1" :precision="0" :controls="false" clearable :placeholder="t('execution.governance.jobId')" />
          <el-select v-model="gateStageFilter" clearable :placeholder="t('execution.governance.allStages')">
            <el-option v-for="stage in gateStages" :key="stage" :label="t(`execution.governance.stages.${stage}`)" :value="stage" />
          </el-select>
          <el-select v-model="gatePassedFilter" clearable :placeholder="t('execution.governance.allResults')">
            <el-option :label="t('execution.governance.passed')" :value="true" />
            <el-option :label="t('execution.governance.failed')" :value="false" />
          </el-select>
          <el-button type="primary" @click="applyGateFilters">{{ t('common.search') }}</el-button>
        </div>
        <el-table :data="gateEvaluations" v-loading="gateLoading" row-key="id">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="job" :label="t('execution.governance.job')" width="85" />
          <el-table-column prop="attempt" :label="t('execution.governance.attempt')" width="95" />
          <el-table-column prop="stage" :label="t('execution.governance.stage')" width="120" />
          <el-table-column :label="t('execution.governance.result')" width="100"><template #default="{ row }"><el-tag :type="row.passed ? 'success' : 'danger'">{{ row.passed ? t('execution.governance.passed') : t('execution.governance.failed') }}</el-tag></template></el-table-column>
          <el-table-column prop="reason" :label="t('execution.governance.reason')" min-width="160" show-overflow-tooltip />
          <el-table-column :label="t('execution.governance.failures')" min-width="180"><template #default="{ row }"><span class="truncated-json">{{ formatJson(row.failures) }}</span></template></el-table-column>
          <el-table-column prop="policy_version" :label="t('execution.governance.policyVersion')" min-width="120" />
          <el-table-column prop="created_at" :label="t('execution.governance.evaluatedAt')" min-width="180" />
          <template #empty><el-empty :description="gateLoading ? '' : t('execution.governance.noGateEvaluations')" /></template>
        </el-table>
        <el-pagination v-model:current-page="gatePage.page" v-model:page-size="gatePage.pageSize" :page-sizes="[10, 20, 50, 100]" :total="gateTotal" :disabled="gateLoading" layout="total, sizes, prev, pager, next, jumper" class="pagination" @current-change="loadGateEvaluations" @size-change="changeGatePageSize" />
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="policyDialogVisible" :title="editingPolicy ? t('execution.governance.editPolicy') : t('execution.governance.addPolicy')" width="720px" destroy-on-close>
      <el-form label-position="top">
        <el-row :gutter="16">
          <el-col :span="12"><el-form-item :label="t('execution.governance.project')" required><el-select v-model="policyForm.project" filterable><el-option v-for="project in projects" :key="project.id" :label="`${project.name} (#${project.id})`" :value="project.id" /></el-select></el-form-item></el-col>
          <el-col :span="12"><el-form-item :label="t('execution.governance.profileId')"><el-input-number v-model="policyForm.profile" :min="1" :precision="0" :controls="false" clearable /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="12"><el-form-item :label="t('execution.governance.name')"><el-input v-model="policyForm.name" maxlength="160" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item :label="t('execution.governance.caseKey')"><el-input v-model="policyForm.case_key" maxlength="255" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="12"><el-form-item :label="t('execution.governance.maxAttempts')"><el-input-number v-model="policyForm.max_attempts" :min="1" :max="100" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item :label="t('execution.governance.backoff')"><el-input-number v-model="policyForm.backoff_seconds" :min="0" :max="86400" /></el-form-item></el-col>
        </el-row>
        <el-form-item :label="t('execution.governance.retryableStatuses')"><el-input v-model="policyForm.retryable_statuses_text" type="textarea" :rows="2" spellcheck="false" placeholder='["failed", "timeout"]' /></el-form-item>
        <el-form-item :label="t('execution.governance.retryableErrors')"><el-input v-model="policyForm.retryable_error_types_text" type="textarea" :rows="2" spellcheck="false" placeholder='["timeout", "connection_error"]' /></el-form-item>
        <el-form-item :label="t('execution.governance.enabled')"><el-switch v-model="policyForm.enabled" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="policyDialogVisible = false">{{ t('common.cancel') }}</el-button><el-button type="primary" :loading="saving" :disabled="permissionDenied" @click="savePolicy">{{ t('common.save') }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="subscriptionDialogVisible" :title="editingSubscription ? t('execution.governance.editSubscription') : t('execution.governance.addSubscription')" width="600px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item :label="t('execution.governance.project')" required><el-select v-model="subscriptionForm.project" filterable><el-option v-for="project in projects" :key="project.id" :label="`${project.name} (#${project.id})`" :value="project.id" /></el-select></el-form-item>
        <el-form-item :label="t('execution.governance.channel')" required><el-select v-model="subscriptionForm.channel" filterable><el-option v-for="channel in channels" :key="channel.id" :label="`${channel.name} (#${channel.id})${channel.enabled ? '' : ` · ${t('execution.governance.disabledChannel')}`}`" :value="channel.id" /></el-select></el-form-item>
        <el-form-item :label="t('execution.governance.eventType')" required><el-select v-model="subscriptionForm.event_type"><el-option v-for="event in notificationTypes" :key="event" :label="t(`execution.governance.notificationTypes.${event}`)" :value="event" /></el-select></el-form-item>
        <el-form-item :label="t('execution.governance.enabled')"><el-switch v-model="subscriptionForm.enabled" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="subscriptionDialogVisible = false">{{ t('common.cancel') }}</el-button><el-button type="primary" :loading="saving" :disabled="permissionDenied" @click="saveSubscription">{{ t('common.save') }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="reviewDialogVisible" :title="t(reviewAction === 'approve' ? 'execution.governance.approveTitle' : 'execution.governance.rejectTitle')" width="560px">
      <el-form label-position="top"><el-form-item :label="t('execution.governance.reviewNote')"><el-input v-model="reviewNote" type="textarea" :rows="4" maxlength="4000" show-word-limit /></el-form-item></el-form>
      <template #footer><el-button @click="reviewDialogVisible = false">{{ t('common.cancel') }}</el-button><el-button :type="reviewAction === 'approve' ? 'success' : 'danger'" :loading="saving" :disabled="permissionDenied" @click="submitReview">{{ t(reviewAction === 'approve' ? 'execution.governance.approve' : 'execution.governance.reject') }}</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import {
  approveExecutionAISuggestion,
  createExecutionNotificationSubscription,
  createExecutionRetryPolicy,
  deleteExecutionRetryPolicy,
  listExecutionAISuggestions,
  listExecutionAISuggestionAudits,
  listExecutionGateEvaluations,
  listExecutionNotificationChannels,
  listExecutionNotificationEvents,
  listExecutionNotificationSubscriptions,
  listExecutionProjects,
  listExecutionRetryPolicies,
  rejectExecutionAISuggestion,
  updateExecutionNotificationSubscription,
  updateExecutionRetryPolicy,
} from '@/api/executions'

const { t } = useI18n()
const activeTab = ref('retry')
const projects = ref([])
const channels = ref([])
const projectFilter = ref(null)
const pageError = ref('')
const permissionDenied = ref(false)
const saving = ref(false)
const retryLoading = ref(false)
const subscriptionLoading = ref(false)
const eventLoading = ref(false)
const suggestionLoading = ref(false)
const gateLoading = ref(false)
const auditLoading = ref(false)
const retryPolicies = ref([])
const subscriptions = ref([])
const notificationEvents = ref([])
const suggestions = ref([])
const audits = ref([])
const gateEvaluations = ref([])
const retryPage = ref({ page: 1, pageSize: 20 })
const subscriptionPage = ref({ page: 1, pageSize: 20 })
const eventPage = ref({ page: 1, pageSize: 20 })
const suggestionPage = ref({ page: 1, pageSize: 20 })
const gatePage = ref({ page: 1, pageSize: 20 })
const retryTotal = ref(0)
const subscriptionTotal = ref(0)
const eventTotal = ref(0)
const suggestionTotal = ref(0)
const gateTotal = ref(0)
const policyDialogVisible = ref(false)
const editingPolicy = ref(null)
const subscriptionDialogVisible = ref(false)
const editingSubscription = ref(null)
const reviewDialogVisible = ref(false)
const reviewAction = ref('approve')
const reviewNote = ref('')
const selectedSuggestion = ref(null)
const auditVisible = ref(false)
const suggestionStatus = ref('')
const deliveryStatus = ref('')
const gateJobFilter = ref(null)
const gateStageFilter = ref('')
const gatePassedFilter = ref(null)
const policyForm = ref({ project: null, profile: null, name: '', case_key: '', max_attempts: 2, backoff_seconds: 0, retryable_statuses_text: '["failed"]', retryable_error_types_text: '[]', enabled: true })
const subscriptionForm = ref({ project: null, channel: null, event_type: 'gate_failed', enabled: true })
const deliveryStatuses = ['pending', 'dispatching', 'sent', 'failed']
const suggestionStatuses = ['proposed', 'approved', 'rejected', 'expired']
const gateStages = ['dispatch', 'result', 'release', 'webhook']
const notificationTypes = ['gate_failed', 'integration_failed', 'failure_reopened', 'quarantine_expiring']
const loadingActive = computed(() => ({ retry: retryLoading.value, notifications: subscriptionLoading.value || eventLoading.value, suggestions: suggestionLoading.value, gates: gateLoading.value }[activeTab.value]))

const rowsFrom = response => {
  const data = response.data || {}
  const rows = data.results || (Array.isArray(data) ? data : [])
  return { rows, count: Number(data.count ?? rows.length) }
}
const formatJson = value => JSON.stringify(value ?? {}, null, 2)
const channelName = id => channels.value.find(channel => Number(channel.id) === Number(id))?.name || `#${id}`
const setError = error => {
  const status = error.response?.status
  const detail = error.response?.data?.detail
  const fallback = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : error.message || t('execution.governance.loadFailed')
  permissionDenied.value = [401, 403, 404].includes(status)
  pageError.value = status === 401 ? t('execution.governance.authRequired')
    : [403, 404].includes(status) ? t('execution.governance.permissionDenied')
      : fallback
}
const beginLoad = () => { pageError.value = ''; permissionDenied.value = false }
const loadReferences = async () => {
  try {
    const [projectResponse, channelResponse] = await Promise.all([
      listExecutionProjects({ page_size: 100, ordering: 'name' }),
      listExecutionNotificationChannels({ page: 1, page_size: 200 }),
    ])
    projects.value = rowsFrom(projectResponse).rows
    channels.value = rowsFrom(channelResponse).rows
  } catch (error) { setError(error) }
}
const fetchPage = async (request, page, setter, totalSetter, loading, params = {}) => {
  loading.value = true
  beginLoad()
  try {
    const response = await request({ ...params, page: page.value.page, page_size: page.value.pageSize })
    const result = rowsFrom(response)
    setter.value = result.rows
    totalSetter.value = result.count
  } catch (error) { setError(error) } finally { loading.value = false }
}
const loadRetryPolicies = () => fetchPage(listExecutionRetryPolicies, retryPage, retryPolicies, retryTotal, retryLoading, {
  ordering: 'project,profile,case_key,id', ...(projectFilter.value ? { project: projectFilter.value } : {}),
})
const loadSubscriptions = () => fetchPage(listExecutionNotificationSubscriptions, subscriptionPage, subscriptions, subscriptionTotal, subscriptionLoading, {
  ...(projectFilter.value ? { project: projectFilter.value } : {}),
})
const loadNotificationEvents = () => fetchPage(listExecutionNotificationEvents, eventPage, notificationEvents, eventTotal, eventLoading, {
  ordering: '-created_at', ...(projectFilter.value ? { project: projectFilter.value } : {}), ...(deliveryStatus.value ? { status: deliveryStatus.value } : {}),
})
const loadSuggestions = () => fetchPage(listExecutionAISuggestions, suggestionPage, suggestions, suggestionTotal, suggestionLoading, {
  ordering: '-created_at', ...(projectFilter.value ? { project: projectFilter.value } : {}), ...(suggestionStatus.value ? { status: suggestionStatus.value } : {}),
})
const loadGateEvaluations = () => fetchPage(listExecutionGateEvaluations, gatePage, gateEvaluations, gateTotal, gateLoading, {
  ordering: '-created_at', ...(gateJobFilter.value ? { job: gateJobFilter.value } : {}), ...(gateStageFilter.value ? { stage: gateStageFilter.value } : {}), ...(gatePassedFilter.value !== null && gatePassedFilter.value !== '' ? { passed: gatePassedFilter.value } : {}),
})
const reloadActive = () => {
  if (activeTab.value === 'retry') return loadRetryPolicies()
  if (activeTab.value === 'notifications') return Promise.all([loadSubscriptions(), loadNotificationEvents()])
  if (activeTab.value === 'suggestions') return loadSuggestions()
  return loadGateEvaluations()
}
const applyProjectFilter = () => {
  retryPage.value = { ...retryPage.value, page: 1 }
  subscriptionPage.value = { ...subscriptionPage.value, page: 1 }
  eventPage.value = { ...eventPage.value, page: 1 }
  suggestionPage.value = { ...suggestionPage.value, page: 1 }
  return reloadActive()
}
const applySuggestionFilter = () => { suggestionPage.value = { ...suggestionPage.value, page: 1 }; loadSuggestions() }
const changeRetryPageSize = size => { retryPage.value = { ...retryPage.value, page: 1, pageSize: size }; loadRetryPolicies() }
const changeSubscriptionPageSize = size => { subscriptionPage.value = { ...subscriptionPage.value, page: 1, pageSize: size }; loadSubscriptions() }
const changeEventPageSize = size => { eventPage.value = { ...eventPage.value, page: 1, pageSize: size }; loadNotificationEvents() }
const changeSuggestionPageSize = size => { suggestionPage.value = { ...suggestionPage.value, page: 1, pageSize: size }; loadSuggestions() }
const changeGatePageSize = size => { gatePage.value = { ...gatePage.value, page: 1, pageSize: size }; loadGateEvaluations() }
const applyGateFilters = () => { gatePage.value = { ...gatePage.value, page: 1 }; loadGateEvaluations() }
const resetDeliveryPage = () => { eventPage.value = { ...eventPage.value, page: 1 }; loadNotificationEvents() }

const newPolicyForm = () => ({ project: projectFilter.value, profile: null, name: '', case_key: '', max_attempts: 2, backoff_seconds: 0, retryable_statuses_text: '["failed"]', retryable_error_types_text: '[]', enabled: true })
const openPolicyCreate = () => { editingPolicy.value = null; policyForm.value = newPolicyForm(); policyDialogVisible.value = true }
const openPolicyEdit = row => {
  editingPolicy.value = row.id
  policyForm.value = { ...row, retryable_statuses_text: formatJson(row.retryable_statuses), retryable_error_types_text: formatJson(row.retryable_error_types) }
  policyDialogVisible.value = true
}
const savePolicy = async () => {
  let retryable_statuses
  let retryable_error_types
  try {
    retryable_statuses = JSON.parse(policyForm.value.retryable_statuses_text || '[]')
    retryable_error_types = JSON.parse(policyForm.value.retryable_error_types_text || '[]')
    if (!Array.isArray(retryable_statuses) || !Array.isArray(retryable_error_types)) throw new Error('arrays-required')
  } catch {
    ElMessage.error(t('execution.governance.retryArraysRequired'))
    return
  }
  if (!policyForm.value.project || policyForm.value.max_attempts < 1) {
    ElMessage.warning(t('execution.governance.policyRequired'))
    return
  }
  saving.value = true
  beginLoad()
  try {
    const payload = { ...policyForm.value, retryable_statuses, retryable_error_types, profile: policyForm.value.profile || null }
    delete payload.retryable_statuses_text
    delete payload.retryable_error_types_text
    if (editingPolicy.value) await updateExecutionRetryPolicy(editingPolicy.value, payload)
    else await createExecutionRetryPolicy(payload)
    policyDialogVisible.value = false
    await loadRetryPolicies()
    ElMessage.success(t('execution.governance.saveSuccess'))
  } catch (error) { setError(error) } finally { saving.value = false }
}
const removePolicy = async row => {
  try {
    await ElMessageBox.confirm(t('execution.governance.deletePolicyConfirm', { name: row.name || row.case_key || row.id }), t('common.confirm'), { type: 'warning' })
    await deleteExecutionRetryPolicy(row.id)
    retryPage.value = { ...retryPage.value, page: 1 }
    await loadRetryPolicies()
  } catch (error) { if (error !== 'cancel' && error !== 'close') setError(error) }
}
const openSubscriptionCreate = () => {
  editingSubscription.value = null
  subscriptionForm.value = { project: projectFilter.value, channel: null, event_type: 'gate_failed', enabled: true }
  subscriptionDialogVisible.value = true
}
const openSubscriptionEdit = row => { editingSubscription.value = row.id; subscriptionForm.value = { project: row.project, channel: row.channel, event_type: row.event_type, enabled: row.enabled }; subscriptionDialogVisible.value = true }
const saveSubscription = async () => {
  if (!subscriptionForm.value.project || !subscriptionForm.value.channel || !subscriptionForm.value.event_type) {
    ElMessage.warning(t('execution.governance.subscriptionRequired'))
    return
  }
  saving.value = true
  beginLoad()
  try {
    if (editingSubscription.value) await updateExecutionNotificationSubscription(editingSubscription.value, subscriptionForm.value)
    else await createExecutionNotificationSubscription(subscriptionForm.value)
    subscriptionDialogVisible.value = false
    await loadSubscriptions()
    ElMessage.success(t('execution.governance.saveSuccess'))
  } catch (error) { setError(error) } finally { saving.value = false }
}
const toggleSubscription = async row => {
  saving.value = true
  beginLoad()
  try {
    await updateExecutionNotificationSubscription(row.id, { enabled: !row.enabled })
    await Promise.all([loadSubscriptions(), loadNotificationEvents()])
    ElMessage.success(t('execution.governance.saveSuccess'))
  } catch (error) { setError(error) } finally { saving.value = false }
}
const showSuggestion = async row => {
  selectedSuggestion.value = row
  auditVisible.value = true
  auditLoading.value = true
  beginLoad()
  try {
    const response = await listExecutionAISuggestionAudits({ suggestion: row.id, page: 1, page_size: 100, ordering: '-created_at' })
    audits.value = rowsFrom(response).rows
  } catch (error) { setError(error) } finally { auditLoading.value = false }
}
const openReview = (row, action) => {
  selectedSuggestion.value = row
  reviewAction.value = action
  reviewNote.value = ''
  reviewDialogVisible.value = true
}
const submitReview = async () => {
  if (!selectedSuggestion.value) return
  saving.value = true
  beginLoad()
  try {
    const review = reviewAction.value === 'approve' ? approveExecutionAISuggestion : rejectExecutionAISuggestion
    const response = await review(selectedSuggestion.value.id, reviewNote.value)
    selectedSuggestion.value = response.data
    reviewDialogVisible.value = false
    await Promise.all([loadSuggestions(), showSuggestion(response.data)])
    ElMessage.success(t(reviewAction.value === 'approve' ? 'execution.governance.approveSuccess' : 'execution.governance.rejectSuccess'))
  } catch (error) { setError(error) } finally { saving.value = false }
}

onMounted(async () => {
  await loadReferences()
  if (!pageError.value) await reloadActive()
})
</script>

<style scoped>
.governance-page { display: grid; gap: 12px; }
.page-header, .header-actions, .section-toolbar, .section-heading, .filter-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.page-header h1 { margin: 0; font-size: 22px; }
.page-header p, .section-heading p { margin: 6px 0 0; color: var(--el-text-color-secondary); }
.header-actions { justify-content: flex-end; }
.page-alert { margin-bottom: 4px; }
.section-toolbar { margin: 0 0 14px; color: var(--el-text-color-secondary); }
.section-heading { margin: 20px 0 12px; }
.section-heading h2 { margin: 0; font-size: 17px; }
.filter-row { justify-content: flex-start; margin-bottom: 14px; flex-wrap: wrap; }
.filter-row > * { min-width: 170px; }
.pagination { display: flex; justify-content: flex-end; margin: 14px 0; }
.json-detail { max-height: 220px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.truncated-json { display: block; max-height: 3.3em; overflow: hidden; white-space: pre-wrap; }
@media (max-width: 768px) {
  .page-header { align-items: flex-start; flex-direction: column; }
  .header-actions, .section-toolbar { align-items: stretch; width: 100%; flex-direction: column; }
  .header-actions > *, .section-toolbar > * { width: 100%; }
}
</style>
