<template>
  <div class="test-case-manager">
    <div class="page-header">
      <h1 class="page-title">{{ t('uiAutomation.testCase.title') }}</h1>
      <div class="header-actions">
        <el-select v-model="projectId" :aria-label="t('uiAutomation.project.selectProject')" :placeholder="t('uiAutomation.project.selectProject')" :disabled="projectsLoading" style="width: 200px">
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
        <el-button type="primary" :disabled="!projectId" @click="openCreateCase">
          <el-icon><Plus /></el-icon>
          {{ t('uiAutomation.testCase.newTestCase') }}
        </el-button>
      </div>
    </div>

    <p v-if="projectsLoading" class="case-page-state" role="status">{{ t('uiAutomation.dashboard.loading') }}</p>
    <div v-else-if="projectsError" class="case-page-state" role="alert">
      <p>{{ t('uiAutomation.project.messages.loadFailed') }}</p>
      <el-button @click="reloadProjects">{{ t('uiAutomation.workbenchExecution.retry') }}</el-button>
    </div>
    <el-empty v-else-if="!projectId" :description="t(invalidProject ? 'uiAutomation.workbenchExecution.invalidProject' : projects.length ? 'uiAutomation.common.selectProject' : 'uiAutomation.workbenchExecution.noProjects')">
      <RouterLink v-if="!projects.length" :to="{ name: 'UiProjects', query: { setup: 'project' } }" class="el-button el-button--primary">
        {{ t('uiAutomation.project.newProject') }}
      </RouterLink>
    </el-empty>
    <div v-else class="main-content">
      <!-- 左侧：测试用例列表 -->
      <div class="left-panel">
        <div class="panel-header">
          <h3>{{ t('uiAutomation.testCase.testCaseList') }}</h3>
          <el-input
            v-model="searchKeyword"
            :placeholder="t('uiAutomation.testCase.searchPlaceholder')"
            clearable
            size="small"
            style="width: 200px"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </div>

        <div class="test-case-list" :aria-busy="casesLoading">
          <p v-if="casesLoading" class="case-page-state" role="status">{{ t('uiAutomation.dashboard.loading') }}</p>
          <div v-else-if="casesError" class="case-page-state" role="alert">
            <p>{{ t('uiAutomation.workbenchOnboarding.casesError') }}</p>
            <el-button @click="loadTestCases">{{ t('uiAutomation.workbenchExecution.retry') }}</el-button>
          </div>
          <el-empty v-else-if="!filteredTestCases.length" :description="t(testCases.length ? 'uiAutomation.workbenchOnboarding.noMatches' : 'uiAutomation.workbenchOnboarding.noCases')">
            <el-button v-if="!testCases.length" type="primary" @click="openCreateCase">{{ t('uiAutomation.testCase.newTestCase') }}</el-button>
          </el-empty>
          <div
            v-for="testCase in filteredTestCases"
            :key="testCase.id"
            class="test-case-item"
            :class="{ active: selectedTestCase?.id === testCase.id }"
            @click="selectTestCase(testCase)"
          >
            <div class="case-header">
              <div class="case-info">
                <h4 class="case-name">
                  <button type="button" :aria-pressed="selectedTestCase?.id === testCase.id" @click.stop="selectTestCase(testCase)">{{ testCase.name }}</button>
                </h4>
                <p class="case-description">{{ testCase.description || t('uiAutomation.testCase.noDescription') }}</p>
              </div>
              <div class="case-actions">
                <el-button size="small" text @click.stop="editTestCase(testCase)">
                  <el-icon><Edit /></el-icon>
                </el-button>
                <el-button size="small" text @click.stop="copyTestCase(testCase)">
                  <el-icon><CopyDocument /></el-icon>
                </el-button>
                <el-button size="small" text type="danger" @click.stop="deleteTestCase(testCase)">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
            </div>
            <div class="case-meta">
              <!-- 移除状态显示 -->
              <span class="step-count">{{ testCase.steps?.length || 0 }} {{ t('uiAutomation.testCase.stepsCount') }}</span>
              <span class="update-time">{{ formatTime(testCase.updated_at) }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 右侧：测试用例详情和步骤编辑 -->
      <div class="right-panel">
        <div v-if="selectedTestCase" class="test-case-detail">
          <div class="detail-header">
            <h3>{{ selectedTestCase.name }}</h3>
            <div class="detail-actions">
              <el-button size="small" @click="addStep">
                <el-icon><Plus /></el-icon>
                {{ t('uiAutomation.testCase.addStep') }}
              </el-button>
              <el-button size="small" type="primary" @click="saveTestCase" :loading="savingSteps" :disabled="isRunning">
                <el-icon><Check /></el-icon>
                {{ t('uiAutomation.testCase.saveTestCase') }}
              </el-button>
              <el-select v-model="selectedEngine" :disabled="readinessLoading || isRunning" :aria-label="t('uiAutomation.testCase.selectEngine')" size="small" style="width: 130px">
                <el-option v-for="item in engineOptions" :key="item.value" :label="item.label" :value="item.value" :disabled="item.disabled" />
              </el-select>
              <el-select v-model="selectedBrowser" :disabled="readinessLoading || isRunning" :aria-label="t('uiAutomation.testCase.selectBrowser')" size="small" style="width: 120px">
                <el-option v-for="item in browserOptions" :key="item.value" :label="item.label" :value="item.value" :disabled="item.disabled" />
              </el-select>
              <el-select v-model="headlessMode" :disabled="isRunning" :aria-label="t('uiAutomation.testCase.runModeLabel')" size="small" style="width: 110px">
                <el-option :label="t('uiAutomation.testCase.headedMode')" :value="false" />
                <el-option :label="t('uiAutomation.testCase.headlessMode')" :value="true" />
              </el-select>
              <el-button size="small" type="success" @click="runTestCase(selectedTestCase)" :loading="isRunning" :disabled="!canRunSelectedCase">
                <el-icon v-if="!isRunning"><CaretRight /></el-icon>
                {{ isRunning ? t('uiAutomation.testCase.running') : t('uiAutomation.testCase.runLabel') }}
              </el-button>
              <el-button size="small" v-if="executionResult" @click="toggleView">
                <el-icon><component :is="showSteps ? View : Edit" /></el-icon>
                {{ showSteps ? t('uiAutomation.testCase.viewResult') : t('uiAutomation.testCase.editSteps') }}
              </el-button>
            </div>
          </div>
          <p v-if="!selectedTestCase.steps?.length" class="case-guide">
            {{ t('uiAutomation.workbenchOnboarding.addSteps') }}
            <RouterLink :to="{ name: 'UiElementsEnhanced', query: { project: String(projectId), caseId: String(selectedTestCase.id) } }">
              {{ t('uiAutomation.dashboard.elementManagement') }}
            </RouterLink>
          </p>
          <p v-else-if="hasUnsavedSteps" class="case-guide">{{ t('uiAutomation.workbenchOnboarding.saveSteps') }}</p>
          <p v-if="readinessLoading" class="case-guide" role="status">{{ t('uiAutomation.workbenchExecution.checkingReadiness') }}</p>
          <div v-else-if="readinessError || readinessReason" class="case-guide" role="alert">
            {{ readinessError ? t('uiAutomation.workbenchExecution.readinessUnavailable') : readinessReason }}
            <el-button link type="primary" @click="loadReadiness">{{ t('uiAutomation.workbenchExecution.retry') }}</el-button>
          </div>
          <div v-if="elementsError" class="case-guide" role="alert">
            {{ t('uiAutomation.workbenchOnboarding.elementsError') }}
            <el-button link type="primary" @click="loadElements(projectId, projectGeneration)">{{ t('uiAutomation.workbenchExecution.retry') }}</el-button>
          </div>
          <p v-if="sourceExecutionLocation" class="case-guide">
            {{ t('uiAutomation.workbenchExecution.repairHint') }}
            <RouterLink :to="sourceExecutionLocation">{{ t('uiAutomation.workbenchExecution.originalExecution') }}</RouterLink>
          </p>
          <section v-if="currentRun" class="case-guide run-tracking" role="status">
            <strong>{{ runPhaseLabel }}</strong>
            <span v-if="currentRun.executionId"> #{{ currentRun.executionId }}</span>
            <p v-if="currentRun.phase === 'unknown'">{{ t('uiAutomation.workbenchExecution.submissionUnknown') }}</p>
            <p v-else-if="currentRun.phase === 'paused'">{{ t('uiAutomation.workbenchExecution.trackingPaused') }}</p>
            <p v-if="currentRun.error">{{ currentRun.error }}</p>
            <RouterLink v-if="currentRun.executionId" :to="uiExecutionLocation('UiExecutions', projectId, currentRun.executionId)">{{ t('uiAutomation.workbenchOnboarding.viewThisExecution') }}</RouterLink>
            <el-button v-if="['unknown', 'paused'].includes(currentRun.phase)" link type="primary" @click="resumeRun">{{ t('uiAutomation.workbenchExecution.reconnectRun') }}</el-button>
            <el-button v-if="currentRun.phase === 'unknown' && !currentRun.executionId" link type="primary" @click="retrySameRun">{{ t('uiAutomation.workbenchExecution.retrySameSubmission') }}</el-button>
            <el-button v-if="currentRun.executionId" link type="danger" :loading="currentRun.cancelling" @click="cancelRun">{{ t('uiAutomation.workbenchExecution.cancelRun') }}</el-button>
          </section>

          <!-- 测试步骤编辑 -->
          <div class="steps-container" v-show="showSteps">
            <div class="steps-header">
              <h4>{{ t('uiAutomation.testCase.testSteps') }}</h4>
              <el-button size="small" text @click="expandAllSteps">
                {{ allStepsExpanded ? t('uiAutomation.testCase.foldAll') : t('uiAutomation.testCase.expandAll') }}
              </el-button>
            </div>

            <div class="steps-scroll-container">
              <div class="steps-list">
                <draggable
                  v-model="currentSteps"
                  item-key="id"
                  handle=".drag-handle"
                  @change="onStepsReorder"
                >
                  <template #item="{ element, index }">
                    <div :id="`ui-case-step-${index + 1}`" class="step-item" :class="{ expanded: element.expanded, 'repair-step': repairStep === index + 1 }">
                      <div class="step-header">
                        <div class="step-left">
                          <el-icon class="drag-handle"><Rank /></el-icon>
                          <span class="step-number">{{ index + 1 }}</span>
                          <el-select
                            v-model="element.action_type"
                            :placeholder="t('uiAutomation.testCase.selectAction')"
                            size="small"
                            style="width: 120px"
                            @change="onActionTypeChange(element)"
                          >
                            <el-option :label="t('uiAutomation.testCase.actionClick')" value="click" />
                            <el-option :label="t('uiAutomation.testCase.actionFill')" value="fill" />
                            <el-option :label="t('uiAutomation.testCase.actionGetText')" value="getText" />
                            <el-option :label="t('uiAutomation.testCase.actionWaitFor')" value="waitFor" />
                            <el-option :label="t('uiAutomation.testCase.actionHover')" value="hover" />
                            <el-option :label="t('uiAutomation.testCase.actionScroll')" value="scroll" />
                            <el-option :label="t('uiAutomation.testCase.actionScreenshot')" value="screenshot" />
                            <el-option :label="t('uiAutomation.testCase.actionAssert')" value="assert" />
                            <el-option :label="t('uiAutomation.testCase.actionWait')" value="wait" />
                            <el-option :label="t('uiAutomation.testCase.actionSwitchTab')" value="switchTab" />
                          </el-select>
                          <el-select
                            v-if="needsElement(element.action_type)"
                            v-model="element.element_id"
                            :placeholder="t('uiAutomation.testCase.selectElement')"
                            size="small"
                            style="width: 200px"
                            filterable
                            @change="onElementChange(element)"
                          >
                            <el-option
                              v-for="elem in availableElements"
                              :key="elem.id"
                              :label="`${elem.name} (${elem.locator_value})`"
                              :value="elem.id"
                            />
                          </el-select>
                        </div>
                        <div class="step-right">
                          <el-button
                            size="small"
                            text
                            @click="element.expanded = !element.expanded"
                          >
                            <el-icon>
                              <component :is="element.expanded ? 'ArrowUp' : 'ArrowDown'" />
                            </el-icon>
                          </el-button>
                          <el-button size="small" text type="danger" @click="removeStep(index)">
                            <el-icon><Delete /></el-icon>
                          </el-button>
                        </div>
                      </div>

                      <div v-if="element.expanded" class="step-content">
                        <!-- 输入参数 -->
                        <div v-if="needsInputValue(element.action_type)" class="step-param">
                          <label>{{ t('uiAutomation.testCase.inputValue') }}</label>
                          <div style="display: flex; gap: 5px; flex: 1">
                            <el-input
                              v-model="element.input_value"
                              :placeholder="element.action_type === 'switchTab' ? t('uiAutomation.testCase.switchTabPlaceholder') : t('uiAutomation.testCase.inputPlaceholder')"
                              size="small"
                            >
                              <template #append>
                                <el-button
                                  size="small"
                                  :icon="MagicStick"
                                  @click="openDataFactorySelector(element, 'input_value')"
                                  :title="t('uiAutomation.testCase.referenceDataFactory')"
                                  class="data-factory-btn"
                                />
                              </template>
                            </el-input>
                            <el-tooltip :content="t('uiAutomation.testCase.insertVariable')" placement="top" v-if="element.action_type !== 'switchTab'">
                              <el-button size="small" @click="openVariableHelper(element, 'input_value')" class="variable-helper-btn">
                                <el-icon><MagicStick /></el-icon>
                              </el-button>
                            </el-tooltip>
                          </div>
                        </div>

                        <!-- 等待时间 -->
                        <div v-if="needsWaitTime(element.action_type)" class="step-param">
                          <label>{{ t('uiAutomation.testCase.waitTime') }}</label>
                          <el-input-number
                            v-model="element.wait_time"
                            :min="100"
                            :max="30000"
                            :step="100"
                            size="small"
                          />
                        </div>

                        <!-- 断言参数 -->
                        <div v-if="element.action_type === 'assert'" class="step-param">
                          <label>{{ t('uiAutomation.testCase.assertType') }}</label>
                          <el-select v-model="element.assert_type" size="small" style="width: 150px">
                            <el-option :label="t('uiAutomation.testCase.assertTextContains')" value="textContains" />
                            <el-option :label="t('uiAutomation.testCase.assertTextEquals')" value="textEquals" />
                            <el-option :label="t('uiAutomation.testCase.assertIsVisible')" value="isVisible" />
                            <el-option :label="t('uiAutomation.testCase.assertExists')" value="exists" />
                            <el-option :label="t('uiAutomation.testCase.assertHasAttribute')" value="hasAttribute" />
                          </el-select>
                          <div style="display: flex; align-items: center; margin-left: 10px; width: 240px">
                            <el-input
                              v-model="element.assert_value"
                              :placeholder="t('uiAutomation.testCase.expectedValue')"
                              size="small"
                              style="flex: 1"
                            >
                              <template #append>
                                <el-button
                                  size="small"
                                  :icon="MagicStick"
                                  @click="openDataFactorySelector(element, 'assert_value')"
                                  :title="t('uiAutomation.testCase.referenceDataFactory')"
                                  class="data-factory-btn"
                                />
                              </template>
                            </el-input>
                            <el-tooltip :content="t('uiAutomation.testCase.insertVariable')" placement="top">
                              <el-button size="small" style="margin-left: 5px" @click="openVariableHelper(element, 'assert_value')" class="variable-helper-btn">
                                <el-icon><MagicStick /></el-icon>
                              </el-button>
                            </el-tooltip>
                          </div>
                        </div>

                        <!-- 步骤描述 -->
                        <div class="step-param">
                          <label>{{ t('uiAutomation.testCase.stepDescription') }}</label>
                          <el-input
                            v-model="element.description"
                            :placeholder="t('uiAutomation.testCase.stepDescPlaceholder')"
                            size="small"
                          />
                        </div>
                      </div>
                    </div>
                  </template>
                </draggable>
              </div>
            </div>
          </div>

          <!-- 执行结果 -->
          <div v-if="executionResult" class="execution-result" v-show="!showSteps">
            <div class="result-header">
              <h4>{{ t('uiAutomation.testCase.executionResult') }}</h4>
              <el-tag :type="executionResult.status === 'aborted' ? 'info' : executionResult.success ? 'success' : 'danger'">
                {{ executionResult.status === 'aborted' ? t('uiAutomation.workbenchExecution.aborted') : executionResult.success ? t('uiAutomation.testCase.executionSuccess') : t('uiAutomation.testCase.executionFailed') }}
              </el-tag>
              <RouterLink v-if="executionResult.executionId" :to="uiExecutionLocation('UiExecutions', projectId, executionResult.executionId)">
                {{ t('uiAutomation.workbenchOnboarding.viewThisExecution') }}
              </RouterLink>
            </div>
            <p v-if="executionResult.preflight" class="case-guide" role="alert">
              {{ t('uiAutomation.workbenchOnboarding.preflight') }}
            </p>
            <div class="result-content">
              <el-tabs v-model="resultActiveTab">
                <el-tab-pane :label="t('uiAutomation.testCase.executionLogs')" name="logs">
                  <div class="logs-container">
                    <div v-if="parsedExecutionLogs.length > 0">
                      <div v-for="(step, index) in parsedExecutionLogs" :key="index" class="log-item">
                        <div class="log-header">
                          <el-tag :type="step.success ? 'success' : 'danger'" size="small">
                            {{ t('uiAutomation.testCase.step') }} {{ step.step_number }}
                          </el-tag>
                          <span class="log-action">{{ getActionText(step.action_type) }}</span>
                          <span class="log-desc">{{ step.description }}</span>
                        </div>
                        <div v-if="step.error" class="log-error">
                          <el-icon><WarningFilled /></el-icon>
                          <pre class="error-message">{{ step.error }}</pre>
                        </div>
                      </div>
                    </div>
                    <pre v-else-if="plainExecutionLog" class="plain-case-log">{{ plainExecutionLog }}</pre>
                    <el-empty v-else :description="t('uiAutomation.testCase.noLogs')" />
                  </div>
                </el-tab-pane>
                <el-tab-pane :label="t('uiAutomation.testCase.failedScreenshots')" name="screenshots" v-if="executionResult.screenshots && executionResult.screenshots.length > 0">
                  <div class="screenshots-container">
                    <div
                      v-for="(screenshot, index) in executionResult.screenshots"
                      :key="index"
                      class="screenshot-item"
                      @click="previewScreenshot(screenshot)"
                    >
                      <div class="screenshot-wrapper">
                        <img
                          :src="screenshot.url"
                          :alt="`${t('uiAutomation.testCase.screenshot')} ${index + 1}`"
                          :data-index="index"
                          @error="handleImageError"
                          @load="handleImageLoad"
                        />
                        <div class="screenshot-placeholder" v-if="!screenshot.loaded">
                          <el-icon><Picture /></el-icon>
                          <span>{{ t('uiAutomation.testCase.loadingImage') }}</span>
                        </div>
                        <div class="screenshot-error" v-if="screenshot.error">
                          <el-icon><Warning /></el-icon>
                          <span>{{ t('uiAutomation.testCase.imageLoadFailed') }}</span>
                        </div>
                        <div class="screenshot-overlay">
                          <el-icon class="zoom-icon"><ZoomIn /></el-icon>
                        </div>
                      </div>
                      <div class="screenshot-info">
                        <p class="screenshot-description">{{ screenshot.description || t('uiAutomation.testCase.screenshot') + ' ' + (index + 1) }}</p>
                        <p class="screenshot-meta" v-if="screenshot.step_number">{{ t('uiAutomation.testCase.step') }} {{ screenshot.step_number }}</p>
                        <p class="screenshot-time" v-if="screenshot.timestamp">{{ formatTime(screenshot.timestamp) }}</p>
                      </div>
                    </div>
                  </div>
                </el-tab-pane>
                <el-tab-pane :label="t('uiAutomation.testCase.errorInfo')" name="errors" v-if="executionResult.errors && executionResult.errors.length > 0">
                  <div class="errors-container">
                    <div
                      v-for="(error, index) in executionResult.errors"
                      :key="index"
                      class="error-item"
                    >
                      <div class="error-header">
                        <el-tag type="danger" size="large">
                          <el-icon><WarningFilled /></el-icon>
                          {{ error.message || error }}
                        </el-tag>
                        <span v-if="error.step_number" class="error-step">
                          {{ t('uiAutomation.testCase.step') }} {{ error.step_number }}
                        </span>
                      </div>

                      <div v-if="error.action_type || error.element || error.description" class="error-meta">
                        <div v-if="error.action_type" class="meta-item">
                          <span class="meta-label">{{ t('uiAutomation.testCase.operationType') }}</span>
                          <span class="meta-value">{{ error.action_type }}</span>
                        </div>
                        <div v-if="error.element" class="meta-item">
                          <span class="meta-label">{{ t('uiAutomation.testCase.targetElement') }}</span>
                          <span class="meta-value">{{ error.element }}</span>
                        </div>
                        <div v-if="error.description" class="meta-item">
                          <span class="meta-label">{{ t('uiAutomation.testCase.stepDesc') }}</span>
                          <span class="meta-value">{{ error.description }}</span>
                        </div>
                      </div>

                      <div v-if="error.details || error.stack" class="error-details">
                        <div class="details-header">{{ t('uiAutomation.testCase.detailErrorInfo') }}</div>
                        <pre class="details-content">{{ error.details || error.stack }}</pre>
                      </div>
                    </div>
                  </div>
                </el-tab-pane>
              </el-tabs>
            </div>
          </div>
        </div>

        <div v-else class="no-selection">
          <el-empty :description="t('uiAutomation.testCase.selectTestCase')" />
        </div>
      </div>
    </div>

    <!-- 新建/编辑测试用例对话框 -->
    <el-dialog
      v-model="showCreateDialog"
      :title="editingTestCase ? t('uiAutomation.testCase.editTestCase') : t('uiAutomation.testCase.createTestCase')"
      :close-on-click-modal="false"
      :before-close="requestCloseCaseForm"
      width="min(500px, calc(100vw - 32px))"
      @closed="closeCaseForm"
    >
      <el-form :model="testCaseForm" label-width="100px">
        <el-form-item :label="t('uiAutomation.testCase.caseName')" required>
          <el-input v-model="testCaseForm.name" :placeholder="t('uiAutomation.testCase.caseNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('uiAutomation.testCase.caseDescription')">
          <el-input
            v-model="testCaseForm.description"
            type="textarea"
            :rows="3"
            :placeholder="t('uiAutomation.testCase.caseDescPlaceholder')"
          />
        </el-form-item>
        <el-form-item :label="t('uiAutomation.testCase.priority')">
          <el-select v-model="testCaseForm.priority" style="width: 100%">
            <el-option :label="t('uiAutomation.testCase.priorityHigh')" value="high" />
            <el-option :label="t('uiAutomation.testCase.priorityMedium')" value="medium" />
            <el-option :label="t('uiAutomation.testCase.priorityLow')" value="low" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button :disabled="savingForm" @click="requestCloseCaseForm">{{ t('uiAutomation.common.cancel') }}</el-button>
          <el-button type="primary" :loading="savingForm" @click="saveTestCaseForm">{{ t('uiAutomation.common.confirm') }}</el-button>
        </span>
      </template>
    </el-dialog>

    <el-dialog :model-value="unsavedVisible" :title="t('uiAutomation.workbenchExecution.unsavedTitle')" width="min(460px, calc(100vw - 32px))" :close-on-click-modal="false" :close-on-press-escape="!unsavedSaving" :show-close="!unsavedSaving" @update:model-value="value => !value && chooseUnsaved('cancel')">
      <p>{{ t('uiAutomation.workbenchExecution.unsavedDescription') }}</p>
      <p v-if="saveRequestPending" role="status">{{ t('uiAutomation.workbenchExecution.savePendingNavigation') }}</p>
      <template #footer>
        <el-button :disabled="unsavedSaving" @click="chooseUnsaved('cancel')">{{ t('uiAutomation.workbenchExecution.keepEditing') }}</el-button>
        <el-button :disabled="unsavedSaving || saveRequestPending" @click="chooseUnsaved('discard')">{{ t('uiAutomation.workbenchExecution.discardChanges') }}</el-button>
        <el-button type="primary" :loading="unsavedSaving" :disabled="saveRequestPending" @click="chooseUnsaved('save')">{{ t('uiAutomation.workbenchExecution.saveAndContinue') }}</el-button>
      </template>
    </el-dialog>

    <!-- 截图预览对话框 -->
    <el-dialog
      v-model="showScreenshotPreview"
      :title="t('uiAutomation.testCase.screenshotPreview')"
      width="80%"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :modal="true"
      :destroy-on-close="false"
    >
      <div v-if="currentScreenshot" class="screenshot-preview">
        <div class="preview-info">
          <h4>{{ currentScreenshot.description }}</h4>
          <p v-if="currentScreenshot.step_number">{{ t('uiAutomation.testCase.failedStep') }}: {{ t('uiAutomation.testCase.step') }} {{ currentScreenshot.step_number }}</p>
          <p v-if="currentScreenshot.timestamp">{{ t('uiAutomation.testCase.screenshotTime') }}: {{ formatTime(currentScreenshot.timestamp) }}</p>
        </div>
        <div class="preview-image">
          <img :src="currentScreenshot.url" :alt="currentScreenshot.description" />
        </div>
      </div>
    </el-dialog>

    <!-- 变量助手对话框 -->
    <el-dialog
      :close-on-press-escape="false"
      :modal="true"
      :destroy-on-close="false"
      v-model="showVariableHelper"
      :title="t('uiAutomation.testCase.variableHelper')"
      :close-on-click-modal="false"
      width="900px"
    >
      <el-tabs tab-position="left" style="height: 450px">
        <el-tab-pane
          v-for="(category, index) in variableCategoriesComputed"
          :key="index"
          :label="category.label"
        >
          <div style="height: 450px; overflow-y: auto; padding: 10px;">
            <el-table :data="category.variables" style="width: 100%" @row-click="insertVariable" highlight-current-row>
              <el-table-column prop="name" :label="t('uiAutomation.testCase.functionName')" width="150" show-overflow-tooltip>
                <template #default="{ row }">
                  <el-tag size="small">{{ row.name }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="desc" :label="t('uiAutomation.testCase.description')" min-width="150" />
              <el-table-column prop="syntax" :label="t('uiAutomation.testCase.syntax')" min-width="200" show-overflow-tooltip />
              <el-table-column prop="example" :label="t('uiAutomation.testCase.example')" min-width="200" show-overflow-tooltip />
              <el-table-column :label="t('uiAutomation.testCase.operation')" width="80" fixed="right">
                <template #default="{ row }">
                  <el-button link type="primary" size="small">{{ t('uiAutomation.testCase.insert') }}</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>
    
    <DataFactorySelector
      v-model="showDataFactorySelector"
      @select="handleDataFactorySelect"
    />
  </div>
</template>

<script setup>
import { ref, reactive, computed, nextTick, onMounted, onScopeDispose, watch } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Search, Plus, Edit, Delete, Check, CaretRight, ArrowUp, ArrowDown, Rank, Picture, Warning, View, ZoomIn, WarningFilled, MagicStick
} from '@element-plus/icons-vue'
import draggable from 'vuedraggable'
import DataFactorySelector from '@/components/DataFactorySelector.vue'
import { useI18n } from 'vue-i18n'
import { useUiProjectContext } from '@/composables/useUiProjectContext'
import { loadUiElementOptions, normalizeUiId, uiExecutionLocation } from '@/utils/ui-project-context.mjs'
import { readCaseExecutionLog } from '@/utils/ui-case-execution.mjs'
import { createUiCaseRunController } from '@/utils/ui-case-run-controller.mjs'
import { uiBrowserOptions, uiEngineOptions, uiReadinessError } from '@/utils/ui-execution-options.mjs'
import {
  getElements,
  createTestCase,
  updateTestCase,
  deleteTestCase as deleteTestCaseApi,
  getTestCases,
  runTestCase as runTestCaseApi,
  getTestCaseExecutionDetail,
  getTestCaseSubmission,
  abortTestCaseExecution,
  getExecutionReadiness,
  copyTestCase as copyTestCaseApi,
  getLocatorStrategies
} from '@/api/ui_automation'
import { getVariableFunctions } from '@/api/data-factory'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const { projects, projectId, userId, projectsLoading, projectsError, invalidProject, reloadProjects } = useUiProjectContext()
const casesLoading = ref(false)
const casesError = ref(false)
const savingForm = ref(false)
const savingSteps = ref(false)
const saveRequestPending = computed(() => savingForm.value || savingSteps.value)
let projectGeneration = 0
let caseListRequest = 0
let caseMutationVersion = 0
let caseMutationLog = []
const testCases = ref([])
const selectedTestCase = ref(null)
const currentSteps = ref([])
const availableElements = ref([])
const elementsError = ref(false)
const readiness = ref(null)
const readinessLoading = ref(false)
const readinessError = ref(false)
const unsavedVisible = ref(false)
const unsavedSaving = ref(false)
const formInitialKey = ref('')
let unsavedResolver = null
let unsavedTarget = 'all'
let readinessRequest = 0
let elementRequest = 0
const caseResults = new Map()
const searchKeyword = ref('')
const showCreateDialog = ref(false)
const editingTestCase = ref(null)
const executionResult = ref(null)
const resultActiveTab = ref('logs')
const allStepsExpanded = ref(false)
const showSteps = ref(true)
const showScreenshotPreview = ref(false)
const currentScreenshot = ref(null)
const runController = createUiCaseRunController({
  reactive,
  actorId: () => userId?.value,
  projectId: () => projectId.value,
  storage: () => globalThis.sessionStorage,
  hidden: () => Boolean(globalThis.document?.hidden),
  schedule: (callback, delay) => globalThis.setTimeout(callback, delay),
  clearTimer: timer => globalThis.clearTimeout?.(timer),
  submit: runTestCaseApi, lookup: getTestCaseSubmission, detail: getTestCaseExecutionDetail, abort: abortTestCaseExecution,
  onResult(project, caseId, result) {
    caseResults.set(runController.key(project, caseId), result)
    if (projectId.value === project && selectedTestCase.value?.id === caseId) setExecutionResult(result)
  },
})
const runningRuns = runController.runs
const selectedEngine = ref('playwright')
const selectedBrowser = ref('chrome')
const headlessMode = ref(true)
const showVariableHelper = ref(false)
const currentEditingStep = ref(null)
const currentEditingField = ref('')
const showDataFactorySelector = ref(false)
const currentStepForDataFactory = ref(null)
const currentFieldForDataFactory = ref('')
const variableCategories = ref([])
const loading = ref(false)



// 表单数据
const testCaseForm = reactive({
  name: '',
  description: '',
  priority: 'medium'
})

// 计算属性
const filteredTestCases = computed(() => {
  if (!searchKeyword.value) return testCases.value
  return testCases.value.filter(tc =>
    tc.name.includes(searchKeyword.value) ||
    tc.description?.includes(searchKeyword.value)
  )
})

const savedStepKeys = ref('[]')
const stepFields = ['action_type', 'element_id', 'input_value', 'wait_time', 'assert_type', 'assert_value', 'description']
const serializeSteps = steps => JSON.stringify(steps.map(step => stepFields.map(key => step[key])))
const hasUnsavedSteps = computed(() => serializeSteps(currentSteps.value) !== savedStepKeys.value)
const formKey = () => JSON.stringify([testCaseForm.name, testCaseForm.description, testCaseForm.priority])
const hasUnsavedForm = computed(() => showCreateDialog.value && formKey() !== formInitialKey.value)
const hasUnsavedChanges = computed(() => hasUnsavedForm.value || Boolean(selectedTestCase.value && hasUnsavedSteps.value))
const runKey = (project, caseId) => runController.key(project, caseId)
const currentRun = computed(() => selectedTestCase.value ? runController.currentRun(selectedTestCase.value.id) : null)
const isRunning = computed(() => Boolean(currentRun.value))
const engineOptions = computed(() => uiEngineOptions(readiness.value))
const browserOptions = computed(() => uiBrowserOptions(readiness.value, selectedEngine.value))
const readinessReason = computed(() => uiReadinessError(readiness.value, selectedEngine.value, selectedBrowser.value))
const canRunSelectedCase = computed(() => Boolean(selectedTestCase.value?.steps?.length && !hasUnsavedSteps.value && !savingSteps.value && !isRunning.value && !readinessLoading.value && !readinessError.value && !readinessReason.value))
const runPhaseLabel = computed(() => t(`uiAutomation.workbenchExecution.${currentRun.value?.cancelling ? 'cancelling' : ({ unknown: 'submissionUnknownTitle', paused: 'trackingPaused', recovering: 'recoveringRun', submitting: 'submittingRun' }[currentRun.value?.phase] || 'trackingRun')}`))
const sourceExecutionId = ref(normalizeUiId(route.query.fromExecutionId))
const sourceCaseId = ref(sourceExecutionId.value ? normalizeUiId(route.query.caseId) : '')
const sourceMatchesSelectedCase = computed(() => Boolean(
  sourceExecutionId.value &&
  normalizeUiId(route.query.fromExecutionId) === sourceExecutionId.value &&
  sourceCaseId.value &&
  sourceCaseId.value === normalizeUiId(selectedTestCase.value?.id)
))
const repairStep = computed(() => sourceMatchesSelectedCase.value ? normalizeUiId(route.query.stepNumber) : '')
const sourceExecutionLocation = computed(() => {
  const id = sourceMatchesSelectedCase.value ? sourceExecutionId.value : ''
  return id && ['case', 'suite'].includes(route.query.fromKind) ? uiExecutionLocation(route.query.fromKind === 'suite' ? 'UiReports' : 'UiExecutions', projectId.value, id) : null
})
const executionLog = computed(() => readCaseExecutionLog(executionResult.value?.logs))
const parsedExecutionLogs = computed(() => executionLog.value.steps)
const plainExecutionLog = computed(() => executionLog.value.text)

const isProjectCurrent = (project, generation) => projectId.value === project && projectGeneration === generation

const recordCaseMutation = (project, generation, testCase, action = 'update') => {
  if (!casesLoading.value || !isProjectCurrent(project, generation)) return
  caseMutationLog.push({ version: ++caseMutationVersion, project, generation, testCase, action })
}

const reconcileCaseMutations = (items, project, generation, requestMutationVersion) => {
  const mutations = caseMutationLog.filter(mutation =>
    mutation.project === project && mutation.generation === generation && mutation.version > requestMutationVersion
  )
  for (const mutation of mutations) {
    const index = items.findIndex(item => normalizeUiId(item.id) === normalizeUiId(mutation.testCase.id))
    if (mutation.action === 'delete') {
      if (index >= 0) items.splice(index, 1)
    } else if (index >= 0) {
      items[index] = { ...items[index], ...mutation.testCase }
    } else if (mutation.action === 'create') {
      items.unshift(mutation.testCase)
    }
  }
  caseMutationLog = caseMutationLog.filter(mutation => mutation.project !== project || mutation.generation !== generation)
  return items
}

const loadTestCases = async () => {
  const project = projectId.value
  if (!project) return
  const generation = projectGeneration
  const request = ++caseListRequest
  const requestMutationVersion = caseMutationVersion
  const isCurrent = () => isProjectCurrent(project, generation) && request === caseListRequest
  const maxAttempts = 3
  casesLoading.value = true
  casesError.value = false
  try {
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      const attemptMutationVersion = caseMutationVersion
      const pageItems = []
      const seenIds = new Set()
      let expectedCount
      let countChanged = false
      let duplicateIds = false
      let invalidPage = false
      let pageCount = 0

      for (let page = 1; ; page += 1) {
        let response
        try {
          response = await getTestCases({ project, page })
        } catch (error) {
          if (!isCurrent()) return
          if (page === 1 || error.response?.status !== 404) throw error
          // DRF returns an invalid-page 404 when deletions remove the requested page.
          invalidPage = true
          break
        }
        if (!isCurrent()) return
        const { data } = response
        const rows = Array.isArray(data) ? data : data?.results
        const hasNext = Boolean(data?.next)
        if (!Array.isArray(rows) || (hasNext && !rows.length)) throw new Error('Invalid UI case list')
        if (Number.isInteger(data?.count) && data.count >= 0) {
          if (expectedCount === undefined) expectedCount = data.count
          else if (expectedCount !== data.count) countChanged = true
        }
        for (const row of rows) {
          const id = normalizeUiId(row.id)
          if (id && seenIds.has(id)) duplicateIds = true
          if (id) seenIds.add(id)
        }
        pageItems.push(...rows)
        pageCount += 1
        if (!hasNext) break
      }

      const structuralMutationObserved = caseMutationLog.some(mutation =>
        mutation.project === project && mutation.generation === generation &&
        mutation.version > attemptMutationVersion && ['create', 'delete'].includes(mutation.action)
      )
      const inconsistentWalk = pageCount > 1 && (
        countChanged || duplicateIds || (expectedCount !== undefined && pageItems.length !== expectedCount)
      )
      if (invalidPage || (pageCount > 1 && (structuralMutationObserved || inconsistentWalk))) continue

      testCases.value = reconcileCaseMutations(pageItems, project, generation, requestMutationVersion)
      return
    }
    throw new Error('UI case list changed during pagination')
  } catch {
    if (isCurrent()) casesError.value = true
  } finally {
    if (isCurrent()) {
      casesLoading.value = false
      caseMutationLog = []
    }
  }
}

