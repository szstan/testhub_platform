<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">{{ isEdit ? '编辑缺陷' : '新建缺陷' }}</h1>
      <el-button @click="router.back()">
        <el-icon><Back /></el-icon>
        返回
      </el-button>
    </div>

    <el-alert v-if="detailLoadFailed" class="detail-error" title="加载缺陷详情失败" type="error" :closable="false" show-icon>
      <el-button type="primary" link @click="loadDefect">重试</el-button>
    </el-alert>

    <div class="card-container">
      <el-form ref="formRef" :model="form" :rules="rules" :disabled="!canSave || submitting" label-width="110px">
        <el-row :gutter="16">
          <el-col :xs="24" :md="16">
            <el-form-item label="缺陷标题" prop="title">
              <el-input v-model="form.title" placeholder="请输入缺陷标题" maxlength="300" show-word-limit />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="8">
            <el-form-item label="所属项目" prop="project_id">
              <el-select
                v-model="form.project_id"
                filterable
                clearable
                placeholder="请选择项目"
                class="full-width"
                @change="handleProjectChange"
              >
                <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="16">
          <el-col :xs="24" :md="6">
            <el-form-item label="版本">
              <el-select
                v-model="form.version_id"
                filterable
                clearable
                :disabled="!form.project_id"
                :placeholder="form.project_id ? '请选择版本' : '请先选择项目'"
                class="full-width"
              >
                <el-option v-for="version in versions" :key="version.id" :label="version.name" :value="version.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="6">
            <el-form-item label="模块" prop="module" required>
              <el-input v-model="form.module" placeholder="请输入模块名称" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="6">
            <el-form-item label="严重级别" prop="severity">
              <el-select v-model="form.severity" class="full-width">
                <el-option v-for="item in severityOptions" :key="item.value" :label="item.label" :value="item.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="6">
            <el-form-item label="优先级" prop="priority">
              <el-select v-model="form.priority" class="full-width">
                <el-option v-for="item in priorityOptions" :key="item.value" :label="item.label" :value="item.value" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="16">
          <el-col :xs="24" :md="8">
            <el-form-item label="缺陷类型" prop="defect_type" required>
              <el-select v-model="form.defect_type" clearable placeholder="请选择缺陷类型" class="full-width">
                <el-option v-for="item in defectTypeOptions" :key="item.value" :label="item.label" :value="item.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="8">
            <el-form-item label="来源">
              <el-select v-model="form.source" class="full-width">
                <el-option v-for="item in sourceOptions" :key="item.value" :label="item.label" :value="item.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="8">
            <el-form-item label="处理人">
              <el-select v-model="form.assignee_id" filterable clearable placeholder="请选择处理人" class="full-width">
                <el-option v-for="user in users" :key="user.id" :label="user.username || user.name" :value="user.id" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="缺陷描述">
          <el-input v-model="form.description" type="textarea" :rows="4" placeholder="描述缺陷现象、影响范围或上下文" />
        </el-form-item>
        <el-form-item label="复现步骤">
          <el-input v-model="form.reproduce_steps" type="textarea" :rows="4" placeholder="按步骤描述如何复现" />
        </el-form-item>

        <el-row :gutter="16">
          <el-col :xs="24" :md="12">
            <el-form-item label="预期结果">
              <el-input v-model="form.expected_result" type="textarea" :rows="3" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item label="实际结果">
              <el-input v-model="form.actual_result" type="textarea" :rows="3" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="期望修复时间">
          <el-date-picker
            v-model="form.due_at"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
            placeholder="请选择时间"
          />
        </el-form-item>

        <el-form-item label="附件">
          <div class="attachment-section">
            <el-upload
              :show-file-list="false"
              :auto-upload="false"
              multiple
              :accept="allowedAttachmentExtensions.join(',')"
              :on-change="handleFileChange"
            >
              <el-button>
                <el-icon><Upload /></el-icon>
                选择附件
              </el-button>
              <template #tip>
                <div class="upload-tip">
                  支持 {{ allowedAttachmentExtensions.join('、') }} 等格式，单个文件不超过 10MB；保存缺陷后自动上传
                </div>
              </template>
            </el-upload>

            <el-table v-if="attachmentRows.length" :data="attachmentRows" size="small" border class="attachment-table">
              <el-table-column label="文件名" prop="name" show-overflow-tooltip />
              <el-table-column label="大小" width="110" prop="sizeText" />
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <el-tag v-if="row.existing" size="small" type="info">已上传</el-tag>
                  <el-tag v-else size="small" type="warning">待上传</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="120" align="center">
                <template #default="{ row }">
                  <el-link v-if="row.existing" :href="row.url" type="primary" target="_blank" :underline="false">查看</el-link>
                  <el-button v-else link type="danger" @click="removePendingFile(row.uid)">移除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-form-item>

      </el-form>
      <div class="form-actions">
        <el-button @click="router.back()">取消</el-button>
        <el-button type="primary" :loading="submitting" :disabled="!canSave" @click="submitForm">保存</el-button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Back, Upload } from '@element-plus/icons-vue'
