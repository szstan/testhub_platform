<template>
  <div class="perf-scenario-editor" v-loading="loading">
    <!-- ---------- 顶栏 ---------- -->
    <div class="editor-header">
      <div class="header-left">
        <el-button link :icon="ArrowLeft" @click="goBack">{{ t('performanceTesting.common.back') }}</el-button>
        <el-divider direction="vertical" />
        <el-input
          v-model="form.name"
          class="name-input"
          :placeholder="t('performanceTesting.scenario.name')"
          maxlength="200"
        />
        <el-tag v-if="dirty" type="warning" size="small" effect="plain">
          {{ t('performanceTesting.editor.unsaved') }}
        </el-tag>
      </div>
      <div class="header-right">
        <el-select
          v-if="isNew"
          v-model="form.project"
          class="hd-select"
          :placeholder="t('performanceTesting.common.selectProject')"
        >
          <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
        </el-select>
        <el-select v-model="form.engine" class="hd-select-sm">
          <el-option
            v-for="opt in engineOptions"
            :key="opt.value"
            :value="opt.value"
            :disabled="!opt.available"
            :label="opt.label"
          >
            <span>{{ opt.label }}</span>
            <span v-if="!opt.available" class="engine-na">{{ t('performanceTesting.engine.unavailable') }}</span>
          </el-option>
        </el-select>
        <el-button :icon="Tools" :loading="debugging" @click="handleDebug">
          {{ t('performanceTesting.scenario.debug') }}
        </el-button>
        <el-button type="primary" :icon="Check" :loading="saving" @click="handleSave()">
          {{ t('performanceTesting.common.save') }}
        </el-button>
        <el-button type="danger" :icon="Odometer" @click="handleSaveAndExecute">
          {{ t('performanceTesting.editor.saveAndExecute') }}
        </el-button>
      </div>
    </div>

    <div class="editor-body">
      <!-- ---------- 左侧步骤列表 ---------- -->
      <div class="step-panel">
        <div class="panel-head">
          <span class="panel-title">{{ t('performanceTesting.editor.steps') }}</span>
          <span class="panel-count">{{ steps.length }}</span>
        </div>
        <div class="panel-tip">{{ t('performanceTesting.editor.dragTip') }}</div>

        <el-scrollbar class="step-scroll">
          <div v-if="!steps.length" class="step-empty">
            {{ t('performanceTesting.editor.noSteps') }}
          </div>
          <draggable
            v-else
            v-model="steps"
            item-key="_uid"
            handle=".drag-handle"
            :animation="160"
            @change="markDirty"
          >
            <template #item="{ element, index }">
              <div
                class="step-row"
                :class="{ active: index === activeIndex, disabled: !element.enabled }"
                @click="selectStep(index)"
              >
                <el-icon class="drag-handle"><Rank /></el-icon>
                <span class="step-idx">{{ index + 1 }}</span>
                <div class="step-main">
                  <div class="step-name">
                    <el-tag v-if="element.is_setup" size="small" type="warning" effect="plain">
                      {{ t('performanceTesting.editor.setupSteps') }}
                    </el-tag>
                    {{ element.name || t('performanceTesting.common.unnamed') }}
                  </div>
                  <div class="step-url">
                    <span class="method" :class="'m-' + (element.method || 'GET').toLowerCase()">
                      {{ element.method || 'GET' }}
                    </span>
                    <span class="url-text">{{ element.url || '-' }}</span>
                  </div>
                </div>
                <el-dropdown trigger="click" @command="(c) => onStepCommand(c, index)">
                  <el-icon class="step-more" @click.stop><MoreFilled /></el-icon>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="toggle">
                        {{ element.enabled ? t('performanceTesting.common.no') : t('performanceTesting.common.yes') }}
                        · {{ t('performanceTesting.editor.stepEnabled') }}
                      </el-dropdown-item>
                      <el-dropdown-item command="copy">{{ t('performanceTesting.common.copy') }}</el-dropdown-item>
                      <el-dropdown-item command="delete" divided>
                        {{ t('performanceTesting.common.delete') }}
                      </el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </template>
          </draggable>
        </el-scrollbar>

        <div class="panel-foot">
          <el-button size="small" :icon="Plus" @click="addStep">
            {{ t('performanceTesting.editor.addStep') }}
          </el-button>
          <el-button size="small" :icon="Download" @click="openImport">
            {{ t('performanceTesting.editor.importFromApi') }}
          </el-button>
        </div>
      </div>

      <!-- ---------- 右侧主区 ---------- -->
      <div class="main-panel">
        <el-tabs v-model="activeTab" class="main-tabs">
          <!-- 请求编排 -->
          <el-tab-pane :label="t('performanceTesting.editor.tabRequest')" name="request">
            <div v-if="!currentStep" class="tab-empty">
              {{ t('performanceTesting.editor.noSteps') }}
            </div>
            <StepEditor
              v-else
              :key="currentStep._uid"
              v-model="steps[activeIndex]"
              :upload-files="uploadFiles"
              @update:modelValue="markDirty"
              @upload-file="handleUploadFile"
            />
          </el-tab-pane>

          <!-- 压力策略 -->
          <el-tab-pane :label="t('performanceTesting.editor.tabLoad')" name="load">
            <!-- Locust/JMeter 引擎只实现了固定并发模型，非固定并发会被预检拦截，
                 提前在这里提示，避免用户配了半天策略最后才发现不生效（脚本模式不受限） -->
            <el-alert
              v-if="engineModelMismatch"
              type="warning"
              show-icon
              :closable="false"
              class="engine-model-alert"
              :title="t('performanceTesting.editor.engineModelMismatch', { engine: t('performanceTesting.engine.' + form.engine) })"
            />
            <LoadProfileEditor v-model="form.load_config" :limits="limits" @update:modelValue="markDirty" />
          </el-tab-pane>

          <!-- 变量与环境 -->
          <el-tab-pane :label="t('performanceTesting.editor.tabEnv')" name="env">
            <div class="pane-body">
              <div class="block-title">{{ t('performanceTesting.editor.envTitle') }}</div>
              <el-form label-width="140px" class="env-form">
                <el-form-item :label="t('performanceTesting.editor.baseUrl')">
                  <el-input
                    v-model="form.env_config.base_url"
                    placeholder="https://api.example.com"
                    @input="markDirty"
                  />
                  <div class="form-tip">{{ t('performanceTesting.editor.baseUrlTip') }}</div>
                </el-form-item>
                <el-form-item :label="t('performanceTesting.editor.globalHeaders')">
                  <KeyValueEditor v-model="globalHeaders" @update:modelValue="onGlobalHeaders" />
                </el-form-item>
                <el-row :gutter="16">
                  <el-col :span="12">
                    <el-form-item :label="t('performanceTesting.editor.verifySsl')">
                      <el-switch v-model="form.env_config.verify_ssl" @change="markDirty" />
                    </el-form-item>
                  </el-col>
                  <el-col :span="12">
                    <el-form-item :label="t('performanceTesting.editor.keepAlive')">
                      <el-switch v-model="form.runtime_config.keep_alive" @change="markDirty" />
                    </el-form-item>
                  </el-col>
                </el-row>
                <el-row :gutter="16">
                  <el-col :span="12">
                    <el-form-item :label="t('performanceTesting.editor.timeout')">
                      <el-input-number
                        v-model="form.runtime_config.timeout"
                        :min="1"
                        :max="300"
                        controls-position="right"
                        @change="markDirty"
                      />
                    </el-form-item>
                  </el-col>
                  <el-col :span="12">
                    <el-form-item :label="t('performanceTesting.editor.sampleInterval')">
                      <el-input-number
                        v-model="form.runtime_config.sample_interval"
                        :min="1"
                        :max="60"
                        controls-position="right"
                        @change="markDirty"
                      />
                    </el-form-item>
                  </el-col>
                </el-row>
                <el-form-item :label="t('performanceTesting.editor.proxy')">
                  <el-input
                    v-model="form.runtime_config.proxy"
                    placeholder="http://127.0.0.1:8888"
                    @input="markDirty"
                  />
                </el-form-item>
              </el-form>

              <div class="block-title">
                {{ t('performanceTesting.editor.variables') }}
                <span class="block-tip">{{ t('performanceTesting.editor.variableTip') }}</span>
              </div>
              <el-table :data="form.variables" size="small" border class="var-table">
                <el-table-column :label="t('performanceTesting.editor.variableName')" width="180">
                  <template #default="{ row }">
                    <el-input v-model="row.name" size="small" @input="markDirty" />
                  </template>
                </el-table-column>
                <el-table-column :label="t('performanceTesting.editor.variableType')" width="150">
                  <template #default="{ row }">
                    <el-select v-model="row.type" size="small" @change="markDirty">
                      <el-option v-for="vt in variableTypes" :key="vt" :label="vt" :value="vt" />
                    </el-select>
                  </template>
                </el-table-column>
                <el-table-column :label="t('performanceTesting.editor.variableValue')">
                  <template #default="{ row }">
                    <!-- 固定值 -->
                    <el-input
                      v-if="row.type === 'CONSTANT'"
                      v-model="row.value"
                      size="small"
                      :type="row.secret ? 'password' : 'text'"
                      show-password
                      @input="markDirty"
                    />
                    <!-- 随机整数 -->
                    <div v-else-if="row.type === 'RANDOM_INT'" class="inline-fields">
                      <el-input-number v-model="row.min" size="small" controls-position="right" @change="markDirty" />
                      <span class="sep">~</span>
                      <el-input-number v-model="row.max" size="small" controls-position="right" @change="markDirty" />
                    </div>
                    <!-- 随机字符串 -->
                    <div v-else-if="row.type === 'RANDOM_STRING'" class="inline-fields">
                      <el-input-number
                        v-model="row.length"
                        :min="1"
                        :max="256"
                        size="small"
                        controls-position="right"
                        @change="markDirty"
                      />
                      <el-select v-model="row.charset" size="small" style="width: 120px" @change="markDirty">
                        <el-option label="alnum" value="alnum" />
                        <el-option label="alpha" value="alpha" />
                        <el-option label="digit" value="digit" />
                      </el-select>
                    </div>
                    <!-- 枚举轮询 -->
                    <div v-else-if="row.type === 'ENUM'" class="inline-fields">
                      <el-select
                        v-model="row.values"
                        multiple
                        filterable
                        allow-create
                        default-first-option
                        size="small"
                        style="flex: 1"
                        :placeholder="t('performanceTesting.editor.variableValue')"
                        @change="markDirty"
                      />
                      <el-select v-model="row.strategy" size="small" style="width: 130px" @change="markDirty">
                        <el-option label="ROUND_ROBIN" value="ROUND_ROBIN" />
                        <el-option label="RANDOM" value="RANDOM" />
                      </el-select>
                    </div>
                    <!-- 时间戳 -->
                    <el-select
                      v-else-if="row.type === 'TIMESTAMP'"
                      v-model="row.format"
                      size="small"
                      @change="markDirty"
                    >
                      <el-option label="ms" value="ms" />
                      <el-option label="s" value="s" />
                    </el-select>
                    <!-- CSV -->
                    <div v-else-if="row.type === 'CSV'" class="inline-fields">
                      <el-select
                        v-model="row.data_file_id"
                        size="small"
                        style="flex: 1"
                        :placeholder="t('performanceTesting.editor.varCsv')"
                        @change="markDirty"
                      >
                        <el-option v-for="f in dataFiles" :key="f.id" :label="f.name" :value="f.id" />
                      </el-select>
                      <el-select
                        v-model="row.column"
                        size="small"
                        style="width: 140px"
                        clearable
                        @change="markDirty"
                      >
                        <el-option v-for="c in columnsOf(row.data_file_id)" :key="c" :label="c" :value="c" />
                      </el-select>
                    </div>
                    <span v-else class="auto-hint">{{ row.type }}</span>
                  </template>
                </el-table-column>
                <el-table-column width="70" align="center">
                  <template #header>
                    <el-tooltip :content="t('performanceTesting.editor.variableValue')" placement="top">
                      <span>🔒</span>
                    </el-tooltip>
                  </template>
                  <template #default="{ row }">
                    <el-switch v-model="row.secret" size="small" @change="markDirty" />
                  </template>
                </el-table-column>
                <el-table-column width="70" align="center" :label="t('performanceTesting.common.actions')">
                  <template #default="{ $index }">
                    <el-button link type="danger" :icon="Delete" @click="removeVariable($index)" />
                  </template>
                </el-table-column>
              </el-table>
              <el-button size="small" :icon="Plus" class="add-row-btn" @click="addVariable">
                {{ t('performanceTesting.editor.addRow') }}
              </el-button>
            </div>
          </el-tab-pane>

          <!-- SLA 阈值 -->
          <el-tab-pane :label="t('performanceTesting.editor.tabSla')" name="sla">
            <div class="pane-body">
              <el-form label-width="200px" class="sla-form">
                <el-form-item :label="t('performanceTesting.sla.enable')">
                  <el-switch v-model="form.sla_config.enabled" @change="markDirty" />
                </el-form-item>
                <template v-if="form.sla_config.enabled">
                  <el-form-item :label="t('performanceTesting.sla.p95ResponseTime')">
                    <el-input-number
                      v-model="form.sla_config.thresholds.p95_response_time"
                      :min="0"
                      :step="100"
                      controls-position="right"
                      @change="markDirty"
                    />
                  </el-form-item>
                  <el-form-item :label="t('performanceTesting.sla.avgResponseTime')">
                    <el-input-number
                      v-model="form.sla_config.thresholds.avg_response_time"
                      :min="0"
                      :step="100"
                      controls-position="right"
                      @change="markDirty"
                    />
                  </el-form-item>
                  <el-form-item :label="t('performanceTesting.sla.errorRate')">
                    <el-input-number
                      v-model="form.sla_config.thresholds.error_rate"
                      :min="0"
                      :max="100"
                      :precision="2"
                      :step="0.1"
                      controls-position="right"
                      @change="markDirty"
                    />
                  </el-form-item>
                  <el-form-item :label="t('performanceTesting.sla.minTps')">
                    <el-input-number
                      v-model="form.sla_config.thresholds.min_tps"
                      :min="0"
                      :step="10"
                      controls-position="right"
                      @change="markDirty"
                    />
                  </el-form-item>
                  <el-form-item :label="t('performanceTesting.sla.abortOnBreach')">
                    <el-switch v-model="form.sla_config.abort_on_breach" @change="markDirty" />
                    <div class="form-tip">{{ t('performanceTesting.sla.abortTip') }}</div>
                  </el-form-item>
                  <el-form-item
                    v-if="form.sla_config.abort_on_breach"
                    :label="t('performanceTesting.sla.breachWindow')"
                  >
                    <el-input-number
                      v-model="form.sla_config.breach_window"
                      :min="1"
                      :max="600"
                      controls-position="right"
                      @change="markDirty"
                    />
                    <div class="form-tip">{{ t('performanceTesting.sla.breachWindowTip') }}</div>
                  </el-form-item>
                </template>
              </el-form>
            </div>
          </el-tab-pane>

          <!-- 验收目标 -->
          <el-tab-pane label="验收目标" name="targets">
            <div class="pane-body">
              <el-form label-width="200px" class="sla-form">
                <div class="form-tip" style="margin-bottom: 16px;">
                  验收目标用于判定压测结果是否"通过"。执行完成后自动评估，结果在报告中显示。
                  留空表示不评估该指标。
                </div>
                <el-form-item label="P95 响应时间上限 (ms)">
                  <el-input-number
                    v-model="form.perf_targets.max_p95_rt"
                    :min="0"
                    :step="100"
                    placeholder="如 2000"
                    controls-position="right"
                    @change="markDirty"
                  />
                  <span class="form-tip" style="margin-left: 8px;">任一步骤 P95 超过即未通过</span>
                </el-form-item>
                <el-form-item label="平均响应时间上限 (ms)">
                  <el-input-number
                    v-model="form.perf_targets.max_avg_rt"
                    :min="0"
                    :step="100"
                    placeholder="如 1000"
                    controls-position="right"
                    @change="markDirty"
                  />
                  <span class="form-tip" style="margin-left: 8px;">任一步骤平均响应时间超过即未通过</span>
                </el-form-item>
                <el-form-item label="TPS 下限">
                  <el-input-number
                    v-model="form.perf_targets.min_tps"
                    :min="0"
                    :step="10"
                    placeholder="如 100"
                    controls-position="right"
                    @change="markDirty"
                  />
                  <span class="form-tip" style="margin-left: 8px;">整体 TPS 低于即未通过</span>
                </el-form-item>
                <el-form-item label="错误率上限 (%)">
                  <el-input-number
                    v-model="form.perf_targets.max_error_rate"
                    :min="0"
                    :max="100"
                    :precision="2"
                    :step="0.1"
                    placeholder="如 1.0"
                    controls-position="right"
                    @change="markDirty"
                  />
                  <span class="form-tip" style="margin-left: 8px;">任一步骤错误率超过即未通过</span>
                </el-form-item>
              </el-form>
            </div>
          </el-tab-pane>

          <!-- JMeter 脚本（仅 JMeter 引擎可见） -->
          <el-tab-pane v-if="isJmeter" :label="t('performanceTesting.script.tab')" name="script">
            <div class="pane-body">
              <div class="block-title">
                {{ t('performanceTesting.script.title') }}
                <span class="block-tip">{{ t('performanceTesting.script.tip') }}</span>
              </div>

              <el-radio-group v-model="scriptMode" class="script-mode" @change="onScriptModeChange">
                <el-radio-button value="scenario">{{ t('performanceTesting.script.modeScenario') }}</el-radio-button>
                <el-radio-button value="script">{{ t('performanceTesting.script.modeScript') }}</el-radio-button>
              </el-radio-group>
              <div class="form-tip">
                {{ scriptMode === 'script'
                  ? t('performanceTesting.script.modeScriptTip')
                  : t('performanceTesting.script.modeScenarioTip') }}
              </div>

              <template v-if="scriptMode === 'script'">
                <div class="script-toolbar">
                  <el-select
                    v-model="selectedScriptId"
                    class="script-select"
                    filterable
                    clearable
                    :placeholder="t('performanceTesting.script.selectPlaceholder')"
                    @change="onScriptSelected"
                  >
                    <el-option v-for="f in scriptFiles" :key="f.id" :label="f.name" :value="f.id">
                      <span>{{ f.name }}</span>
                      <span class="script-opt-meta">
                        {{ (f.meta && f.meta.sampler_count) || 0 }} {{ t('performanceTesting.script.samplers') }}
                      </span>
                    </el-option>
                  </el-select>
                  <el-upload
                    :show-file-list="false"
                    accept=".jmx"
                    :before-upload="handleJmxUpload"
                    :disabled="!form.project"
                  >
                    <el-button :icon="Upload" :loading="uploadingScript" :disabled="!form.project">
                      {{ t('performanceTesting.script.upload') }}
                    </el-button>
                  </el-upload>
                  <el-button
                    :icon="Delete"
                    :disabled="!selectedScriptId"
                    @click="handleDeleteScript"
                  >
                    {{ t('performanceTesting.script.remove') }}
                  </el-button>
                  <el-button :icon="Refresh" @click="loadScriptFiles(form.project)">
                    {{ t('performanceTesting.common.refresh') }}
                  </el-button>
                </div>
                <div v-if="!form.project" class="form-tip warn">
                  {{ t('performanceTesting.scenario.projectRequired') }}
                </div>

                <el-alert
                  v-if="selectedScript"
                  type="info"
                  :closable="false"
                  show-icon
                  class="script-alert"
                  :title="t('performanceTesting.script.overrideNotice')"
                />

                <el-descriptions
                  v-if="selectedScriptMeta"
                  :column="2"
                  border
                  size="small"
                  class="script-desc"
                >
                  <el-descriptions-item :label="t('performanceTesting.script.planName')">
                    {{ selectedScriptMeta.test_plan_name || '-' }}
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('performanceTesting.script.jmeterVersion')">
                    {{ selectedScriptMeta.jmeter_version || '-' }}
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('performanceTesting.script.totalThreads')">
                    {{ selectedScriptMeta.total_threads === null
                      ? t('performanceTesting.script.dynamic')
                      : selectedScriptMeta.total_threads }}
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('performanceTesting.script.maxDuration')">
                    {{ selectedScriptMeta.max_duration === null
                      ? t('performanceTesting.script.dynamic')
                      : formatDuration(selectedScriptMeta.max_duration || 0) }}
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('performanceTesting.script.samplerCount')">
                    {{ selectedScriptMeta.sampler_count || 0 }}
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('performanceTesting.script.hosts')">
                    {{ (selectedScriptMeta.hosts || []).join('、') || t('performanceTesting.script.dynamic') }}
                  </el-descriptions-item>
                </el-descriptions>

                <el-table
                  v-if="selectedScriptMeta && (selectedScriptMeta.thread_groups || []).length"
                  :data="selectedScriptMeta.thread_groups"
                  size="small"
                  border
                  class="script-table"
                >
                  <el-table-column prop="name" :label="t('performanceTesting.script.tgName')" min-width="160" />
                  <el-table-column prop="type" :label="t('performanceTesting.script.tgType')" width="180" />
                  <el-table-column :label="t('performanceTesting.script.tgThreads')" width="100">
                    <template #default="{ row }">{{ row.num_threads === null ? '—' : row.num_threads }}</template>
                  </el-table-column>
                  <el-table-column :label="t('performanceTesting.script.tgRamp')" width="100">
                    <template #default="{ row }">{{ row.ramp_time === null ? '—' : row.ramp_time }}</template>
                  </el-table-column>
                  <el-table-column :label="t('performanceTesting.script.tgDuration')" width="110">
                    <template #default="{ row }">{{ row.duration === null ? '—' : row.duration }}</template>
                  </el-table-column>
                  <el-table-column :label="t('performanceTesting.script.tgEnabled')" width="90">
                    <template #default="{ row }">
                      <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">
                        {{ row.enabled ? t('performanceTesting.script.on') : t('performanceTesting.script.off') }}
                      </el-tag>
                    </template>
                  </el-table-column>
                </el-table>

                <div
                  v-if="selectedScriptMeta && selectedScriptMeta.has_dynamic_props"
                  class="form-tip warn"
                >
                  {{ t('performanceTesting.script.dynamicWarn') }}
                </div>
                <el-empty
                  v-if="!selectedScriptId"
                  :description="t('performanceTesting.script.empty')"
                />
              </template>
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </div>

    <!-- ---------- 从接口导入 ---------- -->
    <ImportFromApiDrawer v-model="importVisible" :scenario-id="scenarioId" @imported="onImported" />

    <!-- ---------- 调试结果 ---------- -->
    <el-drawer v-model="debugVisible" :title="t('performanceTesting.editor.debugTitle')" size="60%">
      <div v-if="debugging" class="debug-loading">{{ t('performanceTesting.editor.debugRunning') }}</div>
      <template v-else-if="debugResult">
        <el-alert
          :type="debugResult.passed ? 'success' : 'error'"
          :closable="false"
          show-icon
          :title="debugResult.passed
            ? t('performanceTesting.editor.debugPassed')
            : t('performanceTesting.editor.debugFailed', { count: debugResult.failed_count })"
          :description="debugResult.engine === 'JMETER'
            ? (debugResult.jmx_valid
                ? t('performanceTesting.editor.jmxValid')
                : t('performanceTesting.editor.jmxInvalid'))
            : `${t('performanceTesting.editor.debugElapsed')}: ${debugResult.elapsed_ms} ms`"
        />
        <el-collapse class="debug-list">
          <el-collapse-item v-for="(s, i) in debugResult.steps" :key="i" :name="i">
            <template #title>
              <el-icon :class="(s.success ?? s.ok) ? 'ok' : 'bad'">
                <component :is="(s.success ?? s.ok) ? CircleCheck : CircleClose" />
              </el-icon>
              <span class="dbg-name">{{ i + 1 }}. {{ s.name }}</span>
              <el-tag size="small" effect="plain">{{ s.method || (debugResult.engine === 'JMETER' ? 'JMX' : '') }}</el-tag>
              <template v-if="s.status_code !== undefined">
                <span class="dbg-code" :class="{ bad: s.status_code >= 400 || !s.status_code }">
                  {{ s.status_code }}
                </span>
              </template>
              <template v-else-if="debugResult.engine === 'JMETER'">
                <span class="dbg-code">校验</span>
              </template>
              <span v-if="s.elapsed_ms !== undefined" class="dbg-time">{{ s.elapsed_ms }} ms</span>
            </template>
            <div class="dbg-body">
              <div class="dbg-line"><b>URL</b><span>{{ s.url }}</span></div>
              <div v-if="s.error" class="dbg-line err"><b>Error</b><span>{{ s.error }}</span></div>
              <div v-if="s.assertion_message" class="dbg-line err">
                <b>{{ t('performanceTesting.editor.debugAssertion') }}</b><span>{{ s.assertion_message }}</span>
              </div>
              <div v-if="s.extracted && Object.keys(s.extracted).length" class="dbg-line">
                <b>{{ t('performanceTesting.editor.debugExtracted') }}</b>
                <span>{{ JSON.stringify(s.extracted) }}</span>
              </div>
              <div class="dbg-sub">{{ t('performanceTesting.editor.debugRequest') }}</div>
              <pre class="dbg-pre">{{ prettify(s.request_body) || '-' }}</pre>
              <div class="dbg-sub">{{ t('performanceTesting.editor.debugResponse') }}</div>
              <pre class="dbg-pre">{{ prettify(s.response_body) || '-' }}</pre>
            </div>
          </el-collapse-item>
        </el-collapse>
      </template>
      <el-empty v-else :description="t('performanceTesting.editor.debugEmpty')" />
    </el-drawer>

    <!-- ---------- 执行确认 ---------- -->
    <el-dialog v-model="execVisible" :title="t('performanceTesting.execute.title')" width="560px">
      <div v-loading="preflighting">
        <el-alert
          v-if="preflight"
          :type="preflight.passed ? 'success' : 'error'"
          :closable="false"
          show-icon
          :title="preflight.passed
            ? t('performanceTesting.execute.preflightPassed')
            : t('performanceTesting.execute.preflightFailed')"
        />
        <ul v-if="preflight && preflight.errors.length" class="pf-list err">
          <li v-for="(e, i) in preflight.errors" :key="'e' + i">{{ e }}</li>
        </ul>
        <ul v-if="preflight && preflight.warnings.length" class="pf-list warn">
          <li v-for="(w, i) in preflight.warnings" :key="'w' + i">{{ w }}</li>
        </ul>
        <el-descriptions v-if="preflight" :column="2" border size="small" class="pf-desc">
          <el-descriptions-item :label="t('performanceTesting.execute.estimatedPeak')">
            {{ preflight.estimated.peak_concurrency }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('performanceTesting.execute.estimatedDuration')">
            {{ formatDuration(preflight.estimated.planned_duration) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('performanceTesting.execute.estimatedRequests')">
            {{ (preflight.estimated.estimated_requests || 0).toLocaleString() }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('performanceTesting.editor.steps')">
            {{ preflight.estimated.step_count }}
          </el-descriptions-item>
        </el-descriptions>
        <div class="pf-confirm">{{ t('performanceTesting.execute.confirmTip') }}</div>
      </div>
      <template #footer>
        <el-button @click="execVisible = false">{{ t('performanceTesting.common.cancel') }}</el-button>
        <el-button
          type="danger"
          :disabled="!preflight || !preflight.passed"
          :loading="executing"
          @click="doExecute"
        >
          {{ t('performanceTesting.execute.start') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft, Check, Odometer, Tools, Plus, Delete, Download, Upload, Refresh,
  Rank, MoreFilled, CircleCheck, CircleClose
} from '@element-plus/icons-vue'
import draggable from 'vuedraggable'

import KeyValueEditor from '@/views/api-testing/components/KeyValueEditor.vue'
import StepEditor from './components/StepEditor.vue'
import LoadProfileEditor from './components/LoadProfileEditor.vue'
import ImportFromApiDrawer from './components/ImportFromApiDrawer.vue'
import { formatDuration, apiError } from './shared'
import {
  getEngineStatus, getPerfProjects, getPerfScenario, createPerfScenario,
  updatePerfScenario, savePerfScenarioSteps, preflightPerfScenario,
  executePerfScenario, debugPerfScenario, getPerfDataFiles,
  uploadPerfJmxScript, uploadPerfUploadFile, deletePerfDataFile
} from '@/api/performance-testing'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const VARIABLE_TYPES = ['CONSTANT', 'RANDOM_INT', 'RANDOM_STRING', 'ENUM', 'UUID', 'TIMESTAMP', 'CSV']
const variableTypes = VARIABLE_TYPES

const scenarioId = ref(Number(route.params.id) || 0)
const isNew = computed(() => !scenarioId.value)

const loading = ref(false)
const saving = ref(false)
const debugging = ref(false)
const executing = ref(false)
const preflighting = ref(false)
const dirty = ref(false)

const activeTab = ref('request')
const activeIndex = ref(-1)
const steps = ref([])
const projects = ref([])
const dataFiles = ref([])
// 步骤 multipart 上传文件（file_type=UPLOAD），下发给 StepEditor 做行内文件选择
const uploadFiles = ref([])
const engines = ref([])
const limits = ref({
  max_concurrency: 1000, max_target_rps: 100000, max_duration: 3600, max_concurrent_executions: 5
})

const importVisible = ref(false)
const debugVisible = ref(false)
const debugResult = ref(null)
const execVisible = ref(false)
const preflight = ref(null)

const globalHeaders = ref([])

const form = reactive({
  project: null,
  name: '',
  description: '',
  engine: 'BUILTIN',
  enabled: true,
  load_config: { model: 'CONCURRENCY', concurrency: 50, duration: 300, ramp_up: 30 },
  sla_config: { enabled: false, thresholds: {}, abort_on_breach: false, breach_window: 10 },
  perf_targets: { max_p95_rt: null, max_avg_rt: null, min_tps: null, max_error_rate: null },
  variables: [],
  env_config: { base_url: '', headers: {}, verify_ssl: false },
  runtime_config: { timeout: 30, sample_interval: 1, keep_alive: true, proxy: '' }
})

const currentStep = computed(() => steps.value[activeIndex.value] || null)

// ------------------------------------------------------------------ //
// JMeter 脚本模式：与后端 script_ref = { mode, data_file_id } 一一对应
// ------------------------------------------------------------------ //
const MAX_JMX_SIZE = 10 * 1024 * 1024

const scriptMode = ref('scenario')
const scriptFiles = ref([])
const selectedScriptId = ref(null)
const uploadingScript = ref(false)

const isJmeter = computed(() => form.engine === 'JMETER')
const isScriptMode = computed(() => isJmeter.value && scriptMode.value === 'script')
const selectedScript = computed(
  () => scriptFiles.value.find(f => f.id === selectedScriptId.value) || null)
const selectedScriptMeta = computed(() => selectedScript.value?.meta || null)

// Locust/JMeter 引擎仅实现固定并发压力模型（与后端 preflight 拦截规则一致）；
// JMeter 脚本模式的压力参数来自 .jmx 本身，不受此限制。
const engineModelMismatch = computed(() =>
  ['LOCUST', 'JMETER'].includes(form.engine)
  && (form.load_config?.model || 'CONCURRENCY') !== 'CONCURRENCY'
  && !isScriptMode.value)

const engineOptions = computed(() => {
  const list = engines.value.length ? engines.value : [{ value: 'BUILTIN', available: true }]
  return list.map(e => ({
    value: e.value || e.key || e.name,
    available: e.available !== false,
    label: t(`performanceTesting.engine.${e.value || e.key || e.name}`)
  }))
})

let uidSeq = 1
function withUid(step) {
  return { ...step, _uid: `s${uidSeq++}` }
}

function markDirty() {
  dirty.value = true
}

// ------------------------------------------------------------------ //
// 加载
// ------------------------------------------------------------------ //
async function loadEngineStatus() {
  try {
    const { data } = await getEngineStatus()
    engines.value = (data.engines || []).map(e =>
      typeof e === 'string' ? { value: e, available: true } : e)
    if (data.limits) limits.value = data.limits
  } catch (e) {
    engines.value = [{ value: 'BUILTIN', available: true }]
  }
}

async function loadProjects() {
  try {
    const res = await getPerfProjects({ page_size: 200 })
    projects.value = res.data.results || res.data || []
  } catch (e) {
    projects.value = []
  }
}

async function loadDataFiles(projectId) {
  if (!projectId) { dataFiles.value = []; return }
  try {
    // 必须带 file_type=CSV：JMX 脚本与 CSV 参数化文件共用一张表，
    // 不过滤会把 .jmx 混进变量的 CSV 文件下拉里
    const res = await getPerfDataFiles({ project: projectId, file_type: 'CSV', page_size: 200 })
    dataFiles.value = res.data.results || res.data || []
  } catch (e) {
    dataFiles.value = []
  }
}

// 只查 file_type=JMX，CSV 参数化文件走 loadDataFiles，两者共用同一张表
async function loadScriptFiles(projectId) {
  if (!projectId) { scriptFiles.value = []; return }
  try {
    const res = await getPerfDataFiles({ project: projectId, file_type: 'JMX', page_size: 200 })
    scriptFiles.value = res.data.results || res.data || []
  } catch (e) {
    scriptFiles.value = []
  }
  // 选中的脚本可能已被他人删除，避免界面停在一个不存在的 id 上
  if (selectedScriptId.value && !scriptFiles.value.some(f => f.id === selectedScriptId.value)) {
    selectedScriptId.value = null
  }
}

// 步骤 multipart 文件候选列表：file_type=UPLOAD，与 CSV/JMX 同表不同类型互不干扰
async function loadUploadFiles(projectId) {
  if (!projectId) { uploadFiles.value = []; return }
  try {
    const res = await getPerfDataFiles({ project: projectId, file_type: 'UPLOAD', page_size: 200 })
    uploadFiles.value = res.data.results || res.data || []
  } catch (e) {
    uploadFiles.value = []
  }
}

// StepEditor 行内选中新文件后回调：上传成功即把新文件 id 回填到对应行，
// 用户无需再手动从下拉里选一次
async function handleUploadFile({ file, row }) {
  if (!form.project) {
    ElMessage.warning(t('performanceTesting.scenario.projectRequired'))
    return
  }
  try {
    const { data } = await uploadPerfUploadFile({ project: form.project, file, name: file.name })
    await loadUploadFiles(form.project)
    if (row) {
      row.file_id = data.id
      row.filename = data.name || file.name
      row.content_type = file.type || ''
    }
    markDirty()
    ElMessage.success(t('performanceTesting.script.uploadSuccess'))
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.editor.fileUpload')))
  }
}

function applyScenario(data) {
  form.project = data.project
  form.name = data.name
  form.description = data.description || ''
  form.engine = data.engine || 'BUILTIN'
  form.enabled = data.enabled !== false
  form.load_config = data.load_config && Object.keys(data.load_config).length
    ? { ...data.load_config }
    : { model: 'CONCURRENCY', concurrency: 50, duration: 300, ramp_up: 30 }
  form.sla_config = {
    enabled: false, thresholds: {}, abort_on_breach: false, breach_window: 10,
    ...(data.sla_config || {})
  }
  form.sla_config.thresholds = { ...(form.sla_config.thresholds || {}) }
  form.perf_targets = {
    max_p95_rt: null, max_avg_rt: null, min_tps: null, max_error_rate: null,
    ...(data.perf_targets || {})
  }
  form.variables = (data.variables || []).map(v => ({ ...v }))
  form.env_config = { base_url: '', headers: {}, verify_ssl: false, ...(data.env_config || {}) }
  form.runtime_config = {
    timeout: 30, sample_interval: 1, keep_alive: true, proxy: '', ...(data.runtime_config || {})
  }
  // 脚本选择持久化在 runtime_config.script_ref，避免刷新后丢失
  const savedRef = form.runtime_config.script_ref || {}
  scriptMode.value = savedRef.mode === 'script' ? 'script' : 'scenario'
  selectedScriptId.value = savedRef.data_file_id || null
  globalHeaders.value = Object.entries(form.env_config.headers || {})
    .map(([key, value]) => ({ enabled: true, key, value, description: '' }))
  steps.value = (data.steps || []).map(withUid)
  activeIndex.value = steps.value.length ? 0 : -1
}

// 首屏灌数据期间置位，避免 watch 里的联动再触发一次重复请求
let hydrating = false

async function load() {
  hydrating = true
  try {
    await Promise.all([loadEngineStatus(), loadProjects()])
    if (isNew.value) {
      form.project = Number(route.query.project) || projects.value[0]?.id || null
      form.name = ''
      await Promise.all([loadDataFiles(form.project), loadScriptFiles(form.project), loadUploadFiles(form.project)])
      dirty.value = true
      return
    }
    loading.value = true
    try {
      const { data } = await getPerfScenario(scenarioId.value)
      applyScenario(data)
      await Promise.all([loadDataFiles(form.project), loadScriptFiles(form.project), loadUploadFiles(form.project)])
      dirty.value = false
    } catch (e) {
      ElMessage.error(apiError(e, t('performanceTesting.common.empty')))
    } finally {
      loading.value = false
    }
  } finally {
    hydrating = false
  }
}

watch(() => form.project, (pid) => {
  if (hydrating) return
  selectedScriptId.value = null
  loadDataFiles(pid)
  loadScriptFiles(pid)
  loadUploadFiles(pid)
})

watch(() => form.engine, (val) => {
  if (hydrating) return
  if (val !== 'JMETER') {
    // 非 JMeter 引擎不支持脚本模式，回落到步骤编排，顺手把 Tab 切回来
    scriptMode.value = 'scenario'
    if (activeTab.value === 'script') activeTab.value = 'request'
  } else if (!scriptFiles.value.length) {
    loadScriptFiles(form.project)
  }
})

// ------------------------------------------------------------------ //
// 步骤操作
// ------------------------------------------------------------------ //
function selectStep(index) {
  activeIndex.value = index
  activeTab.value = 'request'
}

function addStep() {
  steps.value.push(withUid({
    name: `Step ${steps.value.length + 1}`,
    enabled: true,
    is_setup: false,
    method: 'GET',
    url: '',
    headers: {},
    params: {},
    body_type: 'NONE',
    body: '',
    files: [],
    extractors: [],
    assertions: [],
    think_time: 0,
    weight: 1
  }))
  activeIndex.value = steps.value.length - 1
  activeTab.value = 'request'
  markDirty()
}

function onStepCommand(command, index) {
  const step = steps.value[index]
  if (command === 'toggle') {
    step.enabled = !step.enabled
    markDirty()
  } else if (command === 'copy') {
    steps.value.splice(index + 1, 0, withUid({ ...step, name: `${step.name} copy` }))
    markDirty()
  } else if (command === 'delete') {
    steps.value.splice(index, 1)
    if (activeIndex.value >= steps.value.length) activeIndex.value = steps.value.length - 1
    markDirty()
  }
}

function onGlobalHeaders(rows) {
  const obj = {}
  ;(rows || []).forEach(r => {
    if (r && r.enabled !== false && r.key) obj[r.key] = r.value
  })
  form.env_config.headers = obj
  markDirty()
}

// ------------------------------------------------------------------ //
// 变量
// ------------------------------------------------------------------ //
function addVariable() {
  form.variables.push({ name: '', type: 'CONSTANT', value: '', secret: false })
  markDirty()
}

function removeVariable(index) {
  form.variables.splice(index, 1)
  markDirty()
}

function columnsOf(fileId) {
  const file = dataFiles.value.find(f => f.id === fileId)
  return file?.columns || []
}

// ------------------------------------------------------------------ //
// JMeter 脚本
// ------------------------------------------------------------------ //
function onScriptModeChange() {
  if (isScriptMode.value) loadScriptFiles(form.project)
  markDirty()
}

function onScriptSelected() {
  markDirty()
}

// before-upload 返回 false，用自己的接口上传，避免 el-upload 走默认 action
async function handleJmxUpload(file) {
  if (!form.project) {
    ElMessage.warning(t('performanceTesting.scenario.projectRequired'))
    return false
  }
  if (!/\.jmx$/i.test(file.name || '')) {
    ElMessage.warning(t('performanceTesting.script.invalidExt'))
    return false
  }
  if (file.size > MAX_JMX_SIZE) {
    ElMessage.warning(t('performanceTesting.script.tooLarge'))
    return false
  }
  uploadingScript.value = true
  try {
    const { data } = await uploadPerfJmxScript({ project: form.project, file, name: file.name })
    await loadScriptFiles(form.project)
    if (data && data.id) selectedScriptId.value = data.id
    markDirty()
    ElMessage.success(t('performanceTesting.script.uploadSuccess'))
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.script.upload')))
  } finally {
    uploadingScript.value = false
  }
  return false
}

async function handleDeleteScript() {
  const target = selectedScript.value
  if (!target) return
  try {
    await ElMessageBox.confirm(
      t('performanceTesting.script.deleteConfirm', { name: target.name }),
      t('performanceTesting.common.confirm'),
      { type: 'warning' }
    )
  } catch (e) {
    return
  }
  try {
    await deletePerfDataFile(target.id)
    selectedScriptId.value = null
    await loadScriptFiles(form.project)
    markDirty()
    ElMessage.success(t('performanceTesting.common.deleteSuccess'))
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.common.delete')))
  }
}

// 只上报 data_file_id：jmx 真实路径由服务端从 PerfDataFile 反查并校验，
// 前端传路径等于开一个任意文件读取口子
function buildScriptRef() {
  if (isScriptMode.value && selectedScriptId.value) {
    return { mode: 'script', data_file_id: selectedScriptId.value }
  }
  return { mode: 'scenario' }
}

function ensureScriptSelected() {
  if (isScriptMode.value && !selectedScriptId.value) {
    ElMessage.warning(t('performanceTesting.script.needSelect'))
    activeTab.value = 'script'
    return false
  }
  return true
}

// ------------------------------------------------------------------ //
// 保存 / 调试 / 执行
// ------------------------------------------------------------------ //
function buildPayload() {
  return {
    project: form.project,
    name: (form.name || '').trim(),
    description: form.description,
    engine: form.engine,
    enabled: form.enabled,
    load_config: form.load_config,
    sla_config: form.sla_config,
    variables: form.variables,
    env_config: form.env_config,
    // 脚本选择随场景一起持久化，定时压测等触发路径才能复用同一份配置
    runtime_config: { ...form.runtime_config, script_ref: buildScriptRef() }
  }
}

function stepsPayload() {
  return steps.value.map((s, idx) => {
    const copy = { ...s, order: idx }
    delete copy._uid
    delete copy.id
    delete copy.scenario
    return copy
  })
}

async function handleSave(silent = false) {
  if (!(form.name || '').trim()) {
    ElMessage.warning(t('performanceTesting.scenario.nameRequired'))
    return false
  }
  if (!form.project) {
    ElMessage.warning(t('performanceTesting.scenario.projectRequired'))
    return false
  }
  saving.value = true
  try {
    if (isNew.value) {
      const { data: created } = await createPerfScenario(buildPayload())
      scenarioId.value = created.id
      router.replace(`/performance-testing/scenarios/${created.id}`)
    } else {
      await updatePerfScenario(scenarioId.value, buildPayload())
    }
    await savePerfScenarioSteps(scenarioId.value, stepsPayload())
    dirty.value = false
    if (!silent) ElMessage.success(t('performanceTesting.common.saveSuccess'))
    return true
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.common.save')))
    return false
  } finally {
    saving.value = false
  }
}

async function handleDebug() {
  if (dirty.value || isNew.value) {
    const ok = await handleSave(true)
    if (!ok) return
  }
  debugVisible.value = true
  debugging.value = true
  debugResult.value = null
  try {
    const { data } = await debugPerfScenario(scenarioId.value, {})
    debugResult.value = data
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.scenario.debug')))
    debugVisible.value = false
  } finally {
    debugging.value = false
  }
}

async function handleSaveAndExecute() {
  if (!ensureScriptSelected()) return
  const ok = await handleSave(true)
  if (!ok) return
  execVisible.value = true
  preflighting.value = true
  preflight.value = null
  try {
    const { data } = await preflightPerfScenario(scenarioId.value,
                                                 { script_ref: buildScriptRef() })
    preflight.value = data
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.execute.preflightTitle')))
    execVisible.value = false
  } finally {
    preflighting.value = false
  }
}

async function doExecute() {
  if (!ensureScriptSelected()) return
  executing.value = true
  try {
    const { data } = await executePerfScenario(scenarioId.value,
                                               { script_ref: buildScriptRef() })
    ElMessage.success(t('performanceTesting.execute.started'))
    execVisible.value = false
    router.push(`/performance-testing/executions/${data.execution.id}/monitor`)
  } catch (e) {
    ElMessage.error(apiError(e, t('performanceTesting.execute.start')))
  } finally {
    executing.value = false
  }
}

function openImport() {
  if (isNew.value) {
    ElMessage.warning(t('performanceTesting.editor.saveFirst'))
    return
  }
  importVisible.value = true
}

async function onImported() {
  const { data } = await getPerfScenario(scenarioId.value)
  steps.value = (data.steps || []).map(withUid)
  activeIndex.value = steps.value.length ? steps.value.length - 1 : -1
  dirty.value = false
}

function prettify(text) {
  if (!text) return ''
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch (e) {
    return String(text).slice(0, 20000)
  }
}

function goBack() {
  router.push('/performance-testing/scenarios')
}

// 未保存拦截：压测场景配置很重，误退一次要重配十分钟
onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm(
      t('performanceTesting.editor.unsaved'),
      t('performanceTesting.common.confirm'),
      { type: 'warning' }
    )
    return true
  } catch (e) {
    return false
  }
})

function beforeUnloadGuard(event) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => {
  load()
  window.addEventListener('beforeunload', beforeUnloadGuard)
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnloadGuard))
</script>

<style lang="scss" scoped>
.perf-scenario-editor {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 110px);
  background: #f5f7fa;
}