const loadElements = async (project, generation) => {
  const request = ++elementRequest
  elementsError.value = false
  try {
    const rows = await loadUiElementOptions(getElements, project)
    if (request === elementRequest && isProjectCurrent(project, generation)) availableElements.value = rows
  } catch {
    if (request === elementRequest && isProjectCurrent(project, generation)) elementsError.value = true
  }
}

async function loadReadiness() {
  const request = ++readinessRequest
  const project = projectId.value
  readiness.value = null
  readinessError.value = false
  readinessLoading.value = Boolean(project)
  if (!project) return
  try {
    const { data } = await getExecutionReadiness(project)
    if (request !== readinessRequest || projectId.value !== project) return
    readiness.value = data
    const engine = uiEngineOptions(data).find(item => item.value === selectedEngine.value && !item.disabled) || uiEngineOptions(data).find(item => !item.disabled)
    if (engine) selectedEngine.value = engine.value
    const browsers = uiBrowserOptions(data, selectedEngine.value)
    if (!browsers.some(item => item.value === selectedBrowser.value && !item.disabled)) selectedBrowser.value = browsers.find(item => !item.disabled)?.value || selectedBrowser.value
  } catch {
    if (request === readinessRequest && projectId.value === project) readinessError.value = true
  } finally { if (request === readinessRequest) readinessLoading.value = false }
}

