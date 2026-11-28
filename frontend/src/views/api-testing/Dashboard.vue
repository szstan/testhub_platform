<template>
  <div class="dashboard-container">
    <!-- 数据概览 -->
    <div class="stats-section overview-grid">
      <el-card v-for="card in statsCards" :key="card.path" shadow="hover" class="stat-card">
        <button type="button" class="stat-content stat-navigation" @click="router.push(card.path)">
          <div class="stat-icon" :class="card.color"><el-icon><component :is="card.icon" /></el-icon></div>
          <div class="stat-info">
            <div class="stat-value">{{ card.value }}</div>
            <div class="stat-label">{{ card.label }}</div>
          </div>
        </button>
      </el-card>
    </div>
    
    <!-- 最近活动和快速操作 -->
    <el-row :gutter="20" class="content-section">
      <!-- 最近活动 -->
      <el-col :span="12">
      <el-card class="recent-activities" :title="$t('apiTesting.dashboard.operationLogs')" shadow="hover">
        <div v-if="loading" class="loading-container">
          <el-empty :description="$t('apiTesting.dashboard.loading')" />
        </div>
        <div v-else-if="operationLogs.length === 0" class="activities-list">
          <el-empty :description="$t('apiTesting.dashboard.noLogs')" />
        </div>
        <div v-else class="activities-list">
          <div v-for="log in operationLogs" :key="log.id" class="activity-item">
            <div class="activity-icon">
              <el-icon v-if="log.operation_type === 'create'" color="#52c41a"><Plus /></el-icon>
              <el-icon v-else-if="log.operation_type === 'edit'" color="#1890ff"><Edit /></el-icon>
              <el-icon v-else-if="log.operation_type === 'delete'" color="#ff4d4f"><Delete /></el-icon>
              <el-icon v-else-if="log.operation_type === 'execute'" color="#722ed1"><VideoPlay /></el-icon>
              <el-icon v-else color="#666"><Operation /></el-icon>
            </div>
            <div class="activity-content">
              <div class="activity-description">{{ log.description }}</div>
              <div class="activity-meta">
                <span class="activity-user">{{ log.user_name || $t('apiTesting.dashboard.system') }}</span>
                <span class="activity-time">{{ formatTime(log.created_at) }}</span>
              </div>
            </div>
          </div>
        </div>
      </el-card>
      </el-col>
      
      <!-- 快速操作 -->
      <el-col :span="12">
        <el-card class="quick-actions" :title="$t('apiTesting.dashboard.quickActions.title')" shadow="hover">
          <div class="actions-grid">
            <button type="button" class="action-item" @click="goToProjects">
              <div class="action-icon bg-blue">
                <el-icon><Folder /></el-icon>
              </div>
              <div class="action-label">{{ $t('apiTesting.dashboard.quickActions.projectManagement') }}</div>
            </button>
            <button type="button" class="action-item" @click="goToInterfaces">
              <div class="action-icon bg-green">
                <el-icon><Link /></el-icon>
              </div>
              <div class="action-label">{{ $t('apiTesting.dashboard.quickActions.interfaceManagement') }}</div>
            </button>
            <button type="button" class="action-item" @click="goToCases">
              <div class="action-icon bg-indigo"><el-icon><Document /></el-icon></div>
              <div class="action-label">{{ $t('menu.apiTestCases') }}</div>
            </button>
            <button type="button" class="action-item" @click="goToCreateCase">
              <div class="action-icon bg-blue"><el-icon><Edit /></el-icon></div>
              <div class="action-label">{{ $t('menu.apiCaseCreate') }}</div>
            </button>
            <button type="button" class="action-item" @click="goToAutomation">
              <div class="action-icon bg-cyan">
                <el-icon><VideoPlay /></el-icon>
              </div>
              <div class="action-label">{{ $t('apiTesting.dashboard.quickActions.automationTesting') }}</div>
            </button>
            <button type="button" class="action-item" @click="goToHistory">
              <div class="action-icon bg-purple">
                <el-icon><Timer /></el-icon>
              </div>
              <div class="action-label">{{ $t('apiTesting.dashboard.quickActions.requestHistory') }}</div>
            </button>
            <button type="button" class="action-item" @click="goToEnvironments">
              <div class="action-icon bg-orange">
                <el-icon><Setting /></el-icon>
              </div>
              <div class="action-label">{{ $t('apiTesting.dashboard.quickActions.environmentManagement') }}</div>
            </button>
            <button type="button" class="action-item" @click="goToReports">
              <div class="action-icon bg-indigo">
                <el-icon><DataAnalysis /></el-icon>
              </div>
              <div class="action-label">{{ $t('apiTesting.dashboard.quickActions.testReport') }}</div>
            </button>
          </div>
        </el-card>
      </el-col>
    </el-row>
    
    <!-- 核心功能介绍 -->
    <div class="features-section">
      <h2 class="section-title">{{ $t('apiTesting.dashboard.coreFeatures') }}</h2>
      <el-row :gutter="20">
        <el-col :span="6">
          <el-card shadow="hover" class="feature-card">
            <div class="feature-icon">
              <el-icon><Link /></el-icon>
            </div>
            <h3 class="feature-title">{{ $t('apiTesting.dashboard.features.interfaceManagement.title') }}</h3>
            <p class="feature-description">{{ $t('apiTesting.dashboard.features.interfaceManagement.description') }}</p>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="hover" class="feature-card">
            <div class="feature-icon">
              <el-icon><VideoPlay /></el-icon>
            </div>
            <h3 class="feature-title">{{ $t('apiTesting.dashboard.features.automationTesting.title') }}</h3>
            <p class="feature-description">{{ $t('apiTesting.dashboard.features.automationTesting.description') }}</p>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="hover" class="feature-card">
            <div class="feature-icon">
              <el-icon><Timer /></el-icon>
            </div>
            <h3 class="feature-title">{{ $t('apiTesting.dashboard.features.scheduledTask.title') }}</h3>
            <p class="feature-description">{{ $t('apiTesting.dashboard.features.scheduledTask.description') }}</p>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="hover" class="feature-card">
            <div class="feature-icon">
              <el-icon><DataAnalysis /></el-icon>
            </div>
            <h3 class="feature-title">{{ $t('apiTesting.dashboard.features.multiDimensionalReport.title') }}</h3>
            <p class="feature-description">{{ $t('apiTesting.dashboard.features.multiDimensionalReport.description') }}</p>
          </el-card>
        </el-col>
      </el-row>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  Folder, Link, Collection, Timer,
  VideoPlay, Setting, DataAnalysis,
  Plus, Edit, Delete, Operation, Document
} from '@element-plus/icons-vue'
import router from '@/router'
import {
  getDashboardStats,
  getOperationLogs
} from '@/api/api-testing'

