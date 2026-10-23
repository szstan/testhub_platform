<template>
  <div class="quality-loop">
    <div class="page-header">
      <div>
        <h1>{{ $t('execution.qualityLoop.title') }}</h1>
        <p>{{ $t('execution.qualityLoop.subtitle') }}</p>
      </div>
    </div>

    <el-row :gutter="16">
      <el-col :xs="24" :lg="12">
        <el-card shadow="never">
          <template #header>{{ $t('execution.qualityLoop.importTitle') }}</template>
          <el-form label-width="150px">
            <el-form-item :label="$t('execution.qualityLoop.projectId')">
              <el-input-number v-model="importForm.project_id" :min="1" :controls="false" />
            </el-form-item>
            <el-form-item :label="$t('execution.qualityLoop.format')">
              <el-select v-model="importForm.format">
                <el-option label="JUnit XML" value="junit" />
                <el-option label="Allure JSON" value="allure" />
              </el-select>
            </el-form-item>
            <el-form-item :label="$t('execution.qualityLoop.externalRunId')">
              <el-input v-model="importForm.external_run_id" />
            </el-form-item>
            <el-form-item :label="$t('execution.qualityLoop.buildReference')">
              <el-input v-model="importForm.build_reference" />
            </el-form-item>
            <el-form-item :label="$t('execution.qualityLoop.resultFile')">
              <input type="file" accept=".xml,.json" @change="selectFile" />
              <div v-if="importFile" class="file-name">{{ importFile.name }}</div>
            </el-form-item>
            <el-button type="primary" :loading="importing" @click="importResult">
              {{ $t('execution.qualityLoop.importAction') }}
            </el-button>
          </el-form>
          <el-alert v-if="importResultData" class="result" type="success" :closable="false">
            {{ JSON.stringify(importResultData) }}
          </el-alert>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="12">
        <el-card shadow="never">
          <template #header>{{ $t('execution.qualityLoop.gateTitle') }}</template>
          <el-form label-width="150px">
            <el-form-item :label="$t('execution.qualityLoop.currentJobId')">
              <el-input-number v-model="gateForm.jobId" :min="1" :controls="false" />
            </el-form-item>
            <el-form-item :label="$t('execution.qualityLoop.baselineJobId')">
              <el-input-number v-model="gateForm.baselineJobId" :min="1" :controls="false" />
            </el-form-item>
            <el-form-item :label="$t('execution.qualityLoop.maxFailed')">
              <el-input-number v-model="gateForm.maxFailed" :min="0" :controls="false" />
            </el-form-item>
            <el-form-item :label="$t('execution.qualityLoop.maxUnmatched')">
              <el-input-number v-model="gateForm.maxUnmatched" :min="0" :controls="false" />
            </el-form-item>
            <div class="actions">
              <el-button :loading="comparing" @click="compareRuns">{{ $t('execution.qualityLoop.compareAction') }}</el-button>
              <el-button type="primary" :loading="gating" @click="evaluateGate">{{ $t('execution.qualityLoop.gateAction') }}</el-button>
            </div>
          </el-form>
          <el-alert v-if="gateResult" class="result" :type="gateResult.passed ? 'success' : 'error'" :closable="false">
            {{ JSON.stringify(gateResult) }}
          </el-alert>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  compareExecutionJobs,
  evaluateExecutionReleaseGate,
  importExecutionResults,
} from '@/api/executions'

const importing = ref(false)
const comparing = ref(false)
const gating = ref(false)
const importFile = ref(null)
const importResultData = ref(null)
const gateResult = ref(null)
const importForm = ref({ project_id: null, format: 'junit', external_run_id: '', build_reference: '' })
const gateForm = ref({ jobId: null, baselineJobId: null, maxFailed: 0, maxUnmatched: 0 })

const selectFile = event => {
  importFile.value = event.target.files?.[0] || null
}

const importResult = async () => {
  if (!importFile.value || !importForm.value.project_id || !importForm.value.external_run_id) {
    ElMessage.warning('请填写项目、运行 ID 并选择结果文件')
    return
  }
  importing.value = true
  try {
    const data = new FormData()
    Object.entries(importForm.value).forEach(([key, value]) => {
      if (value !== null && value !== '') data.append(key, value)
    })
    data.append('file', importFile.value)
    const response = await importExecutionResults(data)
    importResultData.value = response.data
    gateForm.value.jobId = response.data.job_id
    ElMessage.success('结果导入成功')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '结果导入失败')
  } finally {
    importing.value = false
  }
}

const compareRuns = async () => {
  if (!gateForm.value.jobId || !gateForm.value.baselineJobId) {
    ElMessage.warning('请填写当前运行和基线运行 ID')
    return
  }
  comparing.value = true
  try {
    const response = await compareExecutionJobs(gateForm.value.jobId, gateForm.value.baselineJobId)
    gateResult.value = response.data
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '运行对比失败')
  } finally {
    comparing.value = false
  }
}

const evaluateGate = async () => {
  if (!gateForm.value.jobId) {
    ElMessage.warning('请填写当前运行 ID')
    return
  }
  gating.value = true
  try {
    const response = await evaluateExecutionReleaseGate(gateForm.value.jobId, {
      baseline_job_id: gateForm.value.baselineJobId || undefined,
      max_failed: gateForm.value.maxFailed,
      max_unmatched: gateForm.value.maxUnmatched,
    })
    gateResult.value = response.data
  } catch (error) {
    gateResult.value = error.response?.data || null
    ElMessage.error(error.response?.data?.detail || '质量门禁失败')
  } finally {
    gating.value = false
  }
}
</script>

<style scoped>
.quality-loop { padding: 24px; }
.page-header { margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.page-header p { color: var(--el-text-color-secondary); }
.file-name { margin-top: 8px; color: var(--el-text-color-secondary); }
.actions { display: flex; gap: 12px; }
.result { margin-top: 18px; word-break: break-word; }
</style>