const onProjectChange = project => {
  runController.resetScope()
  caseResults.clear()
  settleUnsaved(false)
  const generation = ++projectGeneration
  caseMutationVersion = 0
  caseMutationLog = []
  selectedTestCase.value = null
  testCases.value = []
  availableElements.value = []
  currentSteps.value = []
  searchKeyword.value = ''
  executionResult.value = null
  showCreateDialog.value = false
  showScreenshotPreview.value = false
  showSteps.value = true
  casesLoading.value = false
  casesError.value = false
  void loadReadiness()
  if (!project) return
  void loadTestCases()
  void loadElements(project, generation)
}

function settleUnsaved(allow) {
  const resolve = unsavedResolver
  unsavedResolver = null
  unsavedVisible.value = false
  resolve?.(allow)
}

function confirmUnsaved(target = 'all') {
  if (saveRequestPending.value) {
    ElMessage.warning(t('uiAutomation.workbenchExecution.savePendingNavigation'))
    return false
  }
  if (!(target === 'form' ? hasUnsavedForm.value : hasUnsavedChanges.value)) return true
  if (unsavedResolver) return false
  unsavedTarget = target
  unsavedVisible.value = true
  return new Promise(resolve => { unsavedResolver = resolve })
}

async function requestCloseCaseForm(done) {
  if (unsavedSaving.value) return false
  if (!await confirmUnsaved('form')) return false
  showCreateDialog.value = false
  if (typeof done === 'function') done()
  return true
}

