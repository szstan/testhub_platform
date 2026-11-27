<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">{{ $t('uiAutomation.project.title') }}</h1>
      <el-button type="primary" @click="showCreateDialog = true">
        <el-icon><Plus /></el-icon>
        {{ $t('uiAutomation.project.newProject') }}
      </el-button>
    </div>
    
    <div class="card-container">
      <div class="filter-bar">
        <el-row :gutter="20">
          <el-col :span="6">
            <el-input
              v-model="searchText"
              :placeholder="$t('uiAutomation.project.searchPlaceholder')"
              clearable
              @input="handleSearch"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
          </el-col>
          <el-col :span="4">
            <el-select v-model="statusFilter" :placeholder="$t('uiAutomation.project.statusFilter')" clearable @change="handleFilter">
              <el-option :label="$t('uiAutomation.status.notStarted')" value="NOT_STARTED" />
              <el-option :label="$t('uiAutomation.status.inProgress')" value="IN_PROGRESS" />
              <el-option :label="$t('uiAutomation.status.completed')" value="COMPLETED" />
            </el-select>
          </el-col>
        </el-row>
      </div>
      
      <el-table :data="projects" v-loading="loading" style="width: 100%">
        <el-table-column prop="name" :label="$t('uiAutomation.project.projectName')" min-width="200">
          <template #default="{ row }">
            <el-link @click="goToProjectDetail(row.id)" type="primary">
              {{ row.name }}
            </el-link>
          </template>
        </el-table-column>
        <el-table-column prop="description" :label="$t('uiAutomation.common.description')" min-width="300" show-overflow-tooltip />
        <el-table-column prop="status" :label="$t('uiAutomation.common.status')" width="100">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)">{{ getStatusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="base_url" :label="$t('uiAutomation.project.baseUrl')" min-width="200" show-overflow-tooltip />
        <el-table-column prop="owner.username" :label="$t('uiAutomation.project.owner')" width="100" />
        <el-table-column prop="created_at" :label="$t('uiAutomation.common.createTime')" width="180" :formatter="formatDate" />
        <el-table-column prop="updated_at" :label="$t('uiAutomation.common.updateTime')" width="180" :formatter="formatDate" />
        <el-table-column :label="$t('uiAutomation.common.operation')" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" @click="goToProjectDetail(row.id)">
              <el-icon><View /></el-icon>
              {{ $t('uiAutomation.common.view') }}
            </el-button>
            <el-button size="small" @click="editProject(row)">
              <el-icon><Edit /></el-icon>
              {{ $t('uiAutomation.common.edit') }}
            </el-button>
            <el-button size="small" type="danger" @click="deleteProject(row.id)">
              <el-icon><Delete /></el-icon>
              {{ $t('uiAutomation.common.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      
      <div class="pagination-container">
        <el-pagination
          v-model:current-page="pagination.currentPage"
          v-model:page-size="pagination.pageSize"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          :total="total"
          @size-change="handleSizeChange"
          @current-change="handleCurrentChange"
        />
      </div>
    </div>
    
    <!-- 创建项目对话框 -->
    <el-dialog v-model="showCreateDialog" :title="$t('uiAutomation.project.createProject')" width="min(500px, calc(100vw - 32px))" :close-on-click-modal="false">
      <el-form ref="createFormRef" :model="createForm" :rules="createFormRules" label-width="80px">
        <el-form-item :label="$t('uiAutomation.project.projectName')" prop="name">
          <el-input v-model="createForm.name" :placeholder="$t('uiAutomation.project.rules.nameRequired')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.project.projectDesc')" prop="description">
          <el-input v-model="createForm.description" type="textarea" :placeholder="$t('uiAutomation.project.projectDesc')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.common.status')" prop="status">
          <el-select v-model="createForm.status" :placeholder="$t('uiAutomation.project.rules.selectStatus')">
            <el-option :label="$t('uiAutomation.status.notStarted')" value="NOT_STARTED" />
            <el-option :label="$t('uiAutomation.status.inProgress')" value="IN_PROGRESS" />
            <el-option :label="$t('uiAutomation.status.completed')" value="COMPLETED" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.project.baseUrl')" prop="base_url">
          <el-input
            v-model="createForm.base_url"
            :placeholder="$t('uiAutomation.project.baseUrlPlaceholder')"
            @input="handleCreateUrlInput"
          >
            <template #prepend>
              <el-select v-model="urlProtocol" :placeholder="$t('uiAutomation.project.urlProtocol')" style="width: 100px">
                <el-option label="https://" value="https://" />
                <el-option label="http://" value="http://" />
              </el-select>
            </template>
          </el-input>
          <div style="color: #909399; font-size: 12px; margin-top: 4px;">
            {{ $t('uiAutomation.project.baseUrlHint') }}
          </div>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.project.startDate')" prop="start_date">
          <el-date-picker v-model="createForm.start_date" type="date" :placeholder="$t('uiAutomation.project.selectDate')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.project.endDate')" prop="end_date">
          <el-date-picker v-model="createForm.end_date" type="date" :placeholder="$t('uiAutomation.project.selectDate')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showCreateDialog = false">{{ $t('uiAutomation.common.cancel') }}</el-button>
          <el-button type="primary" :loading="creatingProject" @click="handleCreate">{{ $t('uiAutomation.common.confirm') }}</el-button>
        </span>
      </template>
    </el-dialog>
    
    <!-- 编辑项目对话框 -->
    <el-dialog v-model="showEditDialog" :title="$t('uiAutomation.project.editProject')" width="500px" :close-on-click-modal="false">
      <el-form ref="editFormRef" :model="editForm" :rules="editFormRules" label-width="80px">
        <el-form-item :label="$t('uiAutomation.project.projectName')" prop="name">
          <el-input v-model="editForm.name" :placeholder="$t('uiAutomation.project.rules.nameRequired')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.project.projectDesc')" prop="description">
          <el-input v-model="editForm.description" type="textarea" :placeholder="$t('uiAutomation.project.projectDesc')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.common.status')" prop="status">
          <el-select v-model="editForm.status" :placeholder="$t('uiAutomation.project.rules.selectStatus')">
            <el-option :label="$t('uiAutomation.status.notStarted')" value="NOT_STARTED" />
            <el-option :label="$t('uiAutomation.status.inProgress')" value="IN_PROGRESS" />
            <el-option :label="$t('uiAutomation.status.completed')" value="COMPLETED" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.project.baseUrl')" prop="base_url">
          <el-input
            v-model="editForm.base_url"
            :placeholder="$t('uiAutomation.project.baseUrlPlaceholder')"
            @input="handleEditUrlInput"
          >
            <template #prepend>
              <el-select v-model="editUrlProtocol" :placeholder="$t('uiAutomation.project.urlProtocol')" style="width: 100px">
                <el-option label="https://" value="https://" />
                <el-option label="http://" value="http://" />
              </el-select>
            </template>
          </el-input>
          <div style="color: #909399; font-size: 12px; margin-top: 4px;">
            {{ $t('uiAutomation.project.baseUrlHint') }}
          </div>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.project.startDate')" prop="start_date">
          <el-date-picker v-model="editForm.start_date" type="date" :placeholder="$t('uiAutomation.project.selectDate')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.project.endDate')" prop="end_date">
          <el-date-picker v-model="editForm.end_date" type="date" :placeholder="$t('uiAutomation.project.selectDate')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showEditDialog = false">{{ $t('uiAutomation.common.cancel') }}</el-button>
          <el-button type="primary" @click="handleEdit">{{ $t('uiAutomation.common.confirm') }}</el-button>
        </span>
      </template>
    </el-dialog>
    
    <!-- 项目详情弹框 -->
    <el-dialog v-model="showDetailDialog" :title="$t('uiAutomation.project.projectDetail')" width="600px">
      <div v-if="currentProjectDetail" class="project-detail">
        <el-descriptions bordered column="1">
          <el-descriptions-item :label="$t('uiAutomation.project.projectName')">{{ currentProjectDetail.name }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.project.projectDesc')" :span="2">{{ currentProjectDetail.description || $t('uiAutomation.project.noDescription') }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.common.status')">
            <el-tag :type="getStatusType(currentProjectDetail.status)">
              {{ getStatusText(currentProjectDetail.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.project.baseUrl')">{{ currentProjectDetail.base_url }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.project.owner')">{{ currentProjectDetail.owner?.username || $t('uiAutomation.project.none') }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.project.startDate')">{{ currentProjectDetail.start_date ? formatDate(null, null, currentProjectDetail.start_date) : $t('uiAutomation.project.notSet') }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.project.endDate')">{{ currentProjectDetail.end_date ? formatDate(null, null, currentProjectDetail.end_date) : $t('uiAutomation.project.notSet') }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.common.createTime')">{{ formatDate(null, null, currentProjectDetail.created_at) }}</el-descriptions-item>
          <el-descriptions-item :label="$t('uiAutomation.common.updateTime')">{{ formatDate(null, null, currentProjectDetail.updated_at) }}</el-descriptions-item>
        </el-descriptions>
      </div>
      <div v-else class="text-center text-gray-500">
        {{ $t('uiAutomation.common.loading') }}
      </div>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showDetailDialog = false">{{ $t('uiAutomation.common.close') }}</el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { normalizeUiId } from '@/utils/ui-project-context.mjs'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, View, Edit, Delete } from '@element-plus/icons-vue'
import { getUiProjects, createUiProject, updateUiProject, deleteUiProject } from '@/api/ui_automation'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const creatingProject = ref(false)
const guidedCreation = computed(() => route.query.setup === 'project')

// 项目数据
const projects = ref([])
const loading = ref(false)
const total = ref(0)
const pagination = reactive({
  currentPage: 1,
  pageSize: 10
})

// 搜索和筛选
const searchText = ref('')
const statusFilter = ref('')

// URL 协议选择
const urlProtocol = ref('https://')
const editUrlProtocol = ref('https://')

// 表单相关
const showCreateDialog = ref(false)
const showEditDialog = ref(false)
const createFormRef = ref(null)
const editFormRef = ref(null)
const currentEditId = ref(null)

// 表单数据
const createForm = reactive({
  name: '',
  description: '',
  status: 'IN_PROGRESS',
  base_url: '',
  start_date: null,
  end_date: null
})

const editForm = reactive({
  name: '',
  description: '',
  status: 'IN_PROGRESS',
  base_url: '',
  start_date: null,
  end_date: null
})

// 粘贴完整 URL 时同步协议选择，避免界面显示重复的协议。
const syncUrlInput = (value, form, protocol) => {
  const match = (value || '').match(/^\s*(https?:\/\/)/i)
  if (match) {
    protocol.value = match[1].toLowerCase()
    form.base_url = value.slice(match[0].length)
  }
}

const handleCreateUrlInput = value => syncUrlInput(value, createForm, urlProtocol)
const handleEditUrlInput = value => syncUrlInput(value, editForm, editUrlProtocol)

const completeUrlPattern = /^[a-z][a-z0-9+.-]*:\/\/[^/\s?#]+/i
const protocolLessHostWithPortPattern = /^(?:\[[\da-f:.]+\]|[a-z\d.-]+):\d+(?:[/?#]|$)/i
const knownProtocolNames = new Set(['http', 'https', 'ftp', 'ws', 'wss', 'file', 'ssh', 'smtp', 'tcp', 'udp'])

const hasMalformedExplicitScheme = value => {
  const trimmed = (value || '').trim()
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(trimmed)
  if (!scheme || completeUrlPattern.test(trimmed)) return false
  if (knownProtocolNames.has(scheme[1].toLowerCase())) return true
  return !protocolLessHostWithPortPattern.test(trimmed)
}

// Keep complete URLs as entered, reject incomplete schemes, and prefix host-only inputs.
const buildBaseUrl = (value, protocol) => {
  const trimmed = (value || '').trim()
  if (!trimmed) return ''
  return completeUrlPattern.test(trimmed) || hasMalformedExplicitScheme(trimmed)
    ? trimmed
    : protocol + trimmed
}

const makeFormRules = protocol => ({
  name: [
    { required: true, message: t('uiAutomation.project.rules.nameRequired'), trigger: 'blur' },
    { min: 2, max: 200, message: t('uiAutomation.project.rules.nameLength'), trigger: 'blur' }
  ],
  base_url: [
    { required: true, message: t('uiAutomation.project.rules.baseUrlRequired'), trigger: 'blur' },
    {
      validator: (rule, value, callback) => {
        const trimmed = (value || '').trim()
        if (hasMalformedExplicitScheme(trimmed)) {
          callback(new Error(t('uiAutomation.project.rules.baseUrlMalformedProtocol')))
          return
        }
        const baseUrl = buildBaseUrl(trimmed, protocol.value)
        if (!baseUrl) {
          callback(new Error(t('uiAutomation.project.rules.baseUrlRequired')))
          return
        }
        // Django URLField limits the complete URL, including the selected protocol, to 200 characters.
        if (Array.from(baseUrl).length > 200) {
          callback(new Error(t('uiAutomation.project.rules.baseUrlLength')))
          return
        }
        try {
          const parsed = new URL(baseUrl)
          if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
            callback(new Error(t('uiAutomation.project.rules.baseUrlHttpOnly')))
          } else if (parsed.username || parsed.password || /^(?:https?):\/\/[^/?#]*@/i.test(baseUrl)) {
            callback(new Error(t('uiAutomation.project.rules.baseUrlNoCredentials')))
          } else {
            callback()
          }
        } catch (e) {
          callback(new Error(t('uiAutomation.project.rules.baseUrlInvalid')))
        }
      },
      trigger: 'blur'
    }
  ]
})

const createFormRules = computed(() => makeFormRules(urlProtocol))
const editFormRules = computed(() => makeFormRules(editUrlProtocol))

// 格式化日期
const formatDate = (row, column, cellValue) => {
  if (!cellValue) return ''
  const dateOnly = typeof cellValue === 'string' && /^(\d{4})-(\d{2})-(\d{2})$/.exec(cellValue)
  if (dateOnly) {
    return new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3])).toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    })
  }
  return new Date(cellValue).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
}

// 获取状态样式
const getStatusType = (status) => {
  const statusMap = {
    'NOT_STARTED': 'warning',
    'IN_PROGRESS': 'primary',
    'COMPLETED': 'success'
  }
  return statusMap[status] || 'default'
}

// 获取状态文本
const getStatusText = (status) => {
  const statusKey = {
    'NOT_STARTED': 'notStarted',
    'IN_PROGRESS': 'inProgress',
    'COMPLETED': 'completed'
  }[status]
  return statusKey ? t(`uiAutomation.status.${statusKey}`) : status
}

// 加载项目列表
const loadProjects = async () => {
  loading.value = true
  try {
    const params = {
      page: pagination.currentPage,
      page_size: pagination.pageSize
    }
    
    // 添加搜索条件
    if (searchText.value) {
      params.search = searchText.value
    }
    
    // 添加筛选条件
    if (statusFilter.value) {
      params.status = statusFilter.value
    }
    
    const response = await getUiProjects(params)
    projects.value = response.data.results || response.data
    total.value = response.data.count || projects.value.length
  } catch (error) {
    ElMessage.error(t('uiAutomation.project.messages.loadFailed'))
    console.error('获取项目列表失败:', error)
  } finally {
    loading.value = false
  }
}

// 搜索处理
const handleSearch = () => {
  pagination.currentPage = 1
  loadProjects()
}

// 筛选处理
const handleFilter = () => {
  pagination.currentPage = 1
  loadProjects()
}

// 分页处理
const handleSizeChange = (size) => {
  pagination.pageSize = size
  loadProjects()
}

const handleCurrentChange = (current) => {
  pagination.currentPage = current
  loadProjects()
}

// 详情相关
const showDetailDialog = ref(false)
const currentProjectDetail = ref(null)

// 查看项目详情
const goToProjectDetail = (id) => {
  // 查找当前项目
  const project = projects.value.find(p => p.id === id)
  if (project) {
    currentProjectDetail.value = project
    showDetailDialog.value = true
  } else {
    ElMessage.error(t('uiAutomation.project.messages.notFound'))
  }
}

// API date-only values represent calendar days, not UTC timestamps.
const parseProjectDate = date => {
  if (!date) return null
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  if (!match) return new Date(date)
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
}

// 编辑项目
const editProject = (project) => {
  currentEditId.value = project.id
  // 复制项目数据到编辑表单
  Object.assign(editForm, {
    name: project.name,
    description: project.description,
    status: project.status,
    base_url: project.base_url,
    start_date: parseProjectDate(project.start_date),
    end_date: parseProjectDate(project.end_date)
  })
  editUrlProtocol.value = 'https://'
  syncUrlInput(editForm.base_url, editForm, editUrlProtocol)
  showEditDialog.value = true
}

// 删除项目
const deleteProject = async (id) => {
  try {
    await ElMessageBox.confirm(t('uiAutomation.project.messages.deleteConfirm'), t('uiAutomation.messages.confirm.delete'), {
      confirmButtonText: t('uiAutomation.common.confirm'),
      cancelButtonText: t('uiAutomation.common.cancel'),
      type: 'warning'
    })

    await deleteUiProject(id)
    ElMessage.success(t('uiAutomation.project.messages.deleteSuccess'))
    loadProjects()
  } catch (error) {
    if (error === 'cancel') return
    ElMessage.error(t('uiAutomation.project.messages.deleteFailed'))
    console.error('删除项目失败:', error)
  }
}

// 导入用户store
import { useUserStore } from '@/stores/user'

const getProjectSaveErrorMessage = (error, fallback) => {
  const response = error?.response?.data || {}
  const baseUrlError = Array.isArray(response.base_url) ? response.base_url[0] : response.base_url
  const messages = [baseUrlError, response.detail].filter(message => typeof message === 'string')
  if (messages.some(message => /受限网络|restricted network/i.test(message))) {
    return t('uiAutomation.project.messages.baseUrlRestrictedNetwork')
  }
  return messages[0] || fallback
}

// 日期格式化辅助函数
const formatDateToISO = (date) => {
  if (!date) return null
  const d = new Date(date)
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

// 处理创建项目
const handleCreate = async () => {
  if (creatingProject.value) return
  creatingProject.value = true
  try {
    const validate = await createFormRef.value.validate()
    if (!validate) return
    await createProject()
  } finally {
    creatingProject.value = false
  }
}

const createProject = async () => {
  try {
    const userStore = useUserStore()
    // 确保用户信息已加载
    if (!userStore.user?.id) {
      await userStore.fetchProfile()
    }
    
    // 创建包含owner字段的项目数据，并格式化日期字段
    const projectData = {
      ...createForm,
      base_url: buildBaseUrl(createForm.base_url, urlProtocol.value),
      owner: userStore.user.id,  // 添加owner字段，值为当前登录用户ID
      // 格式化日期为YYYY-MM-DD格式
      start_date: formatDateToISO(createForm.start_date),
      end_date: formatDateToISO(createForm.end_date)
    }
    
    const response = await createUiProject(projectData)
    const projectId = normalizeUiId(response?.data?.id)
    const continueSetup = guidedCreation.value && route.name === 'UiProjects'
    ElMessage.success(t('uiAutomation.project.messages.createSuccess'))
    if (continueSetup && projectId) {
      await router.replace({ name: 'UiTestCases', query: { project: String(projectId), setup: 'case' } })
    }
    showCreateDialog.value = false
    
    // 重置表单
    Object.keys(createForm).forEach(key => {
      createForm[key] = ''
    })
    createForm.status = 'IN_PROGRESS'
    urlProtocol.value = 'https://'
    
    if (!(continueSetup && projectId)) loadProjects()
  } catch (error) {
    ElMessage.error(getProjectSaveErrorMessage(error, t('uiAutomation.project.messages.createFailed')))
    console.error('创建项目失败:', error)
  }
}

// 处理编辑项目
const handleEdit = async () => {
  const validate = await editFormRef.value.validate()
  if (!validate) return
  
  try {
    // 创建包含格式化日期字段的项目数据
    const projectData = {
      ...editForm,
      base_url: buildBaseUrl(editForm.base_url, editUrlProtocol.value),
      // 格式化日期为YYYY-MM-DD格式
      start_date: formatDateToISO(editForm.start_date),
      end_date: formatDateToISO(editForm.end_date)
    }
    
    await updateUiProject(currentEditId.value, projectData)
    ElMessage.success(t('uiAutomation.project.messages.updateSuccess'))
    showEditDialog.value = false
    loadProjects()
  } catch (error) {
    ElMessage.error(getProjectSaveErrorMessage(error, t('uiAutomation.project.messages.updateFailed')))
    console.error('更新项目失败:', error)
  }
}

watch(guidedCreation, guided => {
  if (guided && route.name === 'UiProjects') showCreateDialog.value = true
}, { immediate: true })

watch(showCreateDialog, open => {
  if (open || !guidedCreation.value || route.name !== 'UiProjects') return
  const query = { ...route.query }
  delete query.setup
  void router.replace({ name: 'UiProjects', query, hash: route.hash })
})

// 组件挂载时加载数据
onMounted(() => {
  loadProjects()
})
</script>

<style scoped>
.page-container {
  padding: 20px;
  height: 100%;
  overflow-y: auto;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.page-title {
  margin: 0;
  font-size: 24px;
}

.card-container {
  background-color: #fff;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
}

.filter-bar {
  margin-bottom: 20px;
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}
</style>