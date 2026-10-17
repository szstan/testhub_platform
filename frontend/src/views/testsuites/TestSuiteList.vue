<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">{{ $t('testSuite.title') }}</h1>
      <div class="header-actions">
        <el-button :icon="Refresh" @click="fetchSuites" :loading="loading">
          {{ $t('testSuite.refresh') }}
        </el-button>
        <el-button type="primary" :icon="Plus" @click="openCreateDialog" :disabled="projects.length === 0">
          {{ $t('testSuite.newSuite') }}
        </el-button>
      </div>
    </div>

    <div class="card-container">
      <div class="filter-bar">
        <el-row :gutter="16">
          <el-col :xs="24" :sm="10" :md="7">
            <el-input
              v-model="searchText"
              clearable
              :placeholder="$t('testSuite.searchPlaceholder')"
              @keyup.enter="fetchSuites"
              @clear="fetchSuites"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
          </el-col>
          <el-col :xs="24" :sm="10" :md="7">
            <el-select
              v-model="projectFilter"
              clearable
              filterable
              :placeholder="$t('testSuite.projectFilter')"
              @change="fetchSuites"
            >
              <el-option
                v-for="project in projects"
                :key="project.id"
                :label="project.name"
                :value="project.id"
              />
            </el-select>
          </el-col>
        </el-row>
      </div>

      <el-alert
        v-if="projects.length === 0 && !projectsLoading"
        :title="$t('testSuite.noProjects')"
        type="info"
        :closable="false"
        show-icon
      />

      <el-table v-else :data="suites" v-loading="loading" row-key="id" style="width: 100%">
        <el-table-column prop="name" :label="$t('testSuite.name')" min-width="220" show-overflow-tooltip />
        <el-table-column :label="$t('testSuite.project')" min-width="160">
          <template #default="{ row }">
            {{ row.project?.name || '-' }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('testSuite.description')" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.description || '-' }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('testSuite.cases')" width="100" align="center">
          <template #default="{ row }">
            <el-tag type="info">{{ row.test_case_count ?? row.testcases?.length ?? 0 }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('testSuite.author')" width="130">
          <template #default="{ row }">
            {{ row.author?.username || '-' }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('testSuite.updatedAt')" width="180">
          <template #default="{ row }">
            {{ formatDate(row.updated_at) }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('testSuite.actions')" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :icon="List" @click="openCasesDialog(row)">
              {{ $t('testSuite.manageCases') }}
            </el-button>
            <el-button link :icon="Edit" @click="openEditDialog(row)">
              {{ $t('common.edit') }}
            </el-button>
            <el-button link type="danger" :icon="Delete" @click="deleteSuite(row)">
              {{ $t('common.delete') }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="$t('testSuite.empty')" />
        </template>
      </el-table>

      <div v-if="total > pageSize" class="pagination-container">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="fetchSuites"
        />
      </div>
    </div>

    <el-dialog
      v-model="suiteDialogVisible"
      :title="editingSuite ? $t('testSuite.editTitle') : $t('testSuite.createTitle')"
      width="620px"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <el-form ref="suiteFormRef" :model="suiteForm" :rules="suiteRules" label-width="100px">
        <el-form-item :label="$t('testSuite.project')" prop="project_id">
          <el-select v-model="suiteForm.project_id" filterable :placeholder="$t('testSuite.selectProject')" :disabled="Boolean(editingSuite)">
            <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('testSuite.name')" prop="name">
          <el-input v-model="suiteForm.name" :placeholder="$t('testSuite.namePlaceholder')" maxlength="200" show-word-limit />
        </el-form-item>
        <el-form-item :label="$t('testSuite.description')" prop="description">
          <el-input v-model="suiteForm.description" type="textarea" :rows="3" :placeholder="$t('testSuite.descriptionPlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('testSuite.cases')">
          <el-select
            v-model="suiteForm.test_case_ids"
            multiple
            filterable
            clearable
            collapse-tags
            collapse-tags-tooltip
            :loading="casesLoading"
            :placeholder="$t('testSuite.casesPlaceholder')"
            @focus="loadFormCases"
          >
            <el-option v-for="testCase in formCases" :key="testCase.id" :label="testCase.title" :value="testCase.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="suiteDialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submitSuite">
          {{ editingSuite ? $t('common.update') : $t('common.create') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="casesDialogVisible"
      :title="$t('testSuite.manageCases')"
      width="720px"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <el-form label-width="100px">
        <el-form-item :label="$t('testSuite.cases')">
          <el-select
            v-model="selectedCaseIds"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :loading="casesLoading"
            :placeholder="$t('testSuite.casesPlaceholder')"
          >
            <el-option v-for="testCase in caseOptions" :key="testCase.id" :label="testCase.title" :value="testCase.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <el-alert :title="$t('testSuite.orderHint')" type="info" :closable="false" show-icon />
      <template #footer>
        <el-button @click="casesDialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="savingCases" :icon="Sort" @click="saveCases">
          {{ $t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Edit, List, Plus, Refresh, Search, Sort } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import api from '@/utils/api'
import {
  addTestSuiteCases,
  createTestSuite,
  deleteTestSuite,
  listTestSuiteCases,
  listTestSuites,
  removeTestSuiteCase,
  reorderTestSuiteCases,
  updateTestSuite
} from '@/api/test-suites'

const { t } = useI18n()
const loading = ref(false)
const projectsLoading = ref(false)
const casesLoading = ref(false)
const submitting = ref(false)
const savingCases = ref(false)
const suiteDialogVisible = ref(false)
const casesDialogVisible = ref(false)
const editingSuite = ref(null)
const selectedSuite = ref(null)
const suiteFormRef = ref()
const projects = ref([])
const suites = ref([])
const formCases = ref([])
const caseOptions = ref([])
const selectedCaseIds = ref([])
const currentCaseIds = ref([])
const searchText = ref('')
const projectFilter = ref('')
const currentPage = ref(1)
const pageSize = 20
const total = ref(0)

const suiteForm = reactive({
  project_id: '',
  name: '',
  description: '',
  test_case_ids: []
})

const suiteRules = computed(() => ({
  project_id: [{ required: true, message: t('testSuite.selectProject'), trigger: 'change' }],
  name: [{ required: true, message: t('testSuite.nameRequired'), trigger: 'blur' }]
}))

const unwrapResults = (data) => data?.results ?? data ?? []

const fetchProjects = async () => {
  projectsLoading.value = true
  try {
    const response = await api.get('/projects/', { params: { page_size: 100 } })
    projects.value = unwrapResults(response.data)
  } catch (error) {
    ElMessage.error(t('testSuite.loadProjectsFailed'))
  } finally {
    projectsLoading.value = false
  }
}

const fetchSuites = async () => {
  loading.value = true
  try {
    const response = await listTestSuites({
      page: currentPage.value,
      page_size: pageSize,
      search: searchText.value || undefined,
      project: projectFilter.value || undefined
    })
    suites.value = unwrapResults(response.data)
    total.value = response.data?.count ?? suites.value.length
  } catch (error) {
    ElMessage.error(t('testSuite.loadFailed'))
  } finally {
    loading.value = false
  }
}

const loadCasesForProject = async (projectId, target) => {
  if (!projectId) {
    target.value = []
    return
  }
  casesLoading.value = true
  try {
    const response = await api.get('/testcases/', {
      params: { project: projectId, page_size: 100, ordering: 'title' }
    })
    target.value = unwrapResults(response.data)
  } catch (error) {
    ElMessage.error(t('testSuite.loadCasesFailed'))
  } finally {
    casesLoading.value = false
  }
}

const loadFormCases = () => loadCasesForProject(suiteForm.project_id, formCases)

const resetSuiteForm = () => {
  suiteForm.project_id = ''
  suiteForm.name = ''
  suiteForm.description = ''
  suiteForm.test_case_ids = []
  formCases.value = []
  editingSuite.value = null
  suiteFormRef.value?.clearValidate()
}

const openCreateDialog = async () => {
  resetSuiteForm()
  suiteDialogVisible.value = true
  if (projects.value.length === 0) await fetchProjects()
  if (!suiteForm.project_id && projects.value.length > 0) {
    suiteForm.project_id = projects.value[0].id
    await loadFormCases()
  }
}

const openEditDialog = async (suite) => {
  editingSuite.value = suite
  suiteForm.project_id = suite.project?.id || suite.project_id
  suiteForm.name = suite.name
  suiteForm.description = suite.description || ''
  suiteForm.test_case_ids = (suite.suite_cases || suite.testcases || [])
    .map(item => Number(item.testcase_id ?? item.id))
    .filter(Number.isInteger)
  suiteDialogVisible.value = true
  await loadFormCases()
}

const submitSuite = async () => {
  if (!suiteFormRef.value) return
  try {
    await suiteFormRef.value.validate()
  } catch (error) {
    return
  }

  submitting.value = true
  try {
    const payload = {
      project_id: Number(suiteForm.project_id),
      name: suiteForm.name.trim(),
      description: suiteForm.description.trim(),
      test_case_ids: suiteForm.test_case_ids.map(Number)
    }
    if (editingSuite.value) {
      await updateTestSuite(editingSuite.value.id, payload)
      ElMessage.success(t('testSuite.updateSuccess'))
    } else {
      await createTestSuite(payload)
      ElMessage.success(t('testSuite.createSuccess'))
    }
    suiteDialogVisible.value = false
    resetSuiteForm()
    await fetchSuites()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || t('testSuite.saveFailed'))
  } finally {
    submitting.value = false
  }
}

const openCasesDialog = async (suite) => {
  selectedSuite.value = suite
  casesDialogVisible.value = true
  casesLoading.value = true
  try {
    const [caseResponse, suiteCaseResponse] = await Promise.all([
      api.get('/testcases/', { params: { project: suite.project?.id, page_size: 100, ordering: 'title' } }),
      listTestSuiteCases(suite.id)
    ])
    caseOptions.value = unwrapResults(caseResponse.data)
    const rows = unwrapResults(suiteCaseResponse.data)
    currentCaseIds.value = rows.map(row => Number(row.testcase_id)).filter(Number.isInteger)
    selectedCaseIds.value = [...currentCaseIds.value]
  } catch (error) {
    ElMessage.error(t('testSuite.loadCasesFailed'))
    casesDialogVisible.value = false
  } finally {
    casesLoading.value = false
  }
}

const saveCases = async () => {
  if (!selectedSuite.value) return
  const desiredIds = selectedCaseIds.value.map(Number)
  const currentIds = currentCaseIds.value.map(Number)
  const additions = desiredIds.filter(id => !currentIds.includes(id))
  const removals = currentIds.filter(id => !desiredIds.includes(id))
  savingCases.value = true
  try {
    if (additions.length > 0) await addTestSuiteCases(selectedSuite.value.id, additions)
    await Promise.all(removals.map(id => removeTestSuiteCase(selectedSuite.value.id, id)))
    await reorderTestSuiteCases(
      selectedSuite.value.id,
      desiredIds.map((testCaseId, order) => ({ test_case_id: testCaseId, order }))
    )
    ElMessage.success(t('testSuite.casesSaved'))
    casesDialogVisible.value = false
    await fetchSuites()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || t('testSuite.saveCasesFailed'))
  } finally {
    savingCases.value = false
  }
}

const deleteSuite = async (suite) => {
  try {
    await ElMessageBox.confirm(t('testSuite.deleteConfirm'), t('common.warning'), {
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
      type: 'warning'
    })
    await deleteTestSuite(suite.id)
    ElMessage.success(t('testSuite.deleteSuccess'))
    await fetchSuites()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(t('testSuite.deleteFailed'))
    }
  }
}

const formatDate = (value) => value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-'

onMounted(async () => {
  await fetchProjects()
  await fetchSuites()
})
</script>

<style scoped>
.header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.filter-bar {
  margin-bottom: 18px;
}

.filter-bar .el-select,
.suite-dialog .el-select {
  width: 100%;
}

.el-form-item .el-select {
  width: 100%;
}

.pagination-container {
  display: flex;
  justify-content: flex-end;
  margin-top: 18px;
}

@media (max-width: 768px) {
  .page-header,
  .header-actions {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