async function chooseUnsaved(choice) {
  if (unsavedSaving.value || !unsavedResolver) return
  if (choice === 'cancel') { settleUnsaved(false); return }
  if (saveRequestPending.value) {
    ElMessage.warning(t('uiAutomation.workbenchExecution.savePendingNavigation'))
    return
  }
  if (choice === 'discard') {
    if (unsavedTarget === 'all' && selectedTestCase.value) {
      currentSteps.value = (selectedTestCase.value.steps || []).map(step => ({ ...step, element_id: step.element || step.element_id || '', expanded: false }))
      savedStepKeys.value = serializeSteps(currentSteps.value)
    }
    showCreateDialog.value = false
    settleUnsaved(true)
    return
  }
  unsavedSaving.value = true
  try {
    if (hasUnsavedForm.value && !await saveTestCaseForm({ preserveSelection: true })) return
    if (unsavedTarget === 'all' && selectedTestCase.value && hasUnsavedSteps.value && !await saveTestCase()) return
    if (!(unsavedTarget === 'form' ? hasUnsavedForm.value : hasUnsavedChanges.value)) settleUnsaved(true)
  } finally { unsavedSaving.value = false }
}

const selectTestCase = (testCase, explicit = true) => {
  if (selectedTestCase.value?.id === testCase.id) return true
  if (explicit && (hasUnsavedChanges.value || saveRequestPending.value)) {
    const project = projectId.value
    const generation = projectGeneration
    return Promise.resolve(confirmUnsaved()).then(allow => allow && isProjectCurrent(project, generation) ? selectTestCase(testCase, explicit) : false)
  }
  if (explicit) {
    const project = normalizeUiId(projectId.value)
    const caseId = normalizeUiId(route.query.caseId)
    const key = `${project}:${caseId}`
    if (route.name === 'UiTestCases' && project && caseId && consumedCaseSelection !== key) {
      cancelledCaseSelection = key
    }
  }

  // 如果点击的是同一个用例，不做任何处理
  if (selectedTestCase.value && selectedTestCase.value.id === testCase.id) {
    return
  }

  const targetCaseId = normalizeUiId(testCase.id)
  let repairContextChanged = false
  const nextRouteQuery = { ...route.query }
  if (normalizeUiId(route.query.fromExecutionId) && sourceCaseId.value !== targetCaseId) {
    sourceExecutionId.value = ''
    sourceCaseId.value = ''
    delete nextRouteQuery.fromExecutionId
    delete nextRouteQuery.fromKind
    delete nextRouteQuery.stepNumber
    repairContextChanged = true
  }
  selectedTestCase.value = testCase
  // 确保步骤数据格式正确，添加前端需要的字段
  if (testCase.steps && testCase.steps.length > 0) {
    currentSteps.value = testCase.steps.map(step => ({
      ...step,
      element_id: step.element || '',
      expanded: false
    }))
  } else {
    currentSteps.value = []
  }
  savedStepKeys.value = serializeSteps(currentSteps.value)
  executionResult.value = null
  showSteps.value = true
  allStepsExpanded.value = false
  const previous = caseResults.get(runKey(projectId.value, testCase.id))
  if (previous) setExecutionResult(previous)
  void runController.restore(testCase.id)
  focusRepairStep()
  if (route.name === 'UiTestCases' && (repairContextChanged || (explicit && normalizeUiId(route.query.caseId) !== targetCaseId))) {
    if (explicit) {
      nextRouteQuery.project = String(projectId.value)
      nextRouteQuery.caseId = String(targetCaseId)
    }
    void router.replace({ name: 'UiTestCases', query: nextRouteQuery, hash: route.hash })
  }
  return true
}