const { t } = useI18n()

// 统计数据
const projectCount = ref(0)
const interfaceCount = ref(0)
const suiteCount = ref(0)
const historyCount = ref(0)
const caseCount = ref(0)
const statsCards = computed(() => [
  { path: '/api-testing/projects', label: t('apiTesting.dashboard.apiProjects'), value: projectCount.value, icon: Folder, color: 'bg-blue' },
  { path: '/api-testing/interfaces', label: t('apiTesting.dashboard.interfaceCount'), value: interfaceCount.value, icon: Link, color: 'bg-green' },
  { path: '/api-testing/test-cases', label: t('menu.apiTestCases'), value: caseCount.value, icon: Document, color: 'bg-blue' },
  { path: '/api-testing/automation', label: t('apiTesting.dashboard.testSuites'), value: suiteCount.value, icon: Collection, color: 'bg-purple' },
  { path: '/api-testing/history', label: t('apiTesting.dashboard.executionRecords'), value: historyCount.value, icon: Timer, color: 'bg-orange' }
])

const loading = ref(false)
const operationLogs = ref([])

// 加载数据
const loadDashboardData = async () => {
  loading.value = true
  try {
    // 并行加载统计数据和操作日志
    const [statsRes, logsRes] = await Promise.all([
      getDashboardStats(),
      getOperationLogs({ page_size: 20, ordering: '-created_at' })
    ])

    // 更新统计数据
    const stats = statsRes.data
    projectCount.value = stats.project_count || 0
    interfaceCount.value = stats.interface_count || 0
    suiteCount.value = stats.suite_count || 0
    historyCount.value = stats.history_count || 0
    caseCount.value = stats.case_count || 0
    
    // 更新操作日志
    operationLogs.value = logsRes.data.results || []

  } catch (error) {
    // ElMessage.error('加载仪表板数据失败')
    console.error('加载仪表板数据失败:', error)
  } finally {
    loading.value = false
  }
}

// 导航到各功能页面
const goToProjects = () => {
  router.push('/api-testing/projects')
}

const goToInterfaces = () => {
  router.push('/api-testing/interfaces')
}

const goToCases = () => router.push('/api-testing/test-cases')
const goToCreateCase = () => router.push('/api-testing/test-cases/create')

