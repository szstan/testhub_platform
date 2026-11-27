<template>
  <div class="requirement-analysis">
    <header class="page-header">
      <div>
        <h1>{{ $t('requirementAnalysis.title') }}</h1>
        <p>{{ $t('requirementAnalysis.workspaceSubtitle') }}</p>
      </div>
      <button class="button secondary" type="button" @click="openRecords">
        {{ $t('requirementAnalysis.generationHistory') }}
      </button>
    </header>

    <div v-if="generationError" class="notice error" role="alert">
      <strong>{{ $t('requirementAnalysis.generateFailed') }}</strong>
      <p>{{ generationError }}</p>
    </div>
    <div v-if="monitoringError" class="notice warning" role="status">
      <p>{{ monitoringError }}</p>
      <button class="text-button" type="button" @click="retryMonitoring">{{ $t('requirementAnalysis.retryConnection') }}</button>
    </div>

    <div v-if="isRestoring" class="empty-state" role="status">{{ $t('requirementAnalysis.restoringTask') }}</div>

    <div v-else-if="!currentTaskId && !isGenerating" class="workspace-grid">
      <section class="panel input-panel">
        <div class="section-heading">
          <span class="section-number">1</span>
          <div><h2>{{ $t('requirementAnalysis.prepareRequirement') }}</h2><p>{{ $t('requirementAnalysis.inputHint') }}</p></div>
        </div>
        <form @submit.prevent="submitGeneration">
          <fieldset :disabled="isSubmitting">
            <div class="form-group">
              <label for="generation-project">{{ $t('requirementAnalysis.associatedProject') }} <span class="required">*</span></label>
              <select id="generation-project" v-model="selectedProject" :disabled="loadingProjects" required>
                <option value="">{{ $t(loadingProjects ? 'requirementAnalysis.loadingProjects' : 'requirementAnalysis.selectProject') }}</option>
                <option v-for="project in projects" :key="project.id" :value="project.id">{{ project.name }}</option>
              </select>
              <div v-if="projectsError" class="field-error" role="alert">
                {{ projectsError }} <button type="button" class="text-button" @click="loadProjects">{{ $t('requirementAnalysis.retryLoad') }}</button>
              </div>
              <p v-else-if="!loadingProjects && !projects.length" class="field-hint">{{ $t('requirementAnalysis.noProjects') }}</p>
              <p v-else class="field-hint">{{ $t('requirementAnalysis.projectHint') }}</p>
            </div>

            <div class="form-group">
              <label for="generation-title">{{ $t('requirementAnalysis.requirementTitle') }} <span class="required">*</span></label>
              <input id="generation-title" v-model="manualInput.title" maxlength="200" required :placeholder="$t('requirementAnalysis.titlePlaceholder')">
            </div>

            <div class="source-selector" role="group" :aria-label="$t('requirementAnalysis.inputSource')">
              <button type="button" :class="{ selected: inputSource === 'manual' }" :aria-pressed="inputSource === 'manual'" @click="inputSource = 'manual'">{{ $t('requirementAnalysis.textInput') }}</button>
              <button type="button" :class="{ selected: inputSource === 'document' }" :aria-pressed="inputSource === 'document'" @click="inputSource = 'document'">{{ $t('requirementAnalysis.documentInput') }}</button>
            </div>

            <div v-if="inputSource === 'manual'" class="form-group">
              <label for="generation-description">{{ $t('requirementAnalysis.requirementDescription') }} <span class="required">*</span></label>
              <textarea id="generation-description" v-model="manualInput.description" :maxlength="maxInputLength" rows="12" required :placeholder="$t('requirementAnalysis.detailedPlaceholder')" />
              <div class="input-meta"><span>{{ $t('requirementAnalysis.requirementHint') }}</span><span>{{ manualInput.description.length.toLocaleString() }} / {{ maxInputLength.toLocaleString() }}</span></div>
            </div>
            <div v-else class="form-group">
              <input ref="fileInput" class="file-input" type="file" accept=".pdf,.docx,.txt,.md" :aria-label="$t('requirementAnalysis.selectFile')" @change="handleFileSelect">
              <div class="upload-area" :class="{ 'drag-over': isDragOver }" @dragover.prevent="isDragOver = true" @dragleave.prevent="isDragOver = false" @drop.prevent="handleDrop">
                <template v-if="selectedFile">
                  <strong class="file-name">{{ selectedFile.name }}</strong>
                  <span class="field-hint">{{ formatFileSize(selectedFile.size) }}</span>
                  <button type="button" class="text-button" @click="removeFile">{{ $t('requirementAnalysis.removeSelectedFile') }}</button>
                </template>
                <template v-else>
                  <strong>{{ $t('requirementAnalysis.dragDropText') }}</strong>
                  <span class="field-hint">{{ $t('requirementAnalysis.supportedFormats') }}</span>
                  <button type="button" class="button secondary" @click="$refs.fileInput.click()">{{ $t('requirementAnalysis.selectFile') }}</button>
                </template>
              </div>
              <p class="field-hint">{{ $t('requirementAnalysis.documentHint') }}</p>
            </div>

            <details class="advanced-settings">
              <summary>{{ $t('requirementAnalysis.displaySettings') }}</summary>
              <div class="radio-row">
                <label><input v-model="globalOutputMode" type="radio" value="stream" @change="outputModeTouched = true">{{ $t('requirementAnalysis.realtimeStream') }}</label>
                <label><input v-model="globalOutputMode" type="radio" value="complete" @change="outputModeTouched = true">{{ $t('requirementAnalysis.completeOutput') }}</label>
              </div>
              <p class="field-hint">{{ $t('requirementAnalysis.displayModeHint') }}</p>
            </details>
            <div class="submit-row">
              <p class="field-hint" role="status">{{ submitHint }}</p>
              <button class="button primary" type="submit" :disabled="!canSubmit">{{ $t('requirementAnalysis.startGeneration') }}</button>
            </div>
          </fieldset>
        </form>
      </section>

      <aside class="sidebar">
        <section class="panel config-panel">
          <div class="section-heading"><span class="section-number">2</span><h2>{{ $t('requirementAnalysis.generationSetup') }}</h2></div>
          <p v-if="checkingConfig" class="field-hint" role="status">{{ $t('requirementAnalysis.checkingConfiguration') }}</p>
          <div v-else-if="configError" class="field-error" role="alert">
            <p>{{ configError }}</p><button class="text-button" type="button" @click="checkConfigStatus">{{ $t('requirementAnalysis.retryLoad') }}</button>
          </div>
          <template v-else>
            <div class="config-status" :class="{ ready: configReady }"><span class="status-dot" />{{ $t(configReady ? 'requirementAnalysis.readyToGenerate' : 'requirementAnalysis.configurationNeeded') }}</div>
            <dl class="config-list">
              <div v-for="item in configItems" :key="item.key">
                <dt>{{ $t(item.label) }}</dt><dd :class="{ missing: !item.ready }">{{ item.name || $t(item.ready ? 'requirementAnalysis.readyToGenerate' : 'requirementAnalysis.notConfigured') }}</dd>
              </div>
              <div><dt>{{ $t('requirementAnalysis.autoReview') }}</dt><dd>{{ $t(showReviewStep ? 'requirementAnalysis.reviewEnabled' : 'requirementAnalysis.reviewDisabled') }}</dd></div>
            </dl>
            <button v-if="!configReady" type="button" class="button secondary full-width" @click="goToConfig">{{ $t('configGuide.goToConfig') }}</button>
            <button type="button" class="text-button" @click="checkConfigStatus">{{ $t('requirementAnalysis.refreshConfiguration') }}</button>
          </template>
        </section>
        <section class="panel guidance-panel">
          <h2>{{ $t('requirementAnalysis.workflowTitle') }}</h2>
          <ol><li>{{ $t('requirementAnalysis.workflowInput') }}</li><li>{{ $t('requirementAnalysis.workflowReview') }}</li><li>{{ $t('requirementAnalysis.workflowImport') }}</li></ol>
          <p>{{ $t('requirementAnalysis.qualityHint') }}</p>
        </section>
      </aside>
    </div>

    <section v-else-if="!isRestoring" class="panel generation-panel" :aria-busy="isGenerating">
      <div class="generation-heading">
        <div><span class="eyebrow">{{ $t('requirementAnalysis.taskId') }} {{ currentTaskId || '—' }}</span><h2>{{ activeTitle || manualInput.title }}</h2></div>
        <span class="status-badge" :class="taskStatus">{{ statusLabel }}</span>
      </div>
      <div class="progress-meta" role="status"><span>{{ progressText }}</span><span>{{ progress }}%</span></div>
      <progress :value="progress" max="100" :aria-label="$t('requirementAnalysis.progress')" />
      <ol class="progress-steps">
        <li :class="{ active: currentStep >= 1 }">{{ $t('requirementAnalysis.stepAnalysis') }}</li>
        <li :class="{ active: currentStep >= 2 }">{{ $t('requirementAnalysis.stepWriting') }}</li>
        <li v-if="showReviewStep" :class="{ active: currentStep >= 3 }">{{ $t('requirementAnalysis.stepReview') }}</li>
        <li :class="{ active: showResults }">{{ $t('requirementAnalysis.stepComplete') }}</li>
      </ol>

      <div v-if="showResults" class="completion-summary">
        <div><strong>{{ $t('requirementAnalysis.resultCount', { count: canonicalCases.length }) }}</strong><p>{{ $t('requirementAnalysis.resultHint') }}</p><p>{{ $t('requirementAnalysis.remainingCases', { count: remainingCases.length }) }}</p></div>
        <div class="action-row">
          <button type="button" class="button secondary" :disabled="!canonicalCases.length" @click="downloadTestCases">{{ $t('requirementAnalysis.downloadExcel') }}</button>
          <button type="button" class="button secondary" @click="openTaskDetail">{{ $t('requirementAnalysis.reviewAndEdit') }}</button>
          <button type="button" class="button primary" :disabled="isSaving || isSaved || !canonicalCases.length" @click="saveToTestCaseRecords">{{ $t(isSaving ? 'requirementAnalysis.importingCases' : isSaved ? 'requirementAnalysis.casesImported' : 'requirementAnalysis.importCases') }}</button>
        </div>
      </div>

      <div class="content-tabs" role="tablist" :aria-label="$t('requirementAnalysis.generatedContent')">
        <button v-for="item in contentTabs" :id="`generation-tab-${item.key}`" :key="item.key" type="button" role="tab" :aria-selected="contentTab === item.key" :aria-controls="`generation-content-${item.key}`" :tabindex="contentTab === item.key ? 0 : -1" :class="{ selected: contentTab === item.key }" @click="contentTab = item.key" @keydown="navigateContentTabs($event, item.key)">{{ $t(item.label) }}</button>
      </div>
      <div :id="`generation-content-${contentTab}`" role="tabpanel" :aria-labelledby="`generation-tab-${contentTab}`" class="content-panel">
        <template v-if="contentTab === 'final' && canonicalCases.length">
          <div class="case-table-wrapper"><table class="case-table">
            <thead><tr><th>{{ $t('requirementAnalysis.excelTestCaseNumber') }}</th><th>{{ $t('requirementAnalysis.excelTestScenario') }}</th><th>{{ $t('requirementAnalysis.excelPrecondition') }}</th><th>{{ $t('requirementAnalysis.excelTestSteps') }}</th><th>{{ $t('requirementAnalysis.excelExpectedResult') }}</th><th>{{ $t('requirementAnalysis.excelPriority') }}</th></tr></thead>
            <tbody><tr v-for="testCase in visibleCases" :key="testCase.case_id"><td>{{ testCase.case_id }}</td><td>{{ testCase.title }}</td><td>{{ testCase.preconditions }}</td><td>{{ (testCase.steps || []).join('\n') }}</td><td>{{ testCase.expected_result }}</td><td><span class="priority-badge">{{ testCase.priority }}</span></td></tr></tbody>
          </table></div>
          <div v-if="canonicalCases.length > pageSize" class="pagination"><button type="button" class="button secondary" :disabled="casePage === 1" @click="casePage--">{{ $t('generatedTestCases.previousPage') }}</button><span>{{ casePage }} / {{ Math.ceil(canonicalCases.length / pageSize) }}</span><button type="button" class="button secondary" :disabled="casePage * pageSize >= canonicalCases.length" @click="casePage++">{{ $t('generatedTestCases.nextPage') }}</button></div>
        </template>
        <template v-else-if="activeContent">
          <p v-if="contentTab === 'final' && showResults" class="field-error">{{ $t('requirementAnalysis.unstructuredResult') }}</p>
          <div class="markdown-content" v-html="formatMarkdown(activeContent)" />
        </template>
        <div v-else class="empty-state" role="status">{{ $t(isGenerating ? 'requirementAnalysis.waitingForContent' : 'requirementAnalysis.noStageContent') }}</div>
      </div>
      <footer class="generation-footer">
        <p class="field-hint">{{ $t(isGenerating ? 'requirementAnalysis.backgroundHint' : 'requirementAnalysis.historyHint') }}</p>
        <button v-if="isGenerating" type="button" class="button danger" :disabled="isCancelling || !currentTaskId || isSubmitting" @click="cancelGeneration">{{ $t(isCancelling ? 'requirementAnalysis.cancelling' : 'requirementAnalysis.cancelGeneration') }}</button>
        <button v-else type="button" class="button secondary" :disabled="isSaving" @click="resetGeneration">{{ $t(showResults ? 'requirementAnalysis.newGeneration' : 'requirementAnalysis.editAndRetry') }}</button>
      </footer>
    </section>
  </div>
