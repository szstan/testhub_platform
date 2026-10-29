<template>
  <div class="traceability-view">
    <div class="page-header">
      <div>
        <h1>{{ $t('execution.traceability.title') }}</h1>
        <p class="page-subtitle">{{ $t('execution.traceability.subtitle') }}</p>
      </div>
      <el-button type="primary" @click="loadTraceability">{{ $t('common.search') }}</el-button>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent="loadTraceability">
        <el-form-item :label="$t('execution.traceability.projectId')">
          <el-input-number v-model="projectId" :min="1" :controls="false" />
        </el-form-item>
        <el-form-item :label="$t('execution.traceability.versionId')">
          <el-input-number v-model="versionId" :min="1" :controls="false" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-row :gutter="16" v-loading="loading" class="summary-row">
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.totalRequirements') }}</span><strong>{{ coverage.total_requirements }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.linkedRequirements') }}</span><strong>{{ coverage.linked_requirements }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.executedRequirements') }}</span><strong>{{ coverage.executed_requirements }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.passedRequirements') }}</span><strong>{{ coverage.passed_requirements }}</strong></div></el-card></el-col>
      <el-col :xs="12" :md="4"><el-card shadow="never"><div class="metric"><span>{{ $t('execution.traceability.unexecutedRequirements') }}</span><strong>{{ coverage.unexecuted_requirements?.length || 0 }}</strong></div></el-card></el-col>
    </el-row>

    <el-card shadow="never">
      <template #header>{{ $t('execution.traceability.builds') }}</template>
      <el-table :data="builds">
        <el-table-column prop="name" :label="$t('execution.traceability.buildName')" />
        <el-table-column prop="build_number" :label="$t('execution.traceability.buildNumber')" />
        <el-table-column prop="branch" :label="$t('execution.traceability.branch')" />
        <el-table-column prop="commit_sha" label="SHA" show-overflow-tooltip />
        <el-table-column prop="environment" :label="$t('execution.traceability.environment')" />
        <el-table-column prop="status" :label="$t('execution.taskCenter.status')" />
        <el-table-column prop="created_at" :label="$t('execution.traceability.createdAt')" />
      </el-table>
      <el-empty v-if="!loading && builds.length === 0" :description="$t('execution.traceability.noBuilds')" />
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/utils/api'
import { getExecutionCoverage, listExecutionBuilds } from '@/api/executions'

const projectId = ref(null)
const versionId = ref(null)
const loading = ref(false)
const builds = ref([])
const coverage = ref({ total_requirements: 0, covered_requirements: 0, linked_requirements: 0, executed_requirements: 0, passed_requirements: 0, unexecuted_requirements: [], coverage_rate: 0 })

const loadTraceability = async () => {
  if (!projectId.value) {
    ElMessage.warning('请输入项目 ID')
    return
  }
  loading.value = true
  try {
    const [buildResponse, coverageResponse] = await Promise.all([
      listExecutionBuilds({ project: projectId.value, ordering: '-created_at' }),
      getExecutionCoverage(projectId.value, versionId.value),
    ])
    const buildData = buildResponse.data || {}
    builds.value = buildData.results || (Array.isArray(buildData) ? buildData : [])
    coverage.value = coverageResponse.data || coverage.value
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载追踪数据失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.traceability-view { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; }
.page-header h1 { margin: 0; font-size: 24px; }
.page-subtitle { color: var(--el-text-color-secondary); }
.filter-card { margin-bottom: 16px; }
.summary-row { margin-bottom: 16px; }
.metric { display: flex; align-items: center; justify-content: space-between; color: var(--el-text-color-secondary); }
.metric strong { font-size: 28px; color: var(--el-text-color-primary); }
</style>