const goToAutomation = () => {
  router.push('/api-testing/automation')
}

const goToHistory = () => {
  router.push('/api-testing/history')
}

const goToEnvironments = () => {
  router.push('/api-testing/environments')
}

const goToReports = () => {
  router.push('/api-testing/reports')
}

// 格式化时间
const formatTime = (timeStr) => {
  if (!timeStr) return ''
  const date = new Date(timeStr)
  const now = new Date()
  // const diff = now - date
  if (isNaN(date.getTime())) return ''; // 处理无效日期
  const diff = Date.now() - date.getTime();

  // 小于1分钟
  if (diff < 60000) {
    return t('apiTesting.dashboard.timeFormat.justNow')
  }
  // 小于1小时
  if (diff < 3600000) {
    return t('apiTesting.dashboard.timeFormat.minutesAgo', { n: Math.floor(diff / 60000) })
  }
  // 小于1天
  if (diff < 86400000) {
    return t('apiTesting.dashboard.timeFormat.hoursAgo', { n: Math.floor(diff / 3600000) })
  }
  // 小于7天
  if (diff < 604800000) {
    return t('apiTesting.dashboard.timeFormat.daysAgo', { n: Math.floor(diff / 86400000) })
  }
  // 超过7天显示具体日期
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

// 组件挂载时加载数据
onMounted(() => {
  loadDashboardData()
})
</script>

<style scoped>
.dashboard-container {
  width: 100%;
}

.overview-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 20px;
}
.stat-navigation {
  width: 100%;
  border: 0;
  padding: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.stat-navigation:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 4px; }
@media (max-width: 1200px) { .overview-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 640px) { .overview-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }

.stats-section {
  margin-bottom: 40px;
}

.stat-card {
  height: 100%;
}

.stat-content {
  display: flex;
  align-items: center;
  height: 100px;
}

.stat-icon {
  width: 60px;
  height: 60px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-right: 20px;
  color: white;
  font-size: 24px;
}

.stat-icon :deep(.el-icon) {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.stat-icon :deep(.el-icon svg) {
  width: 100%;
  height: 60%;
}

.stat-icon.bg-blue {
  background-color: #1890ff;
}

.stat-icon.bg-green {
  background-color: #52c41a;
}

.stat-icon.bg-purple {
  background-color: #722ed1;
}

.stat-icon.bg-orange {
  background-color: #fa8c16;
}

.stat-info {
  flex: 1;
}

.stat-value {
  font-size: 28px;
  font-weight: bold;
  color: #1a1a1a;
  margin-bottom: 5px;
}

.stat-label {
  font-size: 14px;
  color: #666;
}

.content-section {
  margin-bottom: 40px;
}

.recent-activities {
  height: 100%;
}

.activities-list {
  max-height: 400px;
  overflow-y: auto;
}

.quick-actions {
  height: 100%;
}

.actions-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 15px;
}

.action-item {
  display: block;
  width: 100%;
  box-sizing: border-box;
  border: 0;
  color: inherit;
  font: inherit;
  text-align: center;
  padding: 15px 10px;
  border-radius: 8px;
  background-color: #f9f9f9;
  cursor: pointer;
  transition: all 0.3s ease;
}

.action-item:focus-visible {
  outline: 2px solid #1677a8;
  outline-offset: 2px;
}

.action-item:hover {
  background-color: #f0f0f0;
  transform: translateY(-2px);
  box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
}

.action-item .action-icon {
  margin: 0 auto 15px;
  width: 50px;
  height: 50px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-size: 24px;
}