function focusRepairStep() {
  if (normalizeUiId(route.query.caseId) !== selectedTestCase.value?.id) return
  const step = currentSteps.value[repairStep.value - 1]
  if (!step) return
  step.expanded = true
  void nextTick(() => globalThis.document?.getElementById(`ui-case-step-${repairStep.value}`)?.scrollIntoView?.({ block: 'center' }))
}

const addStep = () => {
  const newStep = {
    id: Date.now(),
    action_type: 'click',
    element_id: '',
    input_value: '',
    wait_time: 1000,
    assert_type: 'textContains',
    assert_value: '',
    description: '',
    expanded: true
  }
  currentSteps.value.push(newStep)
}

const removeStep = (index) => {
  currentSteps.value.splice(index, 1)
}

const onStepsReorder = () => {
  // 步骤重新排序后的处理
  console.log('步骤已重新排序')
}

const onActionTypeChange = (step) => {
  // 根据操作类型重置相关参数
  if (step.action_type !== 'fill') {
    step.input_value = ''
  }
  if (step.action_type !== 'wait') {
    step.wait_time = 1000
  }
  if (step.action_type !== 'assert') {
    step.assert_type = 'textContains'
    step.assert_value = ''
  }
}

const onElementChange = (step) => {
  // 元素变化时的处理
  const element = availableElements.value.find(e => e.id === step.element_id)
  if (element && !step.description) {
    step.description = `${getActionTypeText(step.action_type)}${element.name}`
  }
}

const needsInputValue = (actionType) => {
  return ['fill', 'switchTab'].includes(actionType)
}

const needsWaitTime = (actionType) => {
  return ['wait', 'waitFor'].includes(actionType)
}

const needsElement = (actionType) => {
  return !['wait', 'switchTab', 'screenshot'].includes(actionType)
}

const expandAllSteps = () => {
  allStepsExpanded.value = !allStepsExpanded.value
  currentSteps.value.forEach(step => {
    step.expanded = allStepsExpanded.value
  })
}

const saveTestCase = async () => {
  if (!selectedTestCase.value || !projectId.value || savingSteps.value || isRunning.value) return false
  const testCase = selectedTestCase.value
  const project = projectId.value
  const generation = projectGeneration
  const steps = currentSteps.value.map(step => ({ ...step }))
  const savedKeys = serializeSteps(steps)
  savingSteps.value = true
  try {
    const { data } = await updateTestCase(testCase.id, { steps })
    if (!isProjectCurrent(project, generation)) return false
    const updated = { ...testCase, ...data, steps: data?.steps || steps }
    testCases.value = testCases.value.map(item => item.id === testCase.id ? updated : item)
    recordCaseMutation(project, generation, updated)
    if (selectedTestCase.value?.id === testCase.id) {
      selectedTestCase.value = updated
      savedStepKeys.value = savedKeys
    }
    ElMessage.success(t('uiAutomation.testCase.save.success'))
    return true
  } catch {
    if (isProjectCurrent(project, generation)) ElMessage.error(t('uiAutomation.testCase.save.failed'))
    return false
  } finally {
    savingSteps.value = false
  }
}

const runTestCase = async testCase => {
  if (!projectId.value || testCase?.id !== selectedTestCase.value?.id || !canRunSelectedCase.value) return
  executionResult.value = null
  caseResults.delete(runKey(projectId.value, testCase.id))
  showSteps.value = false
  const origin = sourceMatchesSelectedCase.value && sourceCaseId.value === normalizeUiId(testCase.id)
    ? sourceExecutionId.value
    : ''
  await runController.start(testCase.id, {
    engine: selectedEngine.value, browser: selectedBrowser.value, headless: headlessMode.value,
    ...(origin && ['case', 'suite'].includes(route.query.fromKind) ? { rerun_of: origin, rerun_kind: route.query.fromKind } : {}),
  })
}
const resumeRun = () => selectedTestCase.value && runController.resume(selectedTestCase.value.id)
const retrySameRun = () => selectedTestCase.value && runController.retry(selectedTestCase.value.id)
const cancelRun = () => selectedTestCase.value && runController.cancel(selectedTestCase.value.id)

function setExecutionResult(data) {
  const executionId = normalizeUiId(data.execution_id)
  const preflight = !data.success && (data.failure_stage === 'preflight' || data.errors?.some(item => item.step_number === 0))
  executionResult.value = { ...data, executionId, preflight }
  resultActiveTab.value = preflight && data.errors?.length ? 'errors' : 'logs'
  showSteps.value = false
}

