<template>
  <div class="perf-projects">
    <div class="page-header">
      <div class="ph-left">
        <h2>{{ t('performanceTesting.project.title') }}</h2>
        <span class="subtitle">{{ t('performanceTesting.project.subtitle') }}</span>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreate">{{ t('performanceTesting.project.create') }}</el-button>
    </div>

    <div class="filter-bar">
      <el-input v-model="keyword" :placeholder="t('performanceTesting.project.namePlaceholder')" clearable style="width: 240px" @clear="load" @keyup.enter="load" />
      <el-select v-model="statusFilter" :placeholder="t('performanceTesting.project.status')" clearable style="width: 150px" @change="load">
        <el-option :label="t('performanceTesting.project.NOT_STARTED')" value="NOT_STARTED" />
        <el-option :label="t('performanceTesting.project.IN_PROGRESS')" value="IN_PROGRESS" />
        <el-option :label="t('performanceTesting.project.COMPLETED')" value="COMPLETED" />
      </el-select>
      <el-button :icon="Search" @click="load">{{ t('performanceTesting.common.search') }}</el-button>
    </div>

    <el-table :data="list" v-loading="loading" size="small" border>
      <el-table-column :label="t('performanceTesting.project.name')" prop="name" min-width="160" show-overflow-tooltip />
      <el-table-column :label="t('performanceTesting.project.description')" prop="description" min-width="180" show-overflow-tooltip />
      <el-table-column :label="t('performanceTesting.project.status')" width="100">
        <template #default="{ row }">
        <el-tag size="small" :type="statusTagType(row.status)">
          {{ statusLabel(row.status) }}
        </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('performanceTesting.project.scenarioCount')" width="90" align="center" prop="scenario_count" />
      <el-table-column :label="t('performanceTesting.project.executionCount')" width="90" align="center" prop="execution_count" />
      <el-table-column :label="t('performanceTesting.project.owner')" width="120">
        <template #default="{ row }">{{ row.owner?.username || '-' }}</template>
      </el-table-column>
      <el-table-column :label="t('performanceTesting.common.createdAt')" width="160">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('performanceTesting.common.actions')" width="140" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">{{ t('performanceTesting.common.edit') }}</el-button>
          <el-button size="small" type="danger" @click="remove(row)">{{ t('performanceTesting.common.delete') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="load"
      />
    </div>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="480px" @closed="resetForm">
      <el-form :model="form" label-width="90px">
        <el-form-item :label="t('performanceTesting.project.name')" :rules="[{ required: true }]">
          <el-input v-model="form.name" :placeholder="t('performanceTesting.project.namePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('performanceTesting.project.description')">
          <el-input v-model="form.description" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item :label="t('performanceTesting.project.status')">
          <el-select v-model="form.status" style="width: 160px">
            <el-option :label="t('performanceTesting.project.NOT_STARTED')" value="NOT_STARTED" />
            <el-option :label="t('performanceTesting.project.IN_PROGRESS')" value="IN_PROGRESS" />
            <el-option :label="t('performanceTesting.project.COMPLETED')" value="COMPLETED" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('performanceTesting.project.members')">
          <el-select v-model="form.member_ids" multiple filterable style="width: 100%" :placeholder="t('performanceTesting.common.selectProject')">
            <el-option v-for="u in users" :key="u.id" :label="u.username" :value="u.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('performanceTesting.common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ t('performanceTesting.common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search } from '@element-plus/icons-vue'
import { getPerfProjects, createPerfProject, updatePerfProject, deletePerfProject } from '@/api/performance-testing'
import { getUsers } from '@/api/api-testing'
import { formatTime } from './shared'

const { t } = useI18n()
const list = ref([])
const loading = ref(false)
const keyword = ref('')
const statusFilter = ref('')
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)
const users = ref([])

const dialogVisible = ref(false)
const dialogTitle = ref('')
const saving = ref(false)
const editingId = ref(null)
const form = ref({ name: '', description: '', status: 'IN_PROGRESS', member_ids: [] })

const dialogTitle_ = computed(() => dialogTitle.value)

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (keyword.value) params.search = keyword.value
    if (statusFilter.value) params.status = statusFilter.value
    const res = await getPerfProjects(params)
    list.value = res.data.results || res.data || []
    total.value = res.data.count || list.value.length
  } catch (e) {
    ElMessage.error('加载项目列表失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  dialogTitle.value = t('performanceTesting.project.create')
  form.value = { name: '', description: '', status: 'IN_PROGRESS', member_ids: [] }
  dialogVisible.value = true
}
function openEdit(row) {
  editingId.value = row.id
  dialogTitle.value = t('performanceTesting.project.edit')
  form.value = {
    name: row.name, description: row.description || '', status: row.status || 'IN_PROGRESS',
    member_ids: (row.members || []).map(m => m.id)
  }
  dialogVisible.value = true
}
function resetForm() { form.value = { name: '', description: '', status: 'IN_PROGRESS', member_ids: [] } }

function statusTagType(s) {
  if (s === 'IN_PROGRESS') return 'success'
  if (s === 'COMPLETED') return 'warning'
  return 'info'
}
function statusLabel(s) {
  const key = 'performanceTesting.project.' + (s || '')
  const txt = t(key)
  return txt === key ? (s || '-') : txt
}

async function save() {
  if (!form.value.name || !form.value.name.trim()) {
    ElMessage.warning(t('performanceTesting.project.nameRequired'))
    return
  }
  saving.value = true
  try {
    const payload = { ...form.value }
    if (editingId.value) {
      await updatePerfProject(editingId.value, payload)
      ElMessage.success(t('performanceTesting.project.updateSuccess') || '更新成功')
    } else {
      await createPerfProject(payload)
      ElMessage.success(t('performanceTesting.project.createSuccess') || '创建成功')
    }
    dialogVisible.value = false
    await load()
  } catch (e) {
    const msg = e?.response?.data?.name?.[0] || e?.response?.data?.error || '保存失败'
    ElMessage.error(msg)
  } finally {
    saving.value = false
  }
}

function remove(row) {
  ElMessageBox.confirm(t('performanceTesting.project.deleteConfirm', { name: row.name }), t('performanceTesting.common.tips'), {
    type: 'warning', confirmButtonText: t('performanceTesting.common.confirm'), cancelButtonText: t('performanceTesting.common.cancel')
  }).then(async () => {
    try {
      await deletePerfProject(row.id)
      ElMessage.success(t('performanceTesting.project.deleteSuccess') || '删除成功')
      await load()
    } catch (e) {
      const msg = e?.response?.data?.error || '删除失败'
      ElMessage.error(msg)
    }
  }).catch(() => {})
}

async function loadUsers() {
  try {
    const res = await getUsers({ page_size: 200 })
    users.value = res.data.results || res.data || []
  } catch (e) { /* ignore */ }
}

onMounted(() => { load(); loadUsers() })
// 暴露给模板（避免未使用告警）
void dialogTitle_
</script>

<style lang="scss" scoped>
.perf-projects { padding: 16px; }
.page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.ph-left { display: flex; align-items: baseline; gap: 10px; h2 { margin: 0; font-size: 20px; } }
.subtitle { color: #8c8c8c; font-size: 13px; }
.filter-bar { display: flex; gap: 10px; margin-bottom: 14px; }
.pager { margin-top: 14px; display: flex; justify-content: flex-end; }
</style>