.action-icon :deep(.el-icon) {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.action-icon :deep(.el-icon svg) {
  width: 60%;
  height: 60%;
}

.action-icon.bg-blue {
  background-color: #1890ff;
}

.action-icon.bg-green {
  background-color: #52c41a;
}

.action-icon.bg-cyan {
  background-color: #13c2c2;
}

.action-icon.bg-purple {
  background-color: #722ed1;
}

.action-icon.bg-orange {
  background-color: #fa8c16;
}

.action-icon.bg-indigo {
  background-color: #597ef7;
}

.action-label {
  font-size: 16px;
  color: #333;
  font-weight: 500;
}

.features-section {
  margin-bottom: 40px;
}

.section-title {
  font-size: 24px;
  font-weight: bold;
  margin-bottom: 20px;
  color: #1a1a1a;
}

.feature-card {
  height: 100%;
  padding: 30px;
  text-align: center;
}

.feature-icon {
  width: 80px;
  height: 80px;
  border-radius: 50%;
  background-color: #f0f0f0;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 20px;
  font-size: 36px;
  color: #1890ff;
}

.feature-icon :deep(.el-icon) {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.feature-icon :deep(.el-icon svg) {
  width: 60%;
  height: 60%;
}

.feature-title {
  font-size: 18px;
  font-weight: bold;
  margin-bottom: 10px;
  color: #1a1a1a;
}

.feature-description {
  font-size: 14px;
  color: #666;
  line-height: 1.6;
}

.loading-container {
  padding: 40px 0;
}

.activity-item {
  display: flex;
  align-items: flex-start;
  padding: 12px 0;
  border-bottom: 1px solid #f0f0f0;
}

.activity-item:last-child {
  border-bottom: none;
}

.activity-icon {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background-color: #f5f5f5;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-right: 12px;
  flex-shrink: 0;
}

.activity-icon :deep(.el-icon) {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.activity-icon :deep(.el-icon svg) {
  width: 60%;
  height: 60%;
}

.activity-content {
  flex: 1;
  min-width: 0;
}

.activity-description {
  font-size: 14px;
  color: #333;
  margin-bottom: 4px;
  word-break: break-all;
}

.activity-meta {
  display: flex;
  align-items: center;
  font-size: 12px;
  color: #999;
}

.activity-user {
  margin-right: 12px;
}

.activity-time {
  color: #bbb;
}

@media screen and (max-width: 1920px) {
  .stats-section {
    margin-bottom: 36px;
  }
  
  .stat-content {
    height: 90px;
  }
  
  .stat-icon {
    width: 55px;
    height: 55px;
    font-size: 22px;
  }
  
  .stat-value {
    font-size: 26px;
  }
  
  .content-section {
    margin-bottom: 36px;
  }
  
  .features-section {
    margin-bottom: 36px;
  }
}

@media screen and (max-width: 1600px) {
  .stats-section {
    margin-bottom: 32px;
  }
  
  .stat-content {
    height: 85px;
  }
  
  .stat-icon {
    width: 50px;
    height: 50px;
    font-size: 20px;
  }
  
  .stat-value {
    font-size: 24px;
  }
  
  .content-section {
    margin-bottom: 32px;
  }
  
  .features-section {
    margin-bottom: 32px;
  }
  
  .section-title {
    font-size: 22px;
  }
}

@media screen and (max-width: 1440px) {
  .stats-section {
    margin-bottom: 28px;
  }
  
  .stat-content {
    height: 80px;
  }
  
  .stat-icon {
    width: 48px;
    height: 48px;
    font-size: 18px;
  }
  
  .stat-value {
    font-size: 22px;
  }
  
  .content-section {
    margin-bottom: 28px;
  }
  
  .features-section {
    margin-bottom: 28px;
  }
  
  .section-title {
    font-size: 20px;
  }
  
  .actions-grid {
    gap: 12px;
  }
  
  .action-item {
    padding: 12px 8px;
  }
  
  .action-icon {
    width: 45px;
    height: 45px;
    font-size: 22px;
  }
  
  .action-label {
    font-size: 15px;
  }
}

@media screen and (max-width: 1366px) {
  .stats-section {
    margin-bottom: 24px;
  }
  
  .stat-content {
    height: 75px;
  }
  
  .stat-icon {
    width: 45px;
    height: 45px;
    font-size: 18px;
  }
  
  .stat-value {
    font-size: 20px;
  }
  
  .stat-label {
    font-size: 13px;
  }
  
  .content-section {
    margin-bottom: 24px;
  }
  
  .features-section {
    margin-bottom: 24px;
  }
  
  .section-title {
    font-size: 18px;
  }
  
  .activities-list {
    max-height: 350px;
  }
  
  .actions-grid {
    gap: 10px;
  }
  
  .action-item {
    padding: 10px 6px;
  }
  
  .action-icon {
    width: 40px;
    height: 40px;
    font-size: 20px;
  }
  
  .action-label {
    font-size: 14px;
  }
  
  .feature-card {
    padding: 20px;
  }
  
  .feature-icon {
    width: 70px;
    height: 70px;
    font-size: 32px;
  }
  
  .feature-title {
    font-size: 16px;
  }
  
  .feature-description {
    font-size: 13px;
  }
}

@media screen and (max-width: 1280px) {
  .stats-section {
    margin-bottom: 20px;
  }
  
  .stat-content {
    height: 70px;
  }
  
  .stat-icon {
    width: 42px;
    height: 42px;
    font-size: 16px;
  }
  
  .stat-value {
    font-size: 18px;
  }
  
  .stat-label {
    font-size: 12px;
  }
  
  .content-section {
    margin-bottom: 20px;
  }
  
  .features-section {
    margin-bottom: 20px;
  }
  
  .section-title {
    font-size: 18px;
  }
  
  .activities-list {
    max-height: 300px;
  }
  
  .action-item {
    padding: 8px 5px;
  }
  
  .action-icon {
    width: 38px;
    height: 38px;
    font-size: 18px;
  }
  
  .action-label {
    font-size: 13px;
  }
  
  .feature-card {
    padding: 15px;
  }
  
  .feature-icon {
    width: 60px;
    height: 60px;
    font-size: 28px;
  }
}

@media screen and (max-width: 1024px) {
  .stats-section {
    margin-bottom: 18px;
  }
  
  .stat-content {
    height: 65px;
  }
  
  .stat-icon {
    width: 40px;
    height: 40px;
    font-size: 16px;
  }
  
  .stat-value {
    font-size: 16px;
  }
  
  .stat-label {
    font-size: 12px;
  }
  
  .content-section {
    margin-bottom: 18px;
  }
  
  .features-section {
    margin-bottom: 18px;
  }
  
  .section-title {
    font-size: 16px;
  }
  
  .activities-list {
    max-height: 280px;
  }
  
  .actions-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
  }
  
  .action-item {
    padding: 10px 8px;
  }
  
  .action-label {
    font-size: 13px;
  }
  
  .feature-card {
    padding: 12px;
  }
  
  .feature-icon {
    width: 50px;
    height: 50px;
    font-size: 24px;
  }
  
  .feature-title {
    font-size: 14px;
  }
  
  .feature-description {
    font-size: 12px;
  }
}

@media screen and (max-width: 768px) {
  .stats-section {
    margin-bottom: 15px;
  }
  
  .stat-content {
    height: 60px;
  }
  
  .stat-icon {
    width: 35px;
    height: 35px;
    font-size: 14px;
  }
  
  .stat-value {
    font-size: 14px;
  }
  
  .stat-label {
    font-size: 11px;
  }
  
  .content-section {
    margin-bottom: 15px;
  }
  
  .features-section {
    margin-bottom: 15px;
  }
  
  .section-title {
    font-size: 16px;
    margin-bottom: 15px;
  }
  
  .activities-list {
    max-height: 250px;
  }
  
  .activity-item {
    padding: 10px 0;
  }
  
  .activity-icon {
    width: 28px;
    height: 28px;
  }
  
  .activity-description {
    font-size: 13px;
  }
  
  .actions-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
  }
  
  .action-item {
    padding: 8px 5px;
  }
  
  .action-icon {
    width: 35px;
    height: 35px;
    font-size: 16px;
  }
  
  .action-label {
    font-size: 12px;
  }
  
  .feature-card {
    padding: 10px;
  }
  
  .feature-icon {
    width: 45px;
    height: 45px;
    font-size: 20px;
  }
  
  .feature-title {
    font-size: 13px;
  }
  
  .feature-description {
    font-size: 11px;
  }
}

@media screen and (max-width: 480px) {
  .stats-section {
    margin-bottom: 12px;
  }
  
  .stat-content {
    height: 55px;
  }
  
  .stat-icon {
    width: 30px;
    height: 30px;
    font-size: 12px;
  }
  
  .stat-value {
    font-size: 13px;
  }
  
  .stat-label {
    font-size: 10px;
  }
  
  .content-section {
    margin-bottom: 12px;
  }
  
  .features-section {
    margin-bottom: 12px;
  }
  
  .section-title {
    font-size: 14px;
    margin-bottom: 12px;
  }
  
  .activities-list {
    max-height: 200px;
  }
  
  .activity-item {
    padding: 8px 0;
  }
  
  .activity-icon {
    width: 24px;
    height: 24px;
  }
  
  .activity-description {
    font-size: 12px;
  }
  
  .activity-meta {
    font-size: 11px;
  }
  
  .actions-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 6px;
  }
  
  .action-item {
    padding: 6px 3px;
  }
  
  .action-icon {
    width: 30px;
    height: 30px;
    font-size: 14px;
  }
  
  .action-label {
    font-size: 11px;
  }
  
  .feature-card {
    padding: 8px;
  }
  
  .feature-icon {
    width: 40px;
    height: 40px;
    font-size: 18px;
  }
  
  .feature-title {
    font-size: 12px;
  }
  
  .feature-description {
    font-size: 10px;
  }
}
</style>