import api from '@/utils/api'
import { createDefect, getDefect, updateDefect, uploadDefectAttachment } from '@/api/defects'
import {
  defectTypeOptions,
  priorityOptions,
  severityOptions,
  sourceOptions
} from './options'

const route = useRoute()
const router = useRouter()
const formRef = ref()
const submitting = ref(false)
const projects = ref([])
const versions = ref([])
const users = ref([])
const isEdit = computed(() => Boolean(route.params.id))
const openedDefect = ref(null)
const detailLoadFailed = ref(false)
const canSave = computed(() => !isEdit.value || openedDefect.value?.id === route.params.id)
let detailRequest = 0

const pendingFiles = ref([])
const existingAttachments = ref([])

const maxAttachmentSize = 10 * 1024 * 1024
const allowedAttachmentExtensions = ['.txt', '.log', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.doc', '.docx', '.xls', '.xlsx', '.zip']

const nullableFields = ['project_id', 'version_id', 'assignee_id', 'due_at']

const defaultForm = {
  title: '',
  description: '',
  reproduce_steps: '',
  expected_result: '',
  actual_result: '',
  project_id: '',
  version_id: '',
  module: '',
  severity: 'major',
  priority: 'p2',
  defect_type: '',
  source: 'manual',
  assignee_id: '',
  due_at: ''
}
const form = reactive({ ...defaultForm })

const rules = {
  title: [{ required: true, message: '请输入缺陷标题', trigger: 'blur' }],
  project_id: [{ required: true, message: '请选择所属项目', trigger: 'change' }],
  module: [{ required: true, message: '请输入模块名称', trigger: 'blur' }],
  severity: [{ required: true, message: '请选择严重级别', trigger: 'change' }],
  priority: [{ required: true, message: '请选择优先级', trigger: 'change' }],
  defect_type: [{ required: true, message: '请选择缺陷类型', trigger: 'change' }]
}

const normalizeList = (data) => data?.results || data || []

const fetchProjects = async () => {
  try {
    const response = await api.get('/projects/')
    projects.value = normalizeList(response.data)
  } catch (error) {
    projects.value = []
  }
}

const fetchVersions = async (projectId = form.project_id) => {
  versions.value = []
  if (!projectId) return

  try {
    const response = await api.get(`/versions/projects/${projectId}/versions/`)
    if (projectId === form.project_id) versions.value = normalizeList(response.data)
  } catch {
    if (projectId === form.project_id) versions.value = []
  }
}

const fetchUsers = async () => {
  try {
    const response = await api.get('/users/')
    users.value = normalizeList(response.data)
  } catch (error) {
    users.value = []
  }
}

const handleProjectChange = async () => {
  form.version_id = ''
  await fetchVersions()
}

const loadDefect = async () => {
  const defectId = route.params.id
  const request = ++detailRequest
  openedDefect.value = null
  detailLoadFailed.value = false
  existingAttachments.value = []
  pendingFiles.value = []
  versions.value = []
  Object.assign(form, defaultForm)
  if (!defectId) return

  try {
    const { data } = await getDefect(defectId)
    if (request !== detailRequest || defectId !== route.params.id) return
    if (String(data?.id) !== String(defectId)) throw new Error('Invalid defect detail response')

    form.title = data.title || ''
    form.description = data.description || ''
    form.reproduce_steps = data.reproduce_steps || ''
    form.expected_result = data.expected_result || ''
    form.actual_result = data.actual_result || ''
    form.project_id = data.project?.id || ''
    form.version_id = data.version?.id || ''
    form.module = data.module || ''
    form.severity = data.severity || 'major'
    form.priority = data.priority || 'p2'
    form.defect_type = data.defect_type || ''
    form.source = data.source || 'manual'
    form.assignee_id = data.assignee?.id || ''
    form.due_at = data.due_at || ''
    existingAttachments.value = data.attachments || []
    openedDefect.value = { id: defectId, values: buildPayload() }
    await fetchVersions()
  } catch {
    if (request === detailRequest && defectId === route.params.id) detailLoadFailed.value = true
  }
}

const buildPayload = () => {
  const payload = { ...form }

  nullableFields.forEach((field) => {
    if (payload[field] === '' || payload[field] === undefined) {
      payload[field] = null
    }
  })

  return payload
}

const formatSize = (size) => {
  if (size == null) return '-'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

const validateAttachment = (file) => {
  const fileName = file.name || ''
  const ext = fileName.includes('.') ? fileName.slice(fileName.lastIndexOf('.')).toLowerCase() : ''
  if (!allowedAttachmentExtensions.includes(ext)) {
    ElMessage.warning(`仅支持 ${allowedAttachmentExtensions.join('、')} 格式的附件`)
    return false
  }
  if (file.size > maxAttachmentSize) {
    ElMessage.warning('附件大小不能超过 10MB')
    return false
  }
  return true
}

const attachmentRows = computed(() => [
  ...existingAttachments.value.map((item) => ({
    uid: `existing-${item.id}`,
    name: item.name,
    sizeText: '-',
    existing: true,
    url: item.file
  })),
  ...pendingFiles.value.map((item) => ({
    uid: item.uid,
    name: item.name,
    sizeText: item.sizeText,
    existing: false
  }))
])

const handleFileChange = (uploadFile) => {
  const file = uploadFile?.raw
  if (!file) return
  if (!validateAttachment(file)) return
  const duplicated = pendingFiles.value.some(
    (item) => item.uid === uploadFile.uid || (item.name === file.name && item.size === file.size)
  )
  if (duplicated) {
    ElMessage.warning('该附件已添加')
    return
  }
  pendingFiles.value.push({
    uid: uploadFile.uid,
    name: file.name,
    size: file.size,
    sizeText: formatSize(file.size),
    raw: file
  })
}

const removePendingFile = (uid) => {
  const index = pendingFiles.value.findIndex((item) => item.uid === uid)
  if (index !== -1) pendingFiles.value.splice(index, 1)
}

const uploadPendingAttachments = async (defectId) => {
  if (!defectId || !pendingFiles.value.length) return
  const failed = []
  for (const item of pendingFiles.value) {
    const formData = new FormData()
    formData.append('file', item.raw)
    formData.append('name', item.name)
    try {
      await uploadDefectAttachment(defectId, formData)
    } catch (error) {
      failed.push(item.name)
    }
  }
  if (failed.length) {
    ElMessage.warning(`部分附件上传失败：${failed.join('、')}`)
  }
}

const extractErrorMessage = (errorData) => {
  if (!errorData) return ''
  if (typeof errorData === 'string') return errorData
  if (Array.isArray(errorData)) return errorData[0] || ''

  const firstValue = Object.values(errorData)[0]
  if (Array.isArray(firstValue)) return firstValue[0] || ''
  if (typeof firstValue === 'string') return firstValue
  return ''
}

const submitForm = async () => {
  if (!formRef.value || !canSave.value || submitting.value) return

  const defectId = route.params.id
  submitting.value = true
  try {
    const payload = buildPayload()
    let savedId = defectId

    if (defectId) {
      const openedValues = openedDefect.value.values
      const changes = Object.fromEntries(
        Object.entries(payload).filter(([field, value]) => value !== openedValues[field])
      )
      if ('project_id' in changes) {
        // A move replaces the version selection along with the project.
        changes.version_id = payload.version_id
      }
      if (Object.keys(changes).length) {
        await formRef.value.validate()
        if (route.params.id !== defectId) return
        await updateDefect(defectId, changes)
        if (route.params.id !== defectId) return
        openedDefect.value = { id: defectId, values: payload }
        ElMessage.success('缺陷已更新')
      }
    } else {
      await formRef.value.validate()
      if (route.params.id !== defectId) return
      const response = await createDefect(payload)
      savedId = response.data?.id
      ElMessage.success('缺陷已创建')
    }

    await uploadPendingAttachments(savedId)
    if (route.params.id === defectId) router.push(`/defects/${savedId || 'list'}`)
  } catch (error) {
    if (error?.response) {
      const message = extractErrorMessage(error.response.data) || '保存缺陷失败'
      ElMessage.error(message)
    }
  } finally {
    submitting.value = false
  }
}

watch(() => route.params.id, loadDefect)
onMounted(() => Promise.all([fetchProjects(), fetchUsers(), loadDefect()]))
</script>

<style lang="scss" scoped>
.detail-error {
  margin-bottom: 16px;
}

.full-width {
  width: 100%;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding-top: 12px;
}

.attachment-section {
  width: 100%;
}

.attachment-table {
  margin-top: 10px;
}

.upload-tip {
  margin-top: 4px;
  color: #909399;
  font-size: 12px;
  line-height: 1.5;
}
</style>