</template>

<script>
import api from '@/utils/api'
import { ElMessage } from 'element-plus'
import * as XLSX from 'xlsx'
import { useUserStore } from '@/stores/user'
import { renderSafeMarkdown } from '@/utils/safe-html.js'

const ACTIVE_STATUSES = ['pending', 'generating', 'reviewing', 'revising']
const FILE_LIMIT = 10 * 1024 * 1024
const MANUAL_INPUT_LIMIT = 50000
const GENERATION_TEXT_LIMIT = 100000

function errorText(error, fallback) {
  const data = error?.response?.data
  if (typeof data === 'string' && !data.trim().startsWith('<')) return data
  if (data && typeof data === 'object') {
    const messages = Object.values(data).flat().filter(value => typeof value === 'string')
    if (messages.length) return messages.join('\n')
  }
  return error?.message || fallback
}

function newSubmissionId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  // getRandomValues also works on HTTP LAN deployments where randomUUID is unavailable.
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export default {
  name: 'RequirementAnalysisView',
  data() {
    return {
      inputSource: 'manual',
      manualInput: { title: '', description: '' },
      restoredInputStyle: null,
      restoredTitleLabel: '',
      selectedProject: '',
      selectedFile: null,
      projects: [],
      loadingProjects: true,
      projectsError: '',
      projectsLoadingRequest: false,
      configLoadingRequest: false,
      uploadedDocument: null,
      isDragOver: false,
      globalOutputMode: 'stream',
      outputModeTouched: false,
      configStatus: {},
      checkingConfig: true,
      configError: '',
      showReviewStep: true,
      isSubmitting: false,
      isGenerating: false,
      isRestoring: false,
      isCancelling: false,
      isSaving: false,
      currentTaskId: null,
      taskStatus: 'pending',
      activeTitle: '',
      progress: 0,
      progressText: '',
      currentStep: 0,
      generationError: '',
      monitoringError: '',
      streamedContent: '',
      streamedReviewContent: '',
      finalTestCases: '',
      generationResult: null,
      showResults: false,
      hasShownCompletionMessage: false,
      contentTab: 'draft',
      casePage: 1,
      pageSize: 10,
      pollInterval: null,
      pollInFlight: false,
      eventSource: null,
      finalizingTaskId: null,
      runRevision: 0,
      disposed: false,
      storageKey: '',
      pendingSubmissionId: '',
    }
  },
  computed: {
    configItems() {
      const keys = ['writer_model', 'writer_prompt', ...(this.showReviewStep ? ['reviewer_model', 'reviewer_prompt'] : [])]
      const labels = { writer_model: 'requirementAnalysis.writerModel', writer_prompt: 'requirementAnalysis.writerPrompt', reviewer_model: 'requirementAnalysis.reviewerModel', reviewer_prompt: 'requirementAnalysis.reviewerPrompt' }
      return keys.map(key => ({ key, label: labels[key], name: this.configStatus[key]?.name, ready: Boolean(this.configStatus[key]?.configured && this.configStatus[key]?.enabled) }))
    },
    configReady() { return !this.checkingConfig && !this.configError && this.configItems.every(item => item.ready) },
    maxInputLength() {
      if (this.restoredInputStyle === null) return MANUAL_INPUT_LIMIT
      return Math.max(0, GENERATION_TEXT_LIMIT - this.buildRequirementText(this.manualInput.title.trim(), '', 'manual').length)
    },
    canGenerateManual() { return Boolean(this.selectedProject && this.manualInput.title.trim() && this.manualInput.title.trim().length <= 200 && this.manualInput.description.trim() && this.manualInput.description.length <= this.maxInputLength) },
    canSubmit() {
      const inputReady = this.inputSource === 'manual' ? this.canGenerateManual : Boolean(this.selectedProject && this.manualInput.title.trim() && this.manualInput.title.trim().length <= 200 && this.selectedFile)
      return inputReady && this.configReady && !this.loadingProjects && !this.projectsError && !this.isSubmitting && !this.isGenerating && !this.isRestoring
    },
    submitHint() {
      if (!this.selectedProject) return this.$t('requirementAnalysis.selectProjectFirst')
      if (!this.manualInput.title.trim()) return this.$t('requirementAnalysis.titleRequired')
      if (this.manualInput.title.trim().length > 200) return this.$t('requirementAnalysis.titleTooLong')
      if (this.inputSource === 'manual' && !this.manualInput.description.trim()) return this.$t('requirementAnalysis.descriptionRequired')
      if (this.inputSource === 'manual' && this.manualInput.description.length > this.maxInputLength) return this.$t('requirementAnalysis.descriptionTooLong', { limit: this.maxInputLength.toLocaleString() })
      if (this.inputSource === 'document' && !this.selectedFile) return this.$t('requirementAnalysis.fileRequired')
      return this.$t(this.configReady ? 'requirementAnalysis.readyHint' : 'requirementAnalysis.configurationNeeded')
    },
    canonicalCases() { return Array.isArray(this.generationResult?.canonical_cases) ? this.generationResult.canonical_cases : [] },
    visibleCases() { return this.canonicalCases.slice((this.casePage - 1) * this.pageSize, this.casePage * this.pageSize) },
    remainingCases() {
      const adopted = new Set(Array.isArray(this.generationResult?.adopted_case_ids) ? this.generationResult.adopted_case_ids : [])
      return this.canonicalCases.filter(testCase => !adopted.has(testCase.case_id))
    },
    isSaved() { return this.canonicalCases.length > 0 && this.remainingCases.length === 0 },
    contentTabs() { return [{ key: 'final', label: 'requirementAnalysis.finalTestCases' }, { key: 'draft', label: 'requirementAnalysis.generationDraft' }, ...(this.showReviewStep || this.streamedReviewContent ? [{ key: 'review', label: 'requirementAnalysis.aiReviewFeedback' }] : [])] },
    activeContent() { return this.contentTab === 'final' ? this.finalTestCases : this.contentTab === 'review' ? this.streamedReviewContent : this.streamedContent },
    statusLabel() {
      const labels = { pending: 'requirementAnalysis.pending', generating: 'requirementAnalysis.statusGenerating', reviewing: 'requirementAnalysis.statusReviewing', revising: 'requirementAnalysis.statusRevising', completed: 'requirementAnalysis.completed', failed: 'requirementAnalysis.failed', cancelled: 'requirementAnalysis.cancelled' }
      return this.$t(labels[this.taskStatus] || 'requirementAnalysis.preparing')
    },
  },
  mounted() {
    this.progressText = this.$t('requirementAnalysis.preparing')
    const user = useUserStore().user
    this.storageKey = user?.id ? `testhub:requirement-generation:${user.id}` : ''
    this.loadProjects()
    this.checkConfigStatus()
    if (this.storageKey) {
      try { this.pendingSubmissionId = sessionStorage.getItem(`${this.storageKey}:pending`) || '' } catch { /* Storage is optional. */ }
    }
    if (this.$route?.query?.task) this.restoreActiveTask(this.$route.query.task)
    else if (this.pendingSubmissionId) this.restorePendingSubmission()
    else this.restoreActiveTask()
  },
  beforeUnmount() {
    this.disposed = true
    this.runRevision++
    this.cleanupProgress()
  },
  methods: {
    async loadProjects() {
      if (this.projectsLoadingRequest) return
      this.projectsLoadingRequest = true
      this.loadingProjects = true
      this.projectsError = ''
      try {
        const projects = []
        let page = 1
        let next = true
        while (next && page <= 100) {
          const response = await api.get('/projects/', { params: { page, page_size: 100, ordering: 'id' } })
          const data = response.data
          const rows = Array.isArray(data) ? data : data.results
          if (!Array.isArray(rows)) throw new Error(this.$t('requirementAnalysis.loadProjectsFailed'))
          projects.push(...rows)
          next = !Array.isArray(data) && Boolean(data.next)
          page++
        }
        if (next) throw new Error(this.$t('requirementAnalysis.projectListTooLarge'))
        if (!this.disposed) this.projects = [...new Map(projects.map(project => [project.id, project])).values()]
      } catch (error) {
        if (!this.disposed) this.projectsError = errorText(error, this.$t('requirementAnalysis.loadProjectsFailed'))
      } finally {
        this.loadingProjects = false
        this.projectsLoadingRequest = false
      }
    },
    async checkConfigStatus() {
      if (this.configLoadingRequest) return
      this.configLoadingRequest = true
      this.checkingConfig = true
      this.configError = ''
      try {
        const { data } = await api.get('/requirement-analysis/config/check/')
        if (this.disposed) return
        this.configStatus = data
        if (!this.currentTaskId && !this.isGenerating) this.showReviewStep = data.generation_config?.enable_auto_review !== false
        if (!this.outputModeTouched && !this.isGenerating && !this.currentTaskId && ['stream', 'complete'].includes(data.generation_config?.default_output_mode)) this.globalOutputMode = data.generation_config.default_output_mode
      } catch (error) {
        if (!this.disposed) this.configError = errorText(error, this.$t('requirementAnalysis.configurationCheckFailed'))
      } finally {
        this.checkingConfig = false
        this.configLoadingRequest = false
      }
    },
    goToConfig() {
      const missing = this.configItems.find(item => !item.ready)
      this.$router.push(missing?.key.endsWith('prompt') ? '/configuration/prompt-config' : '/configuration/ai-model')
    },
    openRecords() { this.$router.push({ name: 'GeneratedTestCases' }) },
    openTaskDetail() { if (this.currentTaskId) this.$router.push({ name: 'TaskDetail', params: { taskId: this.currentTaskId } }) },
    handleDrop(event) {
      event.preventDefault()
      this.isDragOver = false
      if (!this.isSubmitting && event.dataTransfer.files.length) this.handleFileSelect({ target: { files: event.dataTransfer.files } })
    },
    handleFileSelect(event) {
      const file = event.target.files?.[0]
      if (!file || this.isSubmitting) return
      if (!/\.(pdf|docx|txt|md)$/i.test(file.name)) {
        ElMessage.error(this.$t('requirementAnalysis.invalidFileFormatDetail'))
      } else if (!file.size || file.size > FILE_LIMIT) {
        ElMessage.error(this.$t(file.size ? 'requirementAnalysis.fileTooLarge' : 'requirementAnalysis.emptyFile'))
      } else {
        this.selectedFile = file
        if (!this.manualInput.title.trim()) this.manualInput.title = file.name.replace(/\.[^.]+$/, '').slice(0, 200)
      }
      if (this.$refs.fileInput) this.$refs.fileInput.value = ''
    },
    removeFile() {
      this.selectedFile = null
      if (this.$refs.fileInput) this.$refs.fileInput.value = ''
    },
    formatFileSize(bytes) { return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB` },
    buildRequirementText(title, description, source = this.inputSource) {
      if (source === 'manual' && this.restoredInputStyle === 'plain') return description
      const label = source === 'manual' && this.restoredInputStyle === 'titled'
        ? this.restoredTitleLabel
        : this.$t('requirementAnalysis.requirementTitle')
      return `${label}: ${title}\n\n${description}`
    },
    restoreRequirementInput(task) {
      const title = task.title || ''
      const text = task.requirement_text || ''
      // Recognize only the exact envelopes produced by this page, including older locale variants.
      const labels = new Set([
        this.$t('requirementAnalysis.requirementTitle'), this.$t('requirementAnalysis.documentTitle'),
        '需求标题', 'Requirement Title', '文档标题', 'Document Title',
      ])
      const label = [...labels].find(value => text.startsWith(`${value}: ${title}\n\n`))
      this.restoredInputStyle = label ? 'titled' : 'plain'
      this.restoredTitleLabel = label || ''
      this.manualInput.description = label ? text.slice(`${label}: ${title}\n\n`.length) : text
      this.inputSource = 'manual'
      this.selectedFile = null
      this.uploadedDocument = null
    },
    async submitGeneration() {
      if (!this.canSubmit) return
      // Lock before uploading or awaiting authentication, so repeated clicks cannot submit twice.
      this.isSubmitting = true
      this.isGenerating = true
      const revision = ++this.runRevision
      this.cleanupProgress()
      this.generationError = ''
      this.monitoringError = ''
      this.showResults = false
      this.generationResult = null
      this.currentTaskId = null
      this.taskStatus = 'pending'
      this.progress = 0
      this.currentStep = 1
      this.streamedContent = ''
      this.streamedReviewContent = ''
      this.finalTestCases = ''
      this.contentTab = 'draft'
      this.casePage = 1
      this.hasShownCompletionMessage = false
      this.activeTitle = this.manualInput.title.trim()
      const projectId = this.selectedProject
      const outputMode = this.globalOutputMode
      const useReviewer = this.showReviewStep
      let requirementText = this.manualInput.description.trim()
      let submitted = false
      try {
        if (this.inputSource === 'document') {
          this.progressText = this.$t('requirementAnalysis.extractingContent')
          const cached = this.uploadedDocument
          let documentId = cached?.file === this.selectedFile && cached?.projectId === projectId && cached?.title === this.activeTitle ? cached.id : null
          if (!documentId) {
            const file = this.selectedFile
            const formData = new FormData()
            formData.append('title', this.activeTitle)
            formData.append('project', projectId)
            formData.append('file', file)
            const uploaded = await api.post('/requirement-analysis/documents/', formData, { headers: { 'Content-Type': 'multipart/form-data' } })
            if (this.disposed || this.runRevision !== revision) return
            documentId = uploaded.data.id
            this.uploadedDocument = { id: documentId, file, projectId, title: this.activeTitle }
          }
          const extracted = await api.get(`/requirement-analysis/documents/${documentId}/extract_text/`)
          requirementText = extracted.data.extracted_text?.trim()
          if (!requirementText) throw new Error(this.$t('requirementAnalysis.extractionFailed'))
        }
        if (this.disposed || this.runRevision !== revision) return
        const text = this.buildRequirementText(this.activeTitle, requirementText)
        if (text.length > GENERATION_TEXT_LIMIT) throw new Error(this.$t('requirementAnalysis.documentTooLong'))
        this.progressText = this.$t('requirementAnalysis.creatingTask')
        const submissionId = this.pendingSubmissionId || newSubmissionId()
        this.rememberSubmission(submissionId)
        submitted = true
        const { data } = await api.post('/requirement-analysis/testcase-generation/generate/', { title: this.activeTitle, requirement_text: text, project: projectId, use_writer_model: true, use_reviewer_model: useReviewer, output_mode: outputMode, client_request_id: submissionId })
        if (this.disposed || this.runRevision !== revision) return
        if (!data.task_id) throw new Error(this.$t('requirementAnalysis.createTaskFailed'))
        this.rememberSubmission(null)
        this.currentTaskId = data.task_id
        this.rememberTask(data.task_id)
        if (data.replayed) {
          this.isSubmitting = false
          await this.restoreActiveTask(data.task_id)
          ElMessage.warning(this.$t('requirementAnalysis.submissionRecovered'))
          return
        }
        if (data.task) {
          this.showReviewStep = Boolean(data.task.reviewer_model_config)
          this.globalOutputMode = data.task.output_mode || outputMode
        }
        this.progressText = this.$t('requirementAnalysis.taskCreated')
        if (this.globalOutputMode === 'stream') this.startStreamingProgress()
        else this.startPolling()
      } catch (error) {
        if (this.disposed || this.runRevision !== revision) return
        const existingTaskId = error.response?.data?.task_id
        if (existingTaskId) {
          this.rememberSubmission(null)
          this.currentTaskId = existingTaskId
          this.rememberTask(existingTaskId)
          if (error.response.status === 409) {
            this.isSubmitting = false
            await this.restoreActiveTask(existingTaskId)
            ElMessage.warning(this.$t('requirementAnalysis.submissionRecovered'))
            return
          }
        } else if (submitted && (!error.response || error.response.status >= 500)) {
          // The server may have committed the task before the response was lost.
          this.isGenerating = false
          this.isSubmitting = false
          this.monitoringError = this.$t('requirementAnalysis.submissionUnconfirmed')
          await this.restorePendingSubmission()
          return
        } else if (submitted) this.rememberSubmission(null)
        this.taskStatus = 'failed'
        this.generationError = errorText(error, this.$t('requirementAnalysis.createTaskFailed'))
        this.progressText = this.$t('requirementAnalysis.statusFailed')
        this.isGenerating = false
      } finally {
        if (this.runRevision === revision) this.isSubmitting = false
      }
    },
    isCurrentTask(taskId, revision) { return !this.disposed && this.currentTaskId === taskId && this.runRevision === revision },
    cleanupProgress() {
      if (this.pollInterval) clearInterval(this.pollInterval)
      this.pollInterval = null
      if (this.eventSource) {
        this.eventSource.onmessage = null
        this.eventSource.onerror = null
        this.eventSource.close()
      }
      this.eventSource = null
    },
    async startStreamingProgress() {
      const taskId = this.currentTaskId
      const revision = this.runRevision
      try {
        const { data } = await api.post(`/requirement-analysis/testcase-generation/${taskId}/stream-ticket/`)
        if (!this.isCurrentTask(taskId, revision) || !this.isGenerating) return
        this.startStreamingProgressWithTicket(data.ticket)
      } catch {
        if (this.isCurrentTask(taskId, revision) && this.isGenerating) this.startPolling()
      }
    },
    startStreamingProgressWithTicket(ticket) {
      this.cleanupProgress()
      const taskId = this.currentTaskId
      const revision = this.runRevision
      const stream = new EventSource(`${window.location.origin}/api/requirement-analysis/testcase-generation/${encodeURIComponent(taskId)}/stream_progress/?ticket=${encodeURIComponent(ticket)}`, { withCredentials: true })
      this.eventSource = stream
      stream.onmessage = event => {
        if (!this.isCurrentTask(taskId, revision) || this.eventSource !== stream || !this.isGenerating) return
        try {
          const data = JSON.parse(event.data)
          if (data.type === 'progress') this.updateTaskProgress(data)
          else if (data.type === 'content') this.streamedContent += data.content || ''
          else if (data.type === 'review_content') this.streamedReviewContent += data.content || ''
          else if (data.type === 'final_content') this.finalTestCases += data.content || ''
          else if (data.type === 'status') {
            if (data.status === 'completed') this.fetchFinalResult(taskId, revision)
            else if (data.status === 'failed') this.handleGenerationError()
            else if (data.status === 'cancelled') this.finishCancelledTask()
          } else if (data.type === 'done' && !this.finalizingTaskId) this.startPolling()
        } catch { this.monitoringError = this.$t('requirementAnalysis.invalidProgressMessage') }
      }
      // Reconnecting a short-lived ticket can replay content. Resume from a snapshot instead.
      stream.onerror = () => {
        if (!this.isCurrentTask(taskId, revision) || this.eventSource !== stream || !this.isGenerating) return
        this.startPolling()
      }
    },
    startPolling(immediate = true) {
      if (!this.isGenerating || !this.currentTaskId || this.pollInterval) return
      if (this.eventSource) this.cleanupProgress()
      const taskId = this.currentTaskId
      const revision = this.runRevision
      this.pollInterval = setInterval(() => this.pollTask(taskId, revision), 3000)
      if (immediate) this.pollTask(taskId, revision)
    },
    async pollTask(taskId, revision) {
      if (!this.isCurrentTask(taskId, revision) || !this.isGenerating || this.pollInFlight) return
      this.pollInFlight = true
      try {
        const { data } = await api.get(`/requirement-analysis/testcase-generation/${taskId}/progress/`)
        if (!this.isCurrentTask(taskId, revision) || !this.isGenerating) return
        this.monitoringError = ''
        if (data.status === 'completed') await this.fetchFinalResult(taskId, revision)
        else if (data.status === 'failed') await this.handleGenerationError(data)
        else if (data.status === 'cancelled') this.finishCancelledTask()
        else this.updateTaskProgress(data)
      } catch (error) {
        if (this.isCurrentTask(taskId, revision)) this.monitoringError = errorText(error, this.$t('requirementAnalysis.checkProgressFailed'))
      } finally {
        if (this.isCurrentTask(taskId, revision)) this.pollInFlight = false
      }
    },
    updateTaskProgress(task) {
      if (!ACTIVE_STATUSES.includes(task.status)) return
      this.taskStatus = task.status
      this.progress = Math.max(0, Math.min(99, Number(task.progress) || 0))
      this.currentStep = task.status === 'pending' ? 1 : task.status === 'generating' ? 2 : 3
      const labels = { pending: 'requirementAnalysis.taskCreated', generating: 'requirementAnalysis.statusGenerating', reviewing: 'requirementAnalysis.statusReviewing', revising: 'requirementAnalysis.statusRevising' }
      this.progressText = this.$t(labels[task.status])
      if (task.generated_test_cases) this.streamedContent = task.generated_test_cases
      if (task.review_feedback) this.streamedReviewContent = task.review_feedback
      if (task.final_test_cases) this.finalTestCases = task.final_test_cases
    },
    async fetchFinalResult(taskId = this.currentTaskId, revision = this.runRevision) {
      if (!this.isCurrentTask(taskId, revision) || this.finalizingTaskId === taskId) return
      this.finalizingTaskId = taskId
      try {
        // The detail response owns canonical cases used by preview, export and adoption.
        const { data } = await api.get(`/requirement-analysis/testcase-generation/${taskId}/`)
        if (!this.isCurrentTask(taskId, revision) || !this.isGenerating) return
        if (data.status === 'failed') { await this.handleGenerationError(data); return }
        if (data.status === 'cancelled') { this.finishCancelledTask(); return }
        if (data.status !== 'completed') { this.updateTaskProgress(data); this.startPolling(); return }
        this.cleanupProgress()
        this.generationResult = data
        this.streamedContent = data.generated_test_cases || ''
        this.streamedReviewContent = data.review_feedback || ''
        this.finalTestCases = data.final_test_cases || ''
        this.isGenerating = false
        this.showResults = true
        this.taskStatus = 'completed'
        this.progress = 100
        this.currentStep = this.showReviewStep ? 4 : 3
        this.progressText = this.$t('requirementAnalysis.statusCompleted')
        this.monitoringError = ''
        this.contentTab = 'final'
        if (!this.hasShownCompletionMessage) {
          ElMessage.success(this.$t('requirementAnalysis.generateCompleteSuccess'))
          this.hasShownCompletionMessage = true
        }
      } catch (error) {
        if (this.isCurrentTask(taskId, revision)) {
          this.monitoringError = errorText(error, this.$t('requirementAnalysis.fetchResultFailed'))
          this.startPolling(false)
        }
      } finally {
        if (this.isCurrentTask(taskId, revision)) this.finalizingTaskId = null
      }
    },
    async handleGenerationError(failedTask = null) {
      const failedTaskId = this.currentTaskId
      const revision = this.runRevision
      this.isGenerating = false
      if (this.eventSource) { this.eventSource.close(); this.eventSource = null }
      if (this.pollInterval) { clearInterval(this.pollInterval); this.pollInterval = null }
      let task = failedTask
      let message = this.$t('requirementAnalysis.unknownError')
      if (!task && failedTaskId) {
        try { task = (await api.get(`/requirement-analysis/testcase-generation/${encodeURIComponent(failedTaskId)}/progress/`)).data } catch { /* Keep a persistent fallback if failure details are unavailable. */ }
      }
      if (this.currentTaskId !== failedTaskId || this.runRevision !== revision || this.disposed) return
      if (typeof task?.error_message === 'string' && task.error_message.trim()) message = task.error_message.trim()
      if (task?.generated_test_cases) this.streamedContent = task.generated_test_cases
      if (task?.review_feedback) this.streamedReviewContent = task.review_feedback
      this.taskStatus = 'failed'
      this.showResults = false
      this.progressText = this.$t('requirementAnalysis.statusFailed')
      this.generationError = message
    },
    async cancelGeneration() {
      if (this.isCancelling || !this.currentTaskId || !this.isGenerating) return
      const taskId = this.currentTaskId
      const revision = this.runRevision
      this.isCancelling = true
      try {
        await api.post(`/requirement-analysis/testcase-generation/${taskId}/cancel/`)
        if (!this.isCurrentTask(taskId, revision)) return
        this.finishCancelledTask()
        ElMessage.success(this.$t('requirementAnalysis.generationCancelled'))
      } catch (error) {
        if (!this.isCurrentTask(taskId, revision)) return
        this.monitoringError = errorText(error, this.$t('requirementAnalysis.cancelFailed'))
        // A task can finish between clicking cancel and the server receiving the request.
        this.pollTask(taskId, revision)
      } finally {
        if (this.isCurrentTask(taskId, revision)) this.isCancelling = false
      }
    },
    finishCancelledTask() {
      this.cleanupProgress()
      this.isGenerating = false
      this.showResults = false
      this.taskStatus = 'cancelled'
      this.progressText = this.$t('requirementAnalysis.generationCancelled')
      this.monitoringError = ''
    },
    retryMonitoring() {
      this.monitoringError = ''
      if (this.pendingSubmissionId) { this.restorePendingSubmission(); return }
      if (!this.currentTaskId) return
      if (this.isGenerating) {
        this.startPolling(false)
        this.pollTask(this.currentTaskId, this.runRevision)
      } else this.restoreActiveTask(this.currentTaskId)
    },
    async saveToTestCaseRecords() {
      if (this.isSaving || this.isSaved || !this.showResults || !this.canonicalCases.length) return
      const taskId = this.currentTaskId
      const revision = this.runRevision
      this.isSaving = true
      try {
        const { data } = await api.post(`/requirement-analysis/testcase-generation/${taskId}/save_to_records/`)
        if (!this.isCurrentTask(taskId, revision)) return
        if (Array.isArray(data.adopted_case_ids)) this.generationResult.adopted_case_ids = data.adopted_case_ids
        this.generationResult.is_saved_to_records = Boolean(this.generationResult.adopted_case_ids?.length || data.imported_count || data.already_saved)
        ElMessage.success(this.$t(data.already_saved ? 'requirementAnalysis.alreadySaved' : 'requirementAnalysis.saveSuccess', { count: data.imported_count || 0 }))
      } catch (error) {
        if (this.isCurrentTask(taskId, revision)) ElMessage.error(errorText(error, this.$t('requirementAnalysis.saveFailed')))
      } finally {
        if (this.isCurrentTask(taskId, revision)) this.isSaving = false
      }
    },
    downloadTestCases() {
      if (!this.canonicalCases.length) return
      try {
        const headers = ['excelTestCaseNumber', 'excelTestScenario', 'excelPrecondition', 'excelTestSteps', 'excelExpectedResult', 'excelPriority'].map(key => this.$t(`requirementAnalysis.${key}`))
        const rows = this.canonicalCases.map(testCase => [testCase.case_id, testCase.title, testCase.preconditions || '', (testCase.steps || []).join('\n'), testCase.expected_result, testCase.priority])
        const workbook = XLSX.utils.book_new()
        const sheet = XLSX.utils.aoa_to_sheet([headers, ...rows])
        sheet['!cols'] = [18, 36, 30, 50, 44, 12].map(wch => ({ wch }))
        XLSX.utils.book_append_sheet(workbook, sheet, this.$t('requirementAnalysis.testCaseSheetName'))
        XLSX.writeFile(workbook, this.$t('requirementAnalysis.excelFileName', { taskId: this.currentTaskId, date: new Date().toISOString().slice(0, 10) }))
      } catch (error) { ElMessage.error(errorText(error, this.$t('requirementAnalysis.downloadFailed'))) }
    },
    rememberTask(taskId) {
      if (!this.storageKey) return
      try {
        if (taskId) sessionStorage.setItem(this.storageKey, taskId)
        else sessionStorage.removeItem(this.storageKey)
      } catch { /* Storage may be unavailable; history remains accessible. */ }
    },
    rememberSubmission(submissionId) {
      this.pendingSubmissionId = submissionId || ''
      if (!this.storageKey) return
      try {
        if (submissionId) sessionStorage.setItem(`${this.storageKey}:pending`, submissionId)
        else sessionStorage.removeItem(`${this.storageKey}:pending`)
      } catch { /* Keep the in-memory key when storage is unavailable. */ }
    },
    async restorePendingSubmission() {
      const submissionId = this.pendingSubmissionId
      if (!submissionId || this.isRestoring) return
      const revision = this.runRevision
      this.isRestoring = true
      try {
        const { data } = await api.get('/requirement-analysis/testcase-generation/submission/', { params: { client_request_id: submissionId } })
        if (this.disposed || this.runRevision !== revision || this.pendingSubmissionId !== submissionId) return
        if (!data.task_id) throw new Error(this.$t('requirementAnalysis.submissionUnconfirmed'))
        this.rememberSubmission(null)
        this.monitoringError = ''
        await this.restoreActiveTask(data.task_id)
      } catch (error) {
        if (this.disposed || this.runRevision !== revision) return
        if ([400, 401, 403].includes(error.response?.status)) this.rememberSubmission(null)
        this.monitoringError = this.$t('requirementAnalysis.submissionUnconfirmed')
      } finally {
        if (this.runRevision === revision) this.isRestoring = false
      }
    },
    async restoreActiveTask(explicitTaskId) {
      let taskId = explicitTaskId
      if (!taskId && this.storageKey) {
        try { taskId = sessionStorage.getItem(this.storageKey) } catch { return }
      }
      if (!taskId || !/^[A-Za-z0-9_-]{1,100}$/.test(taskId)) return
      const revision = ++this.runRevision
      this.cleanupProgress()
      this.pollInFlight = false
      this.finalizingTaskId = null
      this.isRestoring = true
      this.currentTaskId = taskId
      this.rememberTask(taskId)
      try {
        const { data } = await api.get(`/requirement-analysis/testcase-generation/${taskId}/`)
        if (!this.isCurrentTask(taskId, revision)) return
        this.activeTitle = data.title
        this.manualInput.title = data.title || ''
        this.restoreRequirementInput(data)
        this.selectedProject = data.project || ''
        this.showReviewStep = Boolean(data.reviewer_model_config)
        this.globalOutputMode = data.output_mode || 'complete'
        this.isGenerating = true
        if (data.status === 'completed') await this.fetchFinalResult(taskId, revision)
        else if (data.status === 'failed') await this.handleGenerationError(data)
        else if (data.status === 'cancelled') this.finishCancelledTask()
        else {
          this.updateTaskProgress(data)
          // Resume from snapshots, avoiding duplicated streamed chunks after a reload.
          this.startPolling()
        }
      } catch (error) {
        if (!this.isCurrentTask(taskId, revision)) return
        this.isGenerating = false
        if ([401, 403, 404].includes(error.response?.status)) {
          this.currentTaskId = null
          this.rememberTask(null)
        } else this.monitoringError = errorText(error, this.$t('requirementAnalysis.restoreFailed'))
      } finally {
        if (this.runRevision === revision) this.isRestoring = false
      }
    },
    resetGeneration() {
      if (this.isGenerating || this.isSubmitting || this.isSaving) return
      this.runRevision++
      this.cleanupProgress()
      this.rememberTask(null)
      this.rememberSubmission(null)
      this.currentTaskId = null
      this.generationResult = null
      this.showResults = false
      this.generationError = ''
      this.monitoringError = ''
      this.streamedContent = ''
      this.streamedReviewContent = ''
      this.finalTestCases = ''
      this.finalizingTaskId = null
      this.pollInFlight = false
      this.progress = 0
      this.currentStep = 0
      if (this.$route?.query?.task) {
        const query = { ...this.$route.query }
        delete query.task
        this.$router.replace({ query })
      }
      this.checkConfigStatus()
    },
    navigateContentTabs(event, key) {
      const keys = this.contentTabs.map(item => item.key)
      const index = keys.indexOf(key)
      let next
      if (event.key === 'ArrowRight') next = keys[(index + 1) % keys.length]
      else if (event.key === 'ArrowLeft') next = keys[(index + keys.length - 1) % keys.length]
      else if (event.key === 'Home') next = keys[0]
      else if (event.key === 'End') next = keys[keys.length - 1]
      if (next) {
        event.preventDefault()
        this.contentTab = next
        this.$nextTick(() => document.getElementById(`generation-tab-${next}`)?.focus())
      }
    },
    formatMarkdown(content) { return renderSafeMarkdown(content || '', { allowTables: true }) },
  },
}
</script>

<style scoped>
.requirement-analysis { max-width: 1440px; margin: 0 auto; padding: 28px 32px 40px; color: var(--el-text-color-primary, #202b3d); }
.page-header, .generation-heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 24px; }
h1 { font-size: 26px; line-height: 1.4; margin: 0 0 8px; font-weight: 650; }
h2 { font-size: 17px; margin: 0; font-weight: 600; }
p { line-height: 1.7; }
.page-header p, .section-heading p { color: var(--el-text-color-secondary, #68778d); margin: 0; font-size: 14px; }
.workspace-grid { display: grid; grid-template-columns: minmax(0, 1fr) 310px; gap: 24px; align-items: start; }
.panel { background: var(--el-bg-color, white); border: 1px solid var(--el-border-color-light, #e5eaf1); border-radius: 12px; padding: 26px; }
.section-heading { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
.section-heading p { font-size: 13px; margin-top: 5px; }
.section-number { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; width: 30px; height: 30px; border-radius: 8px; background: var(--el-color-primary-light-9, #ecf5ff); color: var(--el-color-primary, #409eff); font-weight: 650; }
fieldset { padding: 0; border: 0; margin: 0; min-width: 0; }
.form-group { margin-bottom: 22px; }
.form-group > label { display: block; font-size: 14px; font-weight: 550; margin-bottom: 9px; }
.required, .field-error { color: var(--el-color-danger, #d74747); }
input:not([type=radio]):not([type=file]), select, textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--el-border-color, #d7dfe9); border-radius: 7px; padding: 11px 12px; background: var(--el-fill-color-blank, white); color: inherit; font: inherit; font-size: 14px; }
textarea { resize: vertical; min-height: 230px; line-height: 1.8; }
input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible, summary:focus-visible { outline: 2px solid var(--el-color-primary, #409eff); outline-offset: 3px; }
.field-hint, .input-meta { font-size: 12px; color: var(--el-text-color-secondary, #68778d); margin: 8px 0 0; line-height: 1.6; }
.field-error { font-size: 13px; margin-top: 8px; }
.input-meta { display: flex; justify-content: space-between; gap: 16px; }
.input-meta > span:last-child { white-space: nowrap; }
.source-selector { display: inline-flex; border: 1px solid var(--el-border-color-light, #e5eaf1); border-radius: 8px; padding: 4px; background: var(--el-fill-color-light, #f5f7fa); margin-bottom: 20px; }
.source-selector button, .content-tabs button { font: inherit; font-size: 14px; padding: 9px 18px; border: 0; border-radius: 5px; background: transparent; color: var(--el-text-color-secondary, #68778d); cursor: pointer; }
.source-selector .selected { background: var(--el-bg-color, white); color: var(--el-color-primary, #409eff); box-shadow: 0 1px 4px #192d4810; font-weight: 550; }
.button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 10px 16px; min-height: 40px; font: inherit; font-size: 13px; font-weight: 550; border: 1px solid transparent; border-radius: 7px; cursor: pointer; white-space: nowrap; }
.primary { background: var(--el-color-primary, #409eff); color: white; }
.secondary { border-color: var(--el-border-color, #d7dfe9); background: var(--el-bg-color, white); color: var(--el-text-color-regular, #47556c); }
.danger { border-color: var(--el-color-danger-light-5, #fab6b6); background: var(--el-color-danger-light-9, #fef0f0); color: var(--el-color-danger, #d74747); }
.button:disabled { cursor: not-allowed; opacity: .5; }
.button:not(:disabled):hover { filter: brightness(.97); }
.text-button { font: inherit; font-size: 13px; color: var(--el-color-primary, #409eff); background: transparent; border: 0; padding: 5px 0; cursor: pointer; }
.advanced-settings { border-top: 1px solid var(--el-border-color-light, #e5eaf1); padding: 18px 0; font-size: 13px; }
.advanced-settings summary { cursor: pointer; color: var(--el-text-color-regular, #47556c); }
.radio-row { display: flex; flex-wrap: wrap; gap: 24px; margin-top: 16px; }
.radio-row label { display: inline-flex; align-items: center; gap: 6px; }
input[type=radio] { accent-color: var(--el-color-primary, #409eff); }
.submit-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; border-top: 1px solid var(--el-border-color-light, #e5eaf1); padding-top: 20px; }
.submit-row .field-hint { margin: 0; }
.sidebar { display: grid; gap: 18px; }
.sidebar .panel { padding: 22px; }
.config-status { display: flex; align-items: center; gap: 8px; color: var(--el-color-warning, #a86a1d); font-size: 13px; margin: 0 0 18px; }
.config-status.ready { color: var(--el-color-success, #529b2e); }
.status-dot { width: 7px; height: 7px; background: currentColor; border-radius: 50%; }
.config-list { margin: 0 0 18px; font-size: 13px; }
.config-list > div { padding: 10px 0; border-bottom: 1px solid var(--el-border-color-lighter, #eef1f5); }
.config-list dt { font-size: 12px; color: var(--el-text-color-secondary, #68778d); margin-bottom: 4px; }
.config-list dd { margin: 0; overflow-wrap: anywhere; }
.config-list .missing { color: var(--el-color-danger, #d74747); }
.full-width { width: 100%; margin-bottom: 8px; }
.guidance-panel ol { padding-left: 18px; margin: 18px 0; font-size: 13px; line-height: 1.8; color: var(--el-text-color-regular, #47556c); }
.guidance-panel li + li { margin-top: 12px; }
.guidance-panel > p { font-size: 12px; color: var(--el-text-color-secondary, #68778d); margin-bottom: 0; border-top: 1px solid var(--el-border-color-light, #e5eaf1); padding-top: 14px; }
.file-input { display: none; }
.upload-area { display: flex; flex-direction: column; align-items: center; gap: 16px; text-align: center; border: 1px dashed var(--el-border-color, #d7dfe9); border-radius: 8px; padding: 44px 20px; background: var(--el-fill-color-lighter, #fafbfc); }
.upload-area strong { font-size: 14px; font-weight: 500; }
.drag-over { border-color: var(--el-color-primary, #409eff); background: var(--el-color-primary-light-9, #ecf5ff); }
.file-name { overflow-wrap: anywhere; max-width: 100%; }
.notice { border-radius: 8px; padding: 14px 18px; margin-bottom: 20px; font-size: 13px; border: 1px solid; }
.notice p { margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.notice.error { background: var(--el-color-danger-light-9, #fef0f0); color: var(--el-color-danger, #d74747); border-color: var(--el-color-danger-light-7, #fbc4c4); }
.notice.warning { background: var(--el-color-warning-light-9, #fdf6ec); color: var(--el-color-warning-dark-2, #b88230); border-color: var(--el-color-warning-light-7, #f3d19e); }
.generation-heading { margin-bottom: 20px; }
.generation-heading h2 { margin-top: 8px; font-size: 21px; overflow-wrap: anywhere; }
.eyebrow { font-size: 12px; color: var(--el-text-color-secondary, #68778d); }
.status-badge { padding: 7px 12px; border-radius: 6px; font-size: 12px; color: var(--el-color-primary, #409eff); background: var(--el-color-primary-light-9, #ecf5ff); white-space: nowrap; }
.status-badge.completed { background: var(--el-color-success-light-9, #f0f9eb); color: var(--el-color-success-dark-2, #529b2e); }
.status-badge.failed, .status-badge.cancelled { background: var(--el-fill-color, #f0f2f5); color: var(--el-text-color-regular, #47556c); }
.progress-meta { display: flex; justify-content: space-between; gap: 16px; color: var(--el-text-color-secondary, #68778d); font-size: 13px; }
progress { width: 100%; height: 8px; border: 0; border-radius: 8px; overflow: hidden; appearance: none; margin: 10px 0 18px; accent-color: var(--el-color-primary, #409eff); }
progress::-webkit-progress-bar { background: var(--el-fill-color, #f0f2f5); border-radius: 8px; }
progress::-webkit-progress-value { background: var(--el-color-primary, #409eff); border-radius: 8px; }
.progress-steps { display: flex; justify-content: space-between; padding: 0; margin: 0 0 28px; gap: 8px; list-style: none; counter-reset: step; }
.progress-steps li { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--el-text-color-placeholder, #9ca7b7); }
.progress-steps li::before { counter-increment: step; content: counter(step); display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border-radius: 50%; background: var(--el-fill-color, #f0f2f5); }
.progress-steps .active { color: var(--el-color-primary, #409eff); }
.progress-steps .active::before { background: var(--el-color-primary-light-9, #ecf5ff); }
.completion-summary { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; padding: 20px; margin-bottom: 20px; background: var(--el-fill-color-lighter, #fafbfc); border-radius: 8px; }
.completion-summary strong { font-size: 16px; }
.completion-summary p { margin: 5px 0 0; font-size: 12px; color: var(--el-text-color-secondary, #68778d); }
.action-row { display: flex; flex-wrap: wrap; gap: 10px; }
.content-tabs { display: flex; gap: 12px; border-bottom: 1px solid var(--el-border-color-light, #e5eaf1); }
.content-tabs button { border-radius: 0; border-bottom: 2px solid transparent; padding: 12px 8px; }
.content-tabs .selected { color: var(--el-color-primary, #409eff); border-bottom-color: var(--el-color-primary, #409eff); font-weight: 550; }
.content-panel { min-height: 240px; padding-top: 20px; }
.empty-state { display: flex; align-items: center; justify-content: center; min-height: 220px; color: var(--el-text-color-secondary, #68778d); font-size: 14px; }
.case-table-wrapper { overflow: auto; max-height: 620px; }
.case-table { border-collapse: collapse; width: 100%; font-size: 13px; text-align: left; line-height: 1.7; }
.case-table th { position: sticky; top: 0; background: var(--el-fill-color-light, #f5f7fa); font-weight: 550; color: var(--el-text-color-regular, #47556c); white-space: nowrap; }
.case-table td, .case-table th { padding: 13px 14px; border-bottom: 1px solid var(--el-border-color-lighter, #eef1f5); vertical-align: top; }
.case-table td { white-space: pre-wrap; overflow-wrap: anywhere; min-width: 140px; }
.case-table td:first-child, .case-table td:last-child { min-width: 75px; }
.case-table td:nth-child(4), .case-table td:nth-child(5) { min-width: 190px; }
.priority-badge { font-size: 12px; font-weight: 600; color: var(--el-color-primary, #409eff); }
.pagination { display: flex; justify-content: flex-end; align-items: center; gap: 16px; font-size: 13px; padding-top: 16px; }
.markdown-content { overflow: auto; max-height: 620px; font-size: 14px; line-height: 1.8; overflow-wrap: anywhere; }
.markdown-content :deep(table) { border-collapse: collapse; min-width: 100%; }
.markdown-content :deep(th), .markdown-content :deep(td) { padding: 10px 14px; border: 1px solid var(--el-border-color-light, #e5eaf1); text-align: left; vertical-align: top; min-width: 100px; }
.markdown-content :deep(th) { background: var(--el-fill-color-light, #f5f7fa); }
.markdown-content :deep(pre) { background: var(--el-fill-color-light, #f5f7fa); padding: 16px; border-radius: 6px; overflow: auto; }
.markdown-content :deep(h1), .markdown-content :deep(h2), .markdown-content :deep(h3) { line-height: 1.6; margin: 16px 0 8px; }
.generation-footer { display: flex; justify-content: space-between; align-items: center; gap: 20px; border-top: 1px solid var(--el-border-color-light, #e5eaf1); margin-top: 22px; padding-top: 18px; }
.generation-footer p { margin: 0; }
@media (max-width: 1100px) { .requirement-analysis { padding: 24px 20px; } .workspace-grid { grid-template-columns: minmax(0, 1fr) 270px; gap: 18px; } .panel { padding: 20px; } }
@media (max-width: 800px) { .workspace-grid { grid-template-columns: minmax(0, 1fr); } .sidebar { grid-template-columns: repeat(2, minmax(0, 1fr)); } .requirement-analysis { padding: 18px 12px; } .page-header { align-items: flex-start; } h1 { font-size: 22px; } }
@media (max-width: 560px) { .sidebar { grid-template-columns: minmax(0, 1fr); } .page-header { flex-wrap: wrap; } .input-meta { flex-wrap: wrap; gap: 4px; } .submit-row, .generation-footer { align-items: stretch; flex-direction: column; } .generation-heading { align-items: flex-start; } .progress-steps li { font-size: 11px; gap: 4px; } .progress-steps li::before { display: none; } .action-row { width: 100%; } .action-row .button { flex: 1; } .source-selector button { padding: 9px 14px; } .content-tabs { gap: 6px; } .content-tabs button { font-size: 12px; } }
</style>