const toggleView = () => {
  showSteps.value = !showSteps.value
}

const editTestCase = (testCase) => {
  editingTestCase.value = testCase
  testCaseForm.name = testCase.name
  testCaseForm.description = testCase.description || ''
  testCaseForm.priority = testCase.priority || 'medium'
  formInitialKey.value = formKey()
  showCreateDialog.value = true
}

const deleteTestCase = async (testCase) => {
  const project = projectId.value
  const generation = projectGeneration
  try {
    await ElMessageBox.confirm(
      t('uiAutomation.testCase.delete.confirm', { name: testCase.name }),
      t('uiAutomation.testCase.delete.title'),
      {
        confirmButtonText: t('uiAutomation.common.confirm'),
        cancelButtonText: t('uiAutomation.common.cancel'),
        type: 'warning'
      }
    )

    if (!isProjectCurrent(project, generation)) return
    await deleteTestCaseApi(testCase.id)
    if (!isProjectCurrent(project, generation)) return
    recordCaseMutation(project, generation, { id: testCase.id }, 'delete')
    ElMessage.success(t('uiAutomation.testCase.delete.success'))

    // 从列表中移除
    const index = testCases.value.findIndex(tc => tc.id === testCase.id)
    if (index !== -1) {
      testCases.value.splice(index, 1)
    }

    // 如果删除的是当前选中的用例，清空选择
    if (selectedTestCase.value?.id === testCase.id) {
      selectedTestCase.value = null
      currentSteps.value = []
      executionResult.value = null
    }
  } catch (error) {
    if (error !== 'cancel' && isProjectCurrent(project, generation)) {
      console.error('删除测试用例失败:', error)
      ElMessage.error('删除失败')
    }
  }
}

const copyTestCase = async (testCase) => {
  const project = projectId.value
  const generation = projectGeneration
  try {
    await ElMessageBox.confirm(
      t('uiAutomation.testCase.copy.confirm', { name: testCase.name }),
      t('uiAutomation.testCase.copy.title'),
      {
        confirmButtonText: t('uiAutomation.common.confirm'),
        cancelButtonText: t('uiAutomation.common.cancel'),
        type: 'info'
      }
    )

    if (!isProjectCurrent(project, generation)) return
    const response = await copyTestCaseApi(testCase.id)
    if (!isProjectCurrent(project, generation)) return
    recordCaseMutation(project, generation, response.data, 'create')
    ElMessage.success(t('uiAutomation.testCase.copy.success'))

    // 找到原用例的位置
    const index = testCases.value.findIndex(tc => tc.id === testCase.id)
    if (index !== -1) {
      // 在原用例下方插入新用例
      testCases.value.splice(index + 1, 0, response.data)
    } else {
      // 如果找不到，就添加到末尾
      testCases.value.push(response.data)
    }
  } catch (error) {
    if (error !== 'cancel' && isProjectCurrent(project, generation)) {
      console.error('复制测试用例失败:', error)
      ElMessage.error('复制失败')
    }
  }
}

// 加载变量函数
const loadVariableFunctions = async () => {
  try {
    loading.value = true
    console.log('开始加载变量函数...')
    const apiResponse = await getVariableFunctions()
    console.log('变量函数响应:', apiResponse)
    console.log('变量函数响应.data:', apiResponse.data)
    
    // 检查不同可能的数据结构
    let functionsData = []
    if (apiResponse && apiResponse.data) {
      if (Array.isArray(apiResponse.data)) {
        // 后端返回的是数组，直接使用
        functionsData = apiResponse.data
      } else if (apiResponse.data.functions) {
        // 如果data中有functions字段，使用它
        functionsData = apiResponse.data.functions
      } else if (typeof apiResponse.data === 'object') {
        // 如果data是对象但没有functions字段，假设整个对象就是按分类组织的函数
        functionsData = apiResponse.data
      }
    }
    
    console.log('处理后的函数数据:', functionsData)
    
    // 处理函数数据，按分类组织
    const grouped = {}
    
    if (Array.isArray(functionsData)) {
      // 如果是数组格式
      functionsData.forEach(func => {
        const category = func.category || '未分类'
        if (!grouped[category]) {
          grouped[category] = []
        }
        grouped[category].push({
          name: func.name,
          syntax: func.syntax,
          desc: func.description || func.desc || '',
          example: func.example
        })
      })
    } else if (typeof functionsData === 'object') {
      // 如果是按分类组织的对象格式
      for (const [category, funcs] of Object.entries(functionsData)) {
        if (Array.isArray(funcs)) {
          grouped[category] = funcs.map(func => ({
            name: func.name,
            syntax: func.syntax,
            desc: func.description || func.desc || '',
            example: func.example
          }))
        }
      }
    }
    
    console.log('按分类组织后的函数:', grouped)
    
    // 定义固定的分类顺序
    const categoryOrder = ['随机数', '测试数据', '字符串', '编码转换', '加密', '时间日期', 'Crontab', '未分类']
    
    // 按固定顺序构建分类列表
    const orderedCategories = []
    categoryOrder.forEach(category => {
      if (grouped[category]) {
        orderedCategories.push({
          label: category,
          variables: grouped[category]
        })
        delete grouped[category]
      }
    })
    
    // 添加剩余的分类
    for (const [category, funcs] of Object.entries(grouped)) {
      orderedCategories.push({
        label: category,
        variables: funcs
      })
    }
    
    console.log('最终的分类列表:', orderedCategories)
    variableCategories.value = orderedCategories
  } catch (error) {
    console.error('加载变量函数失败:', error)
    ElMessage.error('加载变量函数失败，使用本地数据')
    useLocalVariableCategories()
  } finally {
    loading.value = false
  }
}

// 使用本地变量分类数据作为 fallback
const useLocalVariableCategories = () => {
  variableCategories.value = [
    {
      label: t('uiAutomation.testCase.variableCategory.randomNumber'),
      variables: [
        { name: 'random_int', syntax: '${random_int(min, max, count)}', desc: t('uiAutomation.testCase.variable.randomInt.desc'), example: '${random_int(100, 999, 1)}' },
        { name: 'random_float', syntax: '${random_float(min, max, precision, count)}', desc: t('uiAutomation.testCase.variable.randomFloat.desc'), example: '${random_float(0, 1, 2, 1)}' }
      ]
    },
    {
      label: t('uiAutomation.testCase.variableCategory.randomString'),
      variables: [
        { name: 'random_string', syntax: '${random_string(length, char_type, count)}', desc: t('uiAutomation.testCase.variable.randomString.desc'), example: '${random_string(8, "all", 1)}' }
      ]
    }
  ]
}

// 计算属性提供变量分类数据
const variableCategoriesComputed = computed(() => {
  return variableCategories.value.length > 0 ? variableCategories.value : [
    {
      label: t('uiAutomation.testCase.variableCategory.randomNumber'),
      variables: []
    }
  ]
})

const openVariableHelper = (step, field) => {
  console.log('TestCaseManager openVariableHelper 被调用, step:', step, 'field:', field)
  console.log('variableCategories.value:', variableCategories.value)
  console.log('variableCategories.value.length:', variableCategories.value.length)
  currentEditingStep.value = step
  currentEditingField.value = field
  showVariableHelper.value = true
  console.log('showVariableHelper.value:', showVariableHelper.value)
}

const openDataFactorySelector = (step, field) => {
  currentStepForDataFactory.value = step
  currentFieldForDataFactory.value = field
  showDataFactorySelector.value = true
}

const handleDataFactorySelect = (record) => {
  const step = currentStepForDataFactory.value
  const field = currentFieldForDataFactory.value
  
  if (record && record.output_data && step && field) {
    let valueToSet = ''
    
    if (typeof record.output_data === 'string') {
      valueToSet = record.output_data
    } else if (record.output_data.result) {
      valueToSet = record.output_data.result
    } else if (record.output_data.output_data) {
      valueToSet = record.output_data.output_data
    } else {
      valueToSet = JSON.stringify(record.output_data)
    }
    
    step[field] = valueToSet
    ElMessage.success(t('uiAutomation.testCase.messages.dataFactorySelected', { toolName: record.tool_name }))
  }
  
  showDataFactorySelector.value = false
}

const insertVariable = (variable) => {
  if (currentEditingStep.value && currentEditingField.value) {
    const example = variable.example
    const currentValue = currentEditingStep.value[currentEditingField.value] || ''
    
    // 简单起见，这里直接追加到末尾，或者如果为空则替换
    if (!currentValue) {
      currentEditingStep.value[currentEditingField.value] = example
    } else {
      currentEditingStep.value[currentEditingField.value] = currentValue + example
    }
    
    ElMessage.success(t('uiAutomation.testCase.messages.variableInserted', { name: variable.name }))
    showVariableHelper.value = false
  }
}

