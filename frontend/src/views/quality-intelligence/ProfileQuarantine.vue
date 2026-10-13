<template>
  <div class="quality-page profile-page">
    <div class="quality-header">
      <div>
        <p class="eyebrow">{{ t('qualityIntelligence.profiles.eyebrow') }}</p>
        <h1>{{ t('qualityIntelligence.profiles.title') }}</h1>
        <p class="subtitle">{{ t('qualityIntelligence.profiles.subtitle') }}</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadProfiles">{{ t('qualityIntelligence.actions.refresh') }}</el-button>
    </div>

    <el-alert v-if="error" class="state-alert" type="error" :title="error" show-icon :closable="false" />
    <el-card shadow="never" class="table-card">
      <el-table v-loading="loading" :data="profiles" row-key="id">
        <el-table-column :label="t('qualityIntelligence.table.profile')" min-width="220">
          <template #default="{ row }">
            <div class="profile-name">{{ row.name || row.test_key }}</div>
            <div class="profile-key">{{ row.test_key }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="source_type" :label="t('qualityIntelligence.table.source')" width="100" />
        <el-table-column :label="t('qualityIntelligence.table.project')" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ row.project_type }} / {{ row.project_id }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.reliability')" width="150">
          <template #default="{ row }"><el-tag size="small" :type="reliabilityType(row.reliability_status)">{{ reliabilityLabel(row.reliability_status) }}</el-tag></template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.samples')" width="110">
          <template #default="{ row }">{{ row.eligible_attempts || 0 }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.flakyScore')" width="120">
          <template #default="{ row }">{{ formatScore(row.flaky_score) }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.lastSeen')" width="170">
          <template #default="{ row }">{{ formatDate(row.last_seen_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.actions')" width="210" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" @click="openHistory(row)">{{ t('qualityIntelligence.actions.history') }}</el-button>
            <el-button text type="warning" @click="openQuarantine(row)">{{ t('qualityIntelligence.actions.quarantine') }}</el-button>
          </template>
        </el-table-column>
        <template #empty><el-empty :description="t('qualityIntelligence.states.noProfiles')" /></template>
      </el-table>
    </el-card>

    <el-card shadow="never" class="table-card quarantine-card">
      <template #header><div class="section-heading"><span>{{ t('qualityIntelligence.profiles.quarantineTitle') }}</span><el-tag size="small" effect="plain">{{ activeQuarantines.length }}</el-tag></div></template>
      <el-table v-loading="loadingQuarantines" :data="activeQuarantines" row-key="id">
        <el-table-column prop="profile" :label="t('qualityIntelligence.table.profileId')" width="110" />
        <el-table-column prop="reason" :label="t('qualityIntelligence.table.reason')" min-width="220" show-overflow-tooltip />
        <el-table-column :label="t('qualityIntelligence.table.startsAt')" width="170"><template #default="{ row }">{{ formatDate(row.starts_at) }}</template></el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.expiresAt')" width="170"><template #default="{ row }">{{ formatDate(row.expires_at) }}</template></el-table-column>
        <el-table-column :label="t('qualityIntelligence.table.actions')" width="110"><template #default="{ row }"><el-button text type="success" @click="release(row)">{{ t('qualityIntelligence.actions.release') }}</el-button></template></el-table-column>
        <template #empty><el-empty :description="t('qualityIntelligence.states.noQuarantines')" /></template>
      </el-table>
    </el-card>

    <el-drawer v-model="historyDrawer" :title="historyProfile ? `${t('qualityIntelligence.profiles.historyTitle')} · ${historyProfile.name || historyProfile.test_key}` : t('qualityIntelligence.profiles.historyTitle')" size="620px">
      <el-skeleton v-if="historyLoading" :rows="5" animated />
      <el-table v-else :data="historyRows" size="small">
        <el-table-column prop="test_key" :label="t('qualityIntelligence.table.testKey')" min-width="160" />
        <el-table-column :label="t('qualityIntelligence.table.outcome')" width="120"><template #default="{ row }"><el-tag size="small" :type="outcomeType(row.outcome)">{{ outcomeLabel(row.outcome) }}</el-tag></template></el-table-column>
        <el-table-column prop="attempt" :label="t('qualityIntelligence.table.attempt')" width="90" />
        <el-table-column :label="t('qualityIntelligence.table.createdAt')" width="170"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column>
        <template #empty><el-empty :description="t('qualityIntelligence.states.noHistory')" /></template>
      </el-table>
    </el-drawer>

    <el-dialog v-model="quarantineDialog" :title="t('qualityIntelligence.quarantine.title')" width="460px" destroy-on-close>
      <el-form :model="quarantineForm" label-position="top">
        <el-form-item :label="t('qualityIntelligence.quarantine.reason')" required>
          <el-input v-model="quarantineForm.reason" maxlength="4096" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('qualityIntelligence.quarantine.note')">
          <el-input v-model="quarantineForm.note" type="textarea" :rows="4" maxlength="4096" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="quarantineDialog = false">{{ t('qualityIntelligence.actions.cancel') }}</el-button>
        <el-button type="primary" :loading="savingQuarantine" @click="createQuarantineRecord">{{ t('qualityIntelligence.actions.create') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { Refresh } from '@element-plus/icons-vue'
import {
  createQualityQuarantine,
  getQualityProfileHistory,
  getQualityProfiles,
  getQualityQuarantines,
  releaseQualityQuarantine
} from '@/api/quality-intelligence'

const { t } = useI18n()
const loading = ref(false)
const loadingQuarantines = ref(false)
const historyLoading = ref(false)
const savingQuarantine = ref(false)
const error = ref('')
const profiles = ref([])
const quarantines = ref([])
const historyRows = ref([])
const historyProfile = ref(null)
const historyDrawer = ref(false)
const quarantineDialog = ref(false)
const selectedProfile = ref(null)
const quarantineForm = reactive({ reason: '', note: '' })
const rowsFrom = payload => payload?.results || payload?.items || (Array.isArray(payload) ? payload : [])
const activeQuarantines = computed(() => quarantines.value.filter(item => item.status === 'ACTIVE'))
const reliabilityLabel = status => t(`qualityIntelligence.reliability.${status}`, status)
const reliabilityType = status => ({ FLAKY: 'warning', STABLE: 'success', INSUFFICIENT_DATA: 'info' }[status] || 'info')
const outcomeLabel = outcome => t(`qualityIntelligence.outcome.${outcome}`, outcome)
const outcomeType = outcome => ({ PASS: 'success', FAIL: 'danger', ERROR: 'danger', SKIP: 'info', INCONCLUSIVE: 'warning', NOT_RUN: 'info' }[outcome] || 'info')
const formatScore = value => value == null ? '-' : `${Math.round(Number(value) * 100)}%`
const formatDate = value => value ? new Date(value).toLocaleString() : '-'

const loadProfiles = async () => {
  loading.value = true
  error.value = ''
  try {
    const response = await getQualityProfiles({ page_size: 200 })
    profiles.value = rowsFrom(response.data)
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || t('qualityIntelligence.errors.loadProfiles')
  } finally {
    loading.value = false
  }
  loadQuarantines()
}

const loadQuarantines = async () => {
  loadingQuarantines.value = true
  try {
    const response = await getQualityQuarantines({ page_size: 200 })
    quarantines.value = rowsFrom(response.data)
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || t('qualityIntelligence.errors.loadQuarantines')
  } finally {
    loadingQuarantines.value = false
  }
}

const openHistory = async profile => {
  historyProfile.value = profile
  historyDrawer.value = true
  historyLoading.value = true
  try {
    const response = await getQualityProfileHistory(profile.id, { page_size: 200 })
    historyRows.value = rowsFrom(response.data)
  } catch (requestError) {
    ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.loadHistory'))
    historyRows.value = []
  } finally {
    historyLoading.value = false
  }
}

const openQuarantine = profile => {
  selectedProfile.value = profile
  quarantineForm.reason = ''
  quarantineForm.note = ''
  quarantineDialog.value = true
}

const createQuarantineRecord = async () => {
  if (!quarantineForm.reason.trim()) {
    ElMessage.warning(t('qualityIntelligence.quarantine.reasonRequired'))
    return
  }
  savingQuarantine.value = true
  try {
    await createQualityQuarantine(selectedProfile.value.id, { ...quarantineForm })
    quarantineDialog.value = false
    ElMessage.success(t('qualityIntelligence.messages.quarantineCreated'))
    await loadQuarantines()
  } catch (requestError) {
    ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.quarantine'))
  } finally {
    savingQuarantine.value = false
  }
}

const release = async quarantine => {
  try {
    await releaseQualityQuarantine(quarantine.id)
    ElMessage.success(t('qualityIntelligence.messages.quarantineReleased'))
    await loadQuarantines()
  } catch (requestError) {
    ElMessage.error(requestError?.response?.data?.detail || t('qualityIntelligence.errors.release'))
  }
}

onMounted(loadProfiles)
</script>

<style scoped>
.quality-page { color: #101828; }
.quality-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.quality-header h1 { margin: 0; font-size: 24px; font-weight: 650; }
.eyebrow { margin: 0 0 6px; color: #2f6bff; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.subtitle { margin: 7px 0 0; color: #667085; }
.table-card { margin-bottom: 16px; border-color: #eaecf0; border-radius: 8px; }
.table-card:last-of-type { margin-bottom: 0; }
.state-alert { margin-bottom: 16px; }
.section-heading { display: flex; align-items: center; justify-content: space-between; }
.profile-name { font-weight: 600; }
.profile-key { margin-top: 3px; color: #667085; font-size: 12px; }
@media (max-width: 768px) { .quality-header { flex-direction: column; } }
</style>
