<template>
  <div class="quality-loop">
    <div class="page-header">
      <div>
        <h1>{{ $t('execution.qualityLoop.title') }}</h1>
        <p>{{ $t('execution.qualityLoop.subtitle') }}</p>
      </div>
      <el-button @click="router.push({ name: 'ExecutionSourceBindings' })">{{ $t('menu.sourceBindings') }}</el-button>
    </div>

    <el-card shadow="never" class="metrics-card">
      <template #header>{{ $t('execution.qualityLoop.metricsTitle') }}</template>
      <el-row v-if="metrics" :gutter="12">
        <el-col v-for="item in metricCards" :key="item.key" :xs="12" :md="4">
          <el-statistic :title="item.label" :value="item.value" :suffix="item.suffix || ''" />
        </el-col>
      </el-row>
      <el-empty v-else :description="$t('execution.qualityLoop.noMetrics')" />
    </el-card>

    <el-row v-if="metrics" :gutter="16" class="control-plane-row">
      <el-col :xs="24" :lg="10">
        <el-card shadow="never">
          <template #header>{{ $t('execution.qualityLoop.readiness') }}</template>
          <div class="readiness-list">
            <div v-for="(ready, source) in metrics.route_readiness || {}" :key="source" class="readiness-row">
              <span>{{ source.toUpperCase() }}</span>
              <el-tag size="small" :type="ready ? 'success' : 'warning'">{{ ready ? $t('execution.qualityLoop.routeReady') : $t('execution.qualityLoop.routeMissing') }}</el-tag>
              <span class="binding-count">{{ metrics.enabled_bindings?.[source] || 0 }} bindings</span>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="14">
        <el-card shadow="never">
          <template #header>{{ $t('execution.qualityLoop.recentRuns') }}</template>
          <el-table :data="metrics.recent_runs || []" size="small">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="source_type" :label="$t('execution.taskCenter.source')" width="110" />
            <el-table-column prop="status" :label="$t('execution.taskCenter.status')" width="110" />
            <el-table-column :label="$t('execution.taskCenter.qualityGate')" min-width="130">
              <template #default="scope">{{ scope.row.quality_gate?.policy_version || '-' }}</template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!(metrics.recent_runs || []).length" :description="$t('execution.qualityLoop.noRecentRuns')" />
        </el-card>
      </el-col>
    </el-row>

    <el-card v-if="metrics?.scale" shadow="never" class="scale-card">
      <template #header>{{ $t('execution.qualityLoop.scaleTitle') }}</template>
      <div class="scale-grid">
        <span>{{ $t('execution.qualityLoop.scaleJobs') }}: <strong>{{ metrics.scale.execution_jobs }}</strong></span>
        <span>{{ $t('execution.qualityLoop.scaleArtifacts') }}: <strong>{{ metrics.scale.execution_artifacts }}</strong></span>
        <span>{{ $t('execution.qualityLoop.scaleBytes') }}: <strong>{{ metrics.scale.execution_artifact_bytes }}</strong></span>
        <span>{{ $t('execution.qualityLoop.scaleExpired') }}: <strong>{{ metrics.scale.expired_execution_artifacts }}</strong></span>
      </div>
      <el-alert v-if="metrics.scale.warnings?.length" class="scale-warning" type="warning" :closable="false" :title="metrics.scale.warnings.join(', ')" />
    </el-card>

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
            <el-form-item :label="$t('execution.qualityLoop.policyVersion')">
              <el-input v-model="gateForm.policyVersion" placeholder="release-2026.10" />
            </el-form-item>
            <el-form-item :label="$t('execution.qualityLoop.metricThresholds')">
              <el-input v-model="gateForm.metricThresholdsText" type="textarea" :rows="3" placeholder='{"p95":{"max":500,"max_delta_pct":20}}' />
            </el-form-item>
            <div class="actions">
              <el-button :loading="comparing" @click="compareRuns">{{ $t('execution.qualityLoop.compareAction') }}</el-button>
              <el-button type="primary" :loading="gating" @click="evaluateGate">{{ $t('execution.qualityLoop.gateAction') }}</el-button>
            </div>
          </el-form>
          <el-alert v-if="gateResult" class="result" :type="gateResult.passed ? 'success' : 'error'" :closable="false">
            <div v-if="gateResult.policy_version" class="policy-line">{{ $t('execution.qualityLoop.policyVersion') }}: {{ gateResult.policy_version }}</div>
            {{ JSON.stringify(gateResult) }}
          </el-alert>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { getExecutionMetrics } from '@/api/executions'
import {
  compareExecutionJobs,
  evaluateExecutionReleaseGate,
  importExecutionResults,
} from '@/api/executions'

const router = useRouter()
const importing = ref(false)
const comparing = ref(false)
const gating = ref(false)
const importFile = ref(null)
const importResultData = ref(null)
const gateResult = ref(null)
const metrics = ref(null)
const metricCards = computed(() => metrics.value ? [
  { key: 'ci', label: 'CI jobs', value: metrics.value.ci_jobs },
  { key: 'association', label: '关联率', value: metrics.value.ci_association_rate, suffix: '%' },
  { key: 'unmatched', label: '未匹配', value: metrics.value.unmatched_results },
  { key: 'gate', label: '门禁通过率', value: metrics.value.gate_pass_rate, suffix: '%' },
  { key: 'flaky', label: 'Flaky', value: metrics.value.flaky_profiles },
] : [])
const importForm = ref({ project_id: null, format: 'junit', external_run_id: '', build_reference: '' })
const gateForm = ref({ jobId: null, baselineJobId: null, maxFailed: 0, maxUnmatched: 0, policyVersion: '', metricThresholdsText: '{}' })

const loadMetrics = async () => {
  try {
    const response = await getExecutionMetrics()
    metrics.value = response.data
  } catch (error) { ElMessage.error(error.response?.data?.detail || '指标加载失败') }
}

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
    let metricThresholds
    try {
      metricThresholds = JSON.parse(gateForm.value.metricThresholdsText || '{}')
    } catch {
      ElMessage.warning('metric thresholds 必须是合法 JSON 对象')
      return
    }
    const response = await evaluateExecutionReleaseGate(gateForm.value.jobId, {
      baseline_job_id: gateForm.value.baselineJobId || undefined,
      max_failed: gateForm.value.maxFailed,
      max_unmatched: gateForm.value.maxUnmatched,
      policy_version: gateForm.value.policyVersion || undefined,
      metric_thresholds: metricThresholds,
    })
    gateResult.value = response.data
  } catch (error) {
    gateResult.value = error.response?.data || null
    ElMessage.error(error.response?.data?.detail || '质量门禁失败')
  } finally {
    gating.value = false
  }
}

onMounted(loadMetrics)
</script>

<style scoped>
.scale-card { margin-bottom: 16px; }
.scale-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; color: var(--el-text-color-secondary); }
.scale-grid strong { color: var(--el-text-color-primary); }
.scale-warning { margin-top: 12px; }
.readiness-list { display: grid; gap: 10px; }
.readiness-row { display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 10px; }
.binding-count { color: var(--el-text-color-secondary); font-size: 12px; }
.policy-line { margin-bottom: 6px; font-weight: 600; }
.page-header { margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.page-header p { color: var(--el-text-color-secondary); }
.file-name { margin-top: 8px; color: var(--el-text-color-secondary); }
.actions { display: flex; gap: 12px; }
.result { margin-top: 18px; word-break: break-word; }
</style>