const saveTestCaseForm = async (options = {}) => {
  if (!projectId.value || savingForm.value) return false
  if (!testCaseForm.name.trim()) {
    ElMessage.warning(t('uiAutomation.testCase.form.nameRequired'))
    return false
  }

  const project = projectId.value
  const generation = projectGeneration
  const editing = editingTestCase.value
  const data = { ...testCaseForm, name: testCaseForm.name.trim(), project }
  const submittedFormKey = formKey()
  savingForm.value = true
  try {
    const response = editing ? await updateTestCase(editing.id, data) : await createTestCase(data)
    if (!isProjectCurrent(project, generation)) return false
    const formUnchanged = formKey() === submittedFormKey
    // The API has settled. Internal selection of a newly created case must
    // not be blocked by the pending-save guard for an already completed write.
    savingForm.value = false
    if (editing) {
      const existing = testCases.value.find(item => item.id === editing.id) || editing
      const updated = { ...existing, ...response.data }
      testCases.value = testCases.value.map(item => item.id === editing.id ? updated : item)
      recordCaseMutation(project, generation, updated)
      if (selectedTestCase.value?.id === editing.id) selectedTestCase.value = { ...selectedTestCase.value, ...response.data }
      ElMessage.success(t('uiAutomation.testCase.update.success'))
    } else {
      testCases.value = [response.data, ...testCases.value.filter(item => item.id !== response.data.id)]
      recordCaseMutation(project, generation, response.data, 'create')
      searchKeyword.value = ''
      if (!options.preserveSelection && formUnchanged) {
        showCreateDialog.value = false
        selectTestCase(response.data)
      }
      if (!formUnchanged) editingTestCase.value = response.data
      ElMessage.success(t('uiAutomation.testCase.create.success'))
    }
    formInitialKey.value = submittedFormKey
    if (formUnchanged) showCreateDialog.value = false
    return formUnchanged
  } catch {
    if (isProjectCurrent(project, generation)) ElMessage.error(t('uiAutomation.testCase.save.failed'))
    return false
  } finally {
    savingForm.value = false
  }
}

const resetForm = () => {
  testCaseForm.name = ''
  testCaseForm.description = ''
  testCaseForm.priority = 'medium'
}

const closeCaseForm = () => {
  if (!showCreateDialog.value) {
    editingTestCase.value = null
    resetForm()
  }
}

const openCreateCase = () => {
  if (!projectId.value || savingForm.value) return false
  if (hasUnsavedChanges.value || saveRequestPending.value) {
    const project = projectId.value
    const generation = projectGeneration
    return Promise.resolve(confirmUnsaved()).then(allow => allow && isProjectCurrent(project, generation) ? openCreateCase() : false)
  }
  editingTestCase.value = null
  resetForm()
  formInitialKey.value = formKey()
  showCreateDialog.value = true
  return true
}

// 辅助方法
const getStatusTag = (status) => {
  const tagMap = {
    'draft': 'info',
    'ready': 'success',
    'running': 'warning',
    'passed': 'success',
    'failed': 'danger'
  }
  return tagMap[status] || 'info'
}

const getStatusText = (status) => {
  const textMap = {
    'draft': t('uiAutomation.testCase.status.draft'),
    'ready': t('uiAutomation.testCase.status.ready'),
    'running': t('uiAutomation.testCase.status.running'),
    'passed': t('uiAutomation.testCase.status.passed'),
    'failed': t('uiAutomation.testCase.status.failed')
  }
  return textMap[status] || t('uiAutomation.testCase.status.unknown')
}

const getActionTypeText = (actionType) => {
  const textMap = {
    'click': t('uiAutomation.testCase.actionType.click'),
    'fill': t('uiAutomation.testCase.actionType.fill'),
    'getText': t('uiAutomation.testCase.actionType.getText'),
    'waitFor': t('uiAutomation.testCase.actionType.waitFor'),
    'hover': t('uiAutomation.testCase.actionType.hover'),
    'scroll': t('uiAutomation.testCase.actionType.scroll'),
    'screenshot': t('uiAutomation.testCase.actionType.screenshot'),
    'assert': t('uiAutomation.testCase.actionType.assert'),
    'wait': t('uiAutomation.testCase.actionType.wait')
  }
  return textMap[actionType] || actionType
}

const formatTime = (timestamp) => {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  return date.toLocaleString()
}

// 获取操作类型文本
const getActionText = (actionType) => {
  const actionMap = {
    'click': t('uiAutomation.testCase.actionText.click'),
    'fill': t('uiAutomation.testCase.actionText.fill'),
    'getText': t('uiAutomation.testCase.actionText.getText'),
    'waitFor': t('uiAutomation.testCase.actionText.waitFor'),
    'hover': t('uiAutomation.testCase.actionText.hover'),
    'scroll': t('uiAutomation.testCase.actionText.scroll'),
    'screenshot': t('uiAutomation.testCase.actionText.screenshot'),
    'assert': t('uiAutomation.testCase.actionText.assert'),
    'wait': t('uiAutomation.testCase.actionText.wait')
  }
  return actionMap[actionType] || actionType
}

// 图片处理方法
const handleImageError = (event) => {
  const img = event.target
  const screenshotIndex = parseInt(img.dataset.index)
  if (executionResult.value && executionResult.value.screenshots) {
    executionResult.value.screenshots[screenshotIndex].error = true
    executionResult.value.screenshots[screenshotIndex].loaded = true
  }
}

const handleImageLoad = (event) => {
  const img = event.target
  const screenshotIndex = parseInt(img.dataset.index)
  if (executionResult.value && executionResult.value.screenshots) {
    executionResult.value.screenshots[screenshotIndex].loaded = true
    executionResult.value.screenshots[screenshotIndex].error = false
  }
}

const previewScreenshot = (screenshot) => {
  currentScreenshot.value = screenshot
  showScreenshotPreview.value = true
}

watch([projectId, () => userId?.value], ([project]) => onProjectChange(project), { immediate: true })
let consumedCaseSelection = ''
let cancelledCaseSelection = ''
let lastCaseSelection = normalizeUiId(route.query.caseId)
let lastCaseRoute = route.name
let lastCaseProject = normalizeUiId(projectId.value)
const restoreCaseSelection = () => {
  const caseId = normalizeUiId(route.query.caseId)
  const project = normalizeUiId(projectId.value)
  if (route.name !== lastCaseRoute || caseId !== lastCaseSelection || project !== lastCaseProject) {
    consumedCaseSelection = ''
    cancelledCaseSelection = ''
    lastCaseRoute = route.name
    lastCaseSelection = caseId
    lastCaseProject = project
  }
  if (route.name !== 'UiTestCases') return
  if (!project || !caseId) return

  const key = `${project}:${caseId}`
  if (consumedCaseSelection === key || cancelledCaseSelection === key) return
  const match = testCases.value.find(item => normalizeUiId(item.id) === caseId)
  if (!match) return

  consumedCaseSelection = key
  selectTestCase(match, false)
}
watch([projectId, testCases, () => route.name, () => route.query.caseId], restoreCaseSelection)
watch([projectId, () => route.query.setup], ([project, setup]) => {
  if (route.name !== 'UiTestCases' || !project || setup !== 'case') return
  openCreateCase()
  const query = { ...route.query, project: String(project) }
  delete query.setup
  void router.replace({ name: 'UiTestCases', query, hash: route.hash })
}, { immediate: true })

watch(() => [route.query.fromExecutionId, route.query.caseId], ([executionValue, caseValue]) => {
  const nextExecutionId = normalizeUiId(executionValue)
  if (!nextExecutionId) {
    sourceExecutionId.value = ''
    sourceCaseId.value = ''
  } else if (nextExecutionId !== sourceExecutionId.value) {
    sourceExecutionId.value = nextExecutionId
    sourceCaseId.value = normalizeUiId(caseValue)
  }
}, { flush: 'sync' })
watch(() => route.query.stepNumber, focusRepairStep)
watch(selectedEngine, () => {
  const available = browserOptions.value.filter(item => !item.disabled)
  if (available.length && !available.some(item => item.value === selectedBrowser.value)) selectedBrowser.value = available[0].value
})
onBeforeRouteLeave(() => confirmUnsaved())
onBeforeRouteUpdate((to, from) => {
  if (to.name !== from.name || normalizeUiId(to.query?.project) !== normalizeUiId(from.query?.project) || normalizeUiId(to.query?.caseId) !== normalizeUiId(from.query?.caseId)) return confirmUnsaved()
  return true
})
const beforeUnload = event => {
  if (!hasUnsavedChanges.value && !saveRequestPending.value) return
  event.preventDefault()
  event.returnValue = ''
}
const onVisibility = () => runController.visible()
onMounted(() => {
  globalThis.addEventListener?.('beforeunload', beforeUnload)
  globalThis.document?.addEventListener('visibilitychange', onVisibility)
})
onMounted(loadVariableFunctions)
onScopeDispose(() => {
  projectGeneration += 1
  readinessRequest += 1
  elementRequest += 1
  settleUnsaved(false)
  runController.dispose()
  globalThis.removeEventListener?.('beforeunload', beforeUnload)
  globalThis.document?.removeEventListener('visibilitychange', onVisibility)
})
</script>

<style scoped>
.test-case-manager {
  height: 100vh;
  display: flex;
  flex-direction: column;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px;
  border-bottom: 1px solid #e6e6e6;
  background: white;
}

.page-title {
  margin: 0;
  font-size: 24px;
}

.header-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.main-content {
  flex: 1;
  display: flex;
  overflow: hidden;
}

.left-panel {
  width: 350px;
  border-right: 1px solid #e6e6e6;
  background: white;
  display: flex;
  flex-direction: column;
}

.panel-header {
  padding: 15px;
  border-bottom: 1px solid #e6e6e6;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.panel-header h3 {
  margin: 0;
}

.test-case-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
}

.test-case-item {
  border: 1px solid #e6e6e6;
  border-radius: 6px;
  margin-bottom: 10px;
  padding: 15px;
  cursor: pointer;
  transition: all 0.3s;
}

.test-case-item:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.1);
}

.test-case-item.active {
  border-color: #409eff;
  background-color: #f0f8ff;
}

.case-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 10px;
}

.case-info {
  flex: 1;
}

.case-name {
  margin: 0 0 5px 0;
  font-size: 16px;
  font-weight: 600;
}

.case-name button {
  padding: 0;
  color: var(--el-color-primary);
  text-align: start;
  font: inherit;
  background: none;
  border: 0;
  cursor: pointer;
  overflow-wrap: anywhere;
}