.editor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;

  .header-left {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    min-width: 0;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .name-input {
    max-width: 320px;
    :deep(.el-input__wrapper) {
      box-shadow: none;
      background: #f5f7fa;
    }
  }

  .hd-select { width: 180px; }
  .hd-select-sm { width: 130px; }
  .engine-na { float: right; color: #c0c4cc; font-size: 12px; }
}

.editor-body {
  flex: 1;
  display: flex;
  gap: 12px;
  padding: 12px;
  overflow: hidden;
}

.step-panel {
  width: 320px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-radius: 6px;
  overflow: hidden;

  .panel-head {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px 14px 4px;
    .panel-title { font-weight: 600; color: #303133; }
    .panel-count {
      background: #f0f2f5;
      color: #909399;
      font-size: 12px;
      border-radius: 9px;
      padding: 0 8px;
    }
  }

  .panel-tip { padding: 0 14px 8px; font-size: 12px; color: #c0c4cc; }
  .step-scroll { flex: 1; }
  .step-empty { padding: 32px 14px; text-align: center; color: #c0c4cc; font-size: 13px; }

  .panel-foot {
    display: flex;
    gap: 8px;
    padding: 10px 14px;
    border-top: 1px solid #f0f2f5;
  }
}

.step-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  margin: 0 8px 6px;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s;

  &:hover { background: #f5f7fa; }
  &.active { background: #ecf5ff; border-color: #1890ff; }
  &.disabled { opacity: 0.5; }

  .drag-handle { cursor: move; color: #c0c4cc; }
  .step-idx { width: 18px; font-size: 12px; color: #909399; }
  .step-main { flex: 1; min-width: 0; }
  .step-name {
    font-size: 13px;
    color: #303133;
    display: flex;
    align-items: center;
    gap: 4px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .step-url {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: #909399;
    margin-top: 2px;
    .method { font-weight: 600; }
    .m-get { color: #1890ff; }
    .m-post { color: #52c41a; }
    .m-put { color: #faad14; }
    .m-delete { color: #f5222d; }
    .url-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  }
  .step-more { color: #c0c4cc; &:hover { color: #1890ff; } }
}

.main-panel {
  flex: 1;
  min-width: 0;
  background: #fff;
  border-radius: 6px;
  overflow: hidden;
  display: flex;
  flex-direction: column;

  .main-tabs {
    flex: 1;
    display: flex;
    flex-direction: column;
    :deep(.el-tabs__header) { margin: 0; padding: 0 14px; }
    :deep(.el-tabs__content) { flex: 1; overflow: auto; padding: 14px; }
  }
}

.tab-empty { padding: 60px 0; text-align: center; color: #c0c4cc; }
.pane-body { max-width: 900px; }
.block-title {
  font-weight: 600;
  color: #303133;
  margin: 4px 0 14px;
  padding-left: 8px;
  border-left: 3px solid #1890ff;
  .block-tip { margin-left: 8px; font-weight: 400; font-size: 12px; color: #c0c4cc; }
}
.form-tip {
  font-size: 12px;
  color: #c0c4cc;
  line-height: 1.5;
  &.warn { color: #faad14; }
}
.env-form { margin-bottom: 24px; }

/* 引擎与压力模型不兼容告警：与压力曲线编辑器保持间距 */
.engine-model-alert { margin-bottom: 12px; }

/* JMeter 脚本模式 */
.script-mode { margin-bottom: 6px; }
.script-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 16px 0 8px;
}
.script-select { width: 340px; }
.script-opt-meta { float: right; color: #c0c4cc; font-size: 12px; }
.script-alert { margin: 8px 0 12px; }
.script-desc { margin-bottom: 14px; }
.script-table { margin-bottom: 10px; }
.var-table { margin-bottom: 8px; }
.add-row-btn { width: 100%; border-style: dashed; }
.inline-fields { display: flex; align-items: center; gap: 6px; .sep { color: #c0c4cc; } }
.auto-hint { color: #c0c4cc; font-size: 12px; }

.debug-loading { padding: 40px; text-align: center; color: #909399; }
.debug-list {
  margin-top: 12px;
  .ok { color: #52c41a; margin-right: 6px; }
  .bad { color: #f5222d; margin-right: 6px; }
  .dbg-name { flex: 1; margin-right: 8px; }
  .dbg-code { margin: 0 10px; font-weight: 600; color: #52c41a; &.bad { color: #f5222d; } }
  .dbg-time { color: #909399; font-size: 12px; }
}
.dbg-body { font-size: 13px; }
.dbg-line {
  display: flex;
  gap: 10px;
  padding: 3px 0;
  b { width: 80px; flex-shrink: 0; color: #909399; font-weight: 500; }
  span { word-break: break-all; }
  &.err span { color: #f5222d; }
}
.dbg-sub { margin: 10px 0 4px; font-weight: 600; color: #606266; }
.dbg-pre {
  margin: 0;
  padding: 8px 10px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 12px;
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.pf-list {
  margin: 10px 0;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.8;
  &.err { color: #f5222d; }
  &.warn { color: #faad14; }
}
.pf-desc { margin-top: 12px; }
.pf-confirm { margin-top: 12px; font-size: 12px; color: #909399; }
</style>
