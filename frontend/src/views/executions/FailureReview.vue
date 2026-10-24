<template>
  <div class="failure-review">
    <div class="page-header">
      <div><h1>{{ $t('execution.failureReview.title') }}</h1><p>{{ $t('execution.failureReview.subtitle') }}</p></div>
      <el-button type="primary" :loading="loading" @click="load">{{ $t('common.refresh') }}</el-button>
    </div>
    <el-form inline @submit.prevent="load">
      <el-form-item :label="$t('execution.failureReview.jobId')"><el-input-number v-model="jobId" :min="1" :controls="false" /></el-form-item>
      <el-form-item><el-button type="primary" @click="load">{{ $t('common.search') }}</el-button></el-form-item>
    </el-form>
    <el-card shadow="never" v-loading="loading">
      <el-table :data="clusters" row-key="signature">
        <el-table-column prop="category" :label="$t('execution.failureReview.category')" width="150" />
        <el-table-column prop="subtype" :label="$t('execution.failureReview.subtype')" width="150" />
        <el-table-column prop="count" :label="$t('execution.failureReview.count')" width="90" />
        <el-table-column prop="signature" :label="$t('execution.failureReview.signature')" width="200" show-overflow-tooltip />
        <el-table-column prop="sample_message" :label="$t('execution.failureReview.message')" show-overflow-tooltip />
        <el-table-column :label="$t('execution.failureReview.actions')" width="180">
          <template #default="scope"><el-button link type="primary" @click="openReview(scope.row)">{{ $t('execution.failureReview.review') }}</el-button></template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !clusters.length" :description="$t('execution.failureReview.empty')" />
    </el-card>
    <el-dialog v-model="dialogVisible" :title="$t('execution.failureReview.dialogTitle')" width="520px">
      <el-form label-width="120px">
        <el-form-item :label="$t('execution.failureReview.category')"><el-select v-model="reviewForm.category"><el-option v-for="item in categories" :key="item" :label="item" :value="item" /></el-select></el-form-item>
        <el-form-item :label="$t('execution.failureReview.caseKey')"><el-input v-model="reviewForm.case_key" /></el-form-item>
        <el-form-item :label="$t('execution.failureReview.note')"><el-input v-model="reviewForm.note" type="textarea" :rows="4" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button><el-button type="primary" :loading="saving" @click="saveReview">{{ $t('common.save') }}</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/utils/api'

const jobId = ref(null)
const clusters = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const reviewForm = ref({ category: 'UNKNOWN', case_key: '', signature: '', note: '' })
const categories = ['PRODUCT', 'TEST', 'ENVIRONMENT', 'INFRASTRUCTURE', 'DATA', 'PERFORMANCE', 'TIMEOUT', 'UNKNOWN']

const load = async () => {
  if (!jobId.value) return ElMessage.warning('请输入 Job ID')
  loading.value = true
  try {
    const response = await api.get(`/executions/jobs/${jobId.value}/failure-analysis/`)
    clusters.value = response.data?.clusters || []
  } catch (error) { ElMessage.error(error.response?.data?.detail || '加载失败') } finally { loading.value = false }
}
const openReview = row => {
  reviewForm.value = { category: row.category, case_key: row.cases?.[0] || '', signature: row.signature, note: '' }
  dialogVisible.value = true
}
const saveReview = async () => {
  saving.value = true
  try {
    await api.post(`/executions/jobs/${jobId.value}/failure-review/`, reviewForm.value)
    dialogVisible.value = false
    ElMessage.success('反馈已保存')
  } catch (error) { ElMessage.error(error.response?.data?.detail || '保存失败') } finally { saving.value = false }
}
</script>

<style scoped>
.failure-review { padding: 24px; }
.page-header { display: flex; justify-content: space-between; margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.page-header p { color: var(--el-text-color-secondary); }
</style>