.case-description {
  margin: 0;
  color: #666;
  font-size: 14px;
  line-height: 1.4;
}

.case-actions {
  display: flex;
  gap: 5px;
}

.case-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: #888;
}

.step-count {
  color: #409eff;
  font-weight: 500;
}

.right-panel {
  flex: 1;
  background: white;
  display: flex;
  flex-direction: column;
}

.test-case-detail {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 20px;
  overflow: hidden;
  height: 100%;
}

.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 15px;
  border-bottom: 1px solid #e6e6e6;
}

.detail-header h3 {
  margin: 0;
}

.detail-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.detail-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.case-page-state {
  padding: 16px;
}

.case-guide {
  padding: 10px 12px;
  margin: 0 0 12px;
  border-radius: 4px;
  color: var(--el-text-color-regular);
  background: var(--el-color-primary-light-9);
}

.repair-step { outline: 2px solid var(--el-color-primary); outline-offset: -2px; }
.run-tracking p { margin: 8px 0; overflow-wrap: anywhere; }

.plain-case-log {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 850px) {
  .test-case-manager {
    height: auto;
    min-height: 0;
  }

  .page-header,
  .main-content,
  .detail-header {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
  }

  .left-panel {
    width: auto;
    max-height: 320px;
  }
}

.steps-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  margin-bottom: 20px;
  border: 1px solid #e6e6e6;
  border-radius: 6px;
  background: #fafafa;
  overflow: hidden;
}

.steps-container.has-steps {
  max-height: 50%;
}

.steps-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 15px;
}

.steps-header h4 {
  margin: 0;
}

.steps-list {
  padding: 10px;
  padding-bottom: 20px;
}

.steps-scroll-container {
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  padding: 10px;
  padding-right: 5px;
}

.steps-scroll-container::-webkit-scrollbar {
  width: 6px;
}

.steps-scroll-container::-webkit-scrollbar-track {
  background: #f5f5f5;
  border-radius: 3px;
}

.steps-scroll-container::-webkit-scrollbar-thumb {
  background: #ccc;
  border-radius: 3px;
}

.steps-scroll-container::-webkit-scrollbar-thumb:hover {
  background: #999;
}

.step-item {
  border: 1px solid #e6e6e6;
  border-radius: 6px;
  margin-bottom: 10px;
  background: white;
  transition: all 0.3s;
}

.step-item:hover {
  border-color: #409eff;
}

.step-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 15px;
  background: #fafafa;
  border-radius: 6px 6px 0 0;
}

.step-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.drag-handle {
  cursor: move;
  color: #999;
}

.step-number {
  background: #409eff;
  color: white;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: bold;
}

.step-right {
  display: flex;
  gap: 5px;
}

.step-content {
  padding: 15px;
  border-top: 1px solid #e6e6e6;
}

.step-param {
  display: flex;
  align-items: center;
  margin-bottom: 10px;
  gap: 10px;
}

.step-param label {
  width: 120px;
  font-weight: 500;
  color: #333;
}

.execution-result {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid #e6e6e6;
  border-radius: 6px;
  background: white;
  overflow: hidden;
}

.execution-result.with-steps {
  margin-top: 0;
}

.execution-result .result-header {
  padding: 15px;
  border-bottom: 1px solid #e6e6e6;
  background: #fafafa;
  border-radius: 6px 6px 0 0;
}

.execution-result .result-content {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 15px;
}

.result-content {
  flex: 1;
  overflow: hidden;
}

/* 为el-tabs和el-tab-pane添加flex布局支持 */
.result-content :deep(.el-tabs) {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.result-content :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.result-content :deep(.el-tab-pane) {
  height: 100%;
  overflow: auto;
}

/* .result-header 已在 .execution-result 中定义 */

.result-header h4 {
  margin: 0;
}

.logs-container {
  max-height: 500px;
  overflow-y: auto;
  background: #f5f7fa;
  padding: 15px;
  border-radius: 4px;
}

.log-item {
  margin-bottom: 15px;
  padding: 12px;
  background: white;
  border-radius: 4px;
  border-left: 3px solid #409eff;
}

.log-item:last-child {
  margin-bottom: 0;
}

.log-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.log-action {
  font-weight: 500;
  color: #606266;
}

.log-desc {
  color: #909399;
  font-size: 14px;
}

.log-error {
  display: flex;
  align-items: flex-start;  /* 改为 flex-start，适配多行文本 */
  gap: 8px;
  color: #f56c6c;
  background: #fef0f0;
  padding: 8px 12px;
  border-radius: 4px;
  margin-top: 8px;
  font-size: 14px;

  .error-message {
    margin: 0;
    padding: 0;
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;  /* 保留换行符和空格 */
    word-break: break-word;  /* 长单词换行 */
    flex: 1;
  }

  .el-icon {
    margin-top: 2px;  /* 图标与文本顶部对齐 */
    flex-shrink: 0;  /* 图标不缩小 */
  }
}

.screenshots-container {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 20px;
  padding: 10px;
}

.screenshot-item {
  display: flex;
  flex-direction: column;
  cursor: pointer;
  transition: transform 0.2s ease;
}

.screenshot-item:hover {
  transform: translateY(-4px);
}

.screenshot-wrapper {
  position: relative;
  width: 100%;
  min-height: 200px;
  background: #f5f5f5;
  border-radius: 8px;
  border: 2px solid #e6e6e6;
  overflow: hidden;
  transition: border-color 0.3s ease;
}

.screenshot-item:hover .screenshot-wrapper {
  border-color: #409eff;
}

.screenshot-wrapper img {
  width: 100%;
  height: auto;
  display: block;
  transition: opacity 0.3s ease;
}

.screenshot-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transition: opacity 0.3s ease;
}

.screenshot-item:hover .screenshot-overlay {
  opacity: 1;
}

.zoom-icon {
  font-size: 48px;
  color: white;
}

.screenshot-placeholder,
.screenshot-error {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  color: #999;
  font-size: 14px;
}

.screenshot-placeholder .el-icon,
.screenshot-error .el-icon {
  font-size: 32px;
  margin-bottom: 8px;
}

.screenshot-error {
  color: #f56c6c;
}

.screenshot-info {
  margin-top: 10px;
}

.screenshot-description {
  margin: 0 0 5px 0;
  font-size: 14px;
  font-weight: 500;
  color: #333;
  text-align: left;
}

.screenshot-meta {
  margin: 0 0 3px 0;
  font-size: 12px;
  color: #666;
  text-align: left;
}

.screenshot-time {
  margin: 0;
  font-size: 11px;
  color: #999;
  text-align: left;
}

/* 截图预览对话框样式 */
.screenshot-preview {
  display: flex;
  flex-direction: column;
}

.preview-info {
  margin-bottom: 20px;
  padding: 15px;
  background: #f5f7fa;
  border-radius: 6px;
}

.preview-info h4 {
  margin: 0 0 10px 0;
  font-size: 16px;
  color: #333;
}

.preview-info p {
  margin: 5px 0;
  font-size: 14px;
  color: #666;
}

.preview-image {
  display: flex;
  justify-content: center;
  align-items: center;
  background: #f5f5f5;
  border-radius: 8px;
  padding: 20px;
  max-height: 70vh;
  overflow: auto;
}

.preview-image img {
  max-width: 100%;
  height: auto;
  border-radius: 4px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}

.errors-container {
  padding: 10px;
  height: 100%;
  overflow-y: auto;
}

.error-item {
  background: #fff;
  border: 2px solid #f56c6c;
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 15px;
}

.error-item:last-child {
  margin-bottom: 0;
}

.error-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 15px;
  padding-bottom: 15px;
  border-bottom: 1px solid #f5f5f5;
}

.error-header .el-tag {
  font-size: 16px;
  padding: 10px 15px;
  font-weight: 600;
}

.error-header .el-icon {
  margin-right: 5px;
}

.error-step {
  background: #fef0f0;
  color: #f56c6c;
  padding: 5px 12px;
  border-radius: 4px;
  font-weight: 600;
  font-size: 14px;
}

.error-meta {
  background: #f9f9f9;
  padding: 15px;
  border-radius: 6px;
  margin-bottom: 15px;
}

.meta-item {
  display: flex;
  align-items: flex-start;
  margin-bottom: 8px;
}

.meta-item:last-child {
  margin-bottom: 0;
}

.meta-label {
  font-weight: 600;
  color: #606266;
  min-width: 80px;
  margin-right: 10px;
}

.meta-value {
  color: #303133;
  flex: 1;
}

.error-details {
  background: #2d2d2d;
  border-radius: 6px;
  overflow: hidden;
}

.details-header {
  background: #1e1e1e;
  color: #fff;
  padding: 10px 15px;
  font-weight: 600;
  font-size: 14px;
  border-bottom: 1px solid #3d3d3d;
}

.details-content {
  color: #ff6b6b;
  padding: 15px;
  margin: 0;
  font-family: 'Courier New', Courier, monospace;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-wrap: break-word;
  max-height: 400px;
  overflow-y: auto;
}

.details-content::-webkit-scrollbar {
  width: 6px;
}

.details-content::-webkit-scrollbar-track {
  background: #1e1e1e;
}

.details-content::-webkit-scrollbar-thumb {
  background: #555;
  border-radius: 3px;
}

.details-content::-webkit-scrollbar-thumb:hover {
  background: #777;
}

.no-selection {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.data-factory-btn {
  background-color: #409eff !important;
  border-color: #409eff !important;
  color: white !important;
}

.data-factory-btn:hover {
  background-color: #66b1ff !important;
  border-color: #66b1ff !important;
}

.variable-helper-btn {
  background-color: #67c23a;
  border-color: #67c23a;
  color: white;
}

.variable-helper-btn:hover {
  background-color: #5daf34;
  border-color: #5daf34;
}
</style>