<template>
  <div class="element-manager">
      <div v-if="returnToCase" class="element-setup-guide">
        <p>{{ $t('uiAutomation.workbenchOnboarding.elementGuide') }}</p>
        <RouterLink :to="returnToCase">{{ $t('uiAutomation.workbenchOnboarding.returnToCase') }}</RouterLink>
      </div>
      <p v-if="projectsLoading" class="element-setup-guide" role="status">{{ $t('uiAutomation.dashboard.loading') }}</p>
      <el-alert v-else-if="projectsError || invalidProject" :title="$t(projectsError ? 'uiAutomation.project.messages.loadFailed' : 'uiAutomation.workbenchExecution.invalidProject')" type="warning" :closable="false">
        <el-button v-if="projectsError" link @click="reloadProjects">{{ $t('uiAutomation.workbenchExecution.retry') }}</el-button>
      </el-alert>
    <div class="element-layout">
      <!-- 左侧页面树 -->
      <div class="sidebar">
        <div class="sidebar-header">
          <el-select v-model="selectedProject" :placeholder="$t('common.selectProject')" :aria-label="$t('uiAutomation.common.selectProject')" :disabled="projectsLoading">
            <el-option
              v-for="project in projects"
              :key="project.id"
              :label="project.name"
              :value="project.id"
            />
          </el-select>
          <div class="header-actions">
            <el-button type="primary" size="small" :disabled="!selectedProject" @click="showCreatePageDialog = true" :aria-label="$t('uiAutomation.element.createPage')" :title="$t('uiAutomation.element.createPage')">
              <el-icon><Folder /></el-icon>
            </el-button>
            <el-button type="success" size="small" :disabled="!selectedProject" @click="createEmptyElement" :aria-label="$t('uiAutomation.element.addElement')" :title="$t('uiAutomation.element.addElement')">
              <el-icon><Plus /></el-icon>
            </el-button>
          </div>
        </div>

        <div class="page-tree">
          <el-tree
            ref="treeRef"
            :key="treeKey"
            :data="treeData"
            :props="treeProps"
            node-key="id"
            :expand-on-click-node="false"
            :default-expanded-keys="expandedKeys"
            @node-click="onNodeClick"
            @node-contextmenu="onNodeRightClick"
            @node-expand="onNodeExpand"
            @node-collapse="onNodeCollapse"
          >
            <template #default="{ node, data }">
              <div class="tree-node">
                <el-icon v-if="data.type === 'page'">
                  <Folder />
                </el-icon>
                <el-icon v-else>
                  <Document />
                </el-icon>

                <!-- 页面名称编辑 -->
                <div v-if="data.type === 'page' && editingNodeId === data.id" class="node-edit">
                  <el-input
                    v-model="editingNodeName"
                    size="small"
                    @blur="savePageName"
                    @keyup.enter="savePageName"
                    @keyup.esc="cancelEdit"
                    ref="editInputRef"
                  />
                </div>

                <!-- 普通显示模式 -->
                <span v-else class="node-label">{{ node.label }}</span>

                <span v-if="data.type === 'element'" class="element-type-tag" :class="data.element_type?.toLowerCase()">
                  {{ getElementTypeLabel(data.element_type) }}
                </span>
              </div>
            </template>
          </el-tree>
        </div>
      </div>

      <!-- 右侧元素详情 -->
      <div class="main-content">
        <div v-if="!selectedElement" class="empty-state">
          <el-empty :description="$t('uiAutomation.element.emptyElementTip')">
            <el-button type="primary" @click="createEmptyElement">{{ $t('uiAutomation.element.createNewElement') }}</el-button>
          </el-empty>
        </div>

        <div v-else class="element-detail">
          <!-- 元素基本信息 -->
          <div class="element-header">
            <div class="element-info">
              <el-form ref="elementHeaderFormRef" :model="selectedElement" :rules="elementHeaderRules" inline>
                <el-form-item prop="name" :label="$t('uiAutomation.element.elementName')" required>
                  <el-input
                    v-model="selectedElement.name"
                    :placeholder="$t('uiAutomation.element.elementNamePlaceholder')"
                    style="width: 300px"
                    @blur="validateHeaderField('name')"
                  />
                </el-form-item>
                <el-form-item :label="$t('uiAutomation.element.elementType')">
                  <el-select v-model="selectedElement.element_type" :placeholder="$t('uiAutomation.element.elementType')" style="width: 120px;">
                    <el-option :label="$t('uiAutomation.element.elementTypes.button')" value="BUTTON" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.input')" value="INPUT" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.link')" value="LINK" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.dropdown')" value="DROPDOWN" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.checkbox')" value="CHECKBOX" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.radio')" value="RADIO" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.text')" value="TEXT" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.image')" value="IMAGE" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.table')" value="TABLE" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.form')" value="FORM" />
                    <el-option :label="$t('uiAutomation.element.elementTypes.modal')" value="MODAL" />
                  </el-select>
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" @click="saveElement" :loading="saving" ref="saveButtonRef">
                    {{ $t('uiAutomation.common.save') }}
                  </el-button>
                </el-form-item>
              </el-form>
            </div>
          </div>

          <!-- 元素配置 -->
          <div class="element-form">
            <el-form ref="elementFormRef" :key="formKey" :model="selectedElement" :rules="elementRules" label-width="100px">
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('uiAutomation.element.page')">
                    <el-select v-model="selectedElement.page" :placeholder="$t('uiAutomation.element.selectPage')">
                      <el-option
                        v-for="page in pages"
                        :key="page.id"
                        :label="page.name"
                        :value="page.name"
                      />
                    </el-select>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item :label="$t('uiAutomation.element.componentName')">
                    <el-input v-model="selectedElement.component_name" :placeholder="$t('uiAutomation.element.componentNamePlaceholder')" />
                  </el-form-item>
                </el-col>
              </el-row>

              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('uiAutomation.element.locatorStrategy')" prop="locator_strategy_id" required>
                    <el-select
                      v-model="selectedElement.locator_strategy_id"
                      :key="`strategy-${formKey}-${selectedElement.locator_strategy_id || 'null'}`"
                      :placeholder="$t('uiAutomation.element.rules.strategyRequired')"
                      value-key="id"
                      @blur="validateField('locator_strategy_id')"
                    >
                      <el-option
                        v-for="strategy in locatorStrategies"
                        :key="strategy.id"
                        :label="strategy.name"
                        :value="strategy.id"
                      />
                    </el-select>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item :label="$t('uiAutomation.element.waitTimeout') + '(' + $t('uiAutomation.element.waitTimeoutUnit') + ')'">
                    <el-input-number v-model="selectedElement.wait_timeout" :min="1" :max="60" style="width: 100%" />
                  </el-form-item>
                </el-col>
              </el-row>

              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item :label="$t('uiAutomation.element.forceAction')">
                    <el-switch
                      v-model="selectedElement.force_action"
                      :active-text="$t('uiAutomation.element.forceActionEnabled')"
                      :inactive-text="$t('uiAutomation.element.forceActionDisabled')"
                    />
                    <div class="form-help-text" style="margin-top: 5px;">
                      {{ $t('uiAutomation.element.forceActionTip') }}
                    </div>
                  </el-form-item>
                </el-col>
              </el-row>

              <el-form-item :label="$t('uiAutomation.element.locatorExpression')" prop="locator_value" required>
                <el-input v-model="selectedElement.locator_value" :placeholder="$t('uiAutomation.element.locatorExpressionPlaceholder')" @blur="validateField('locator_value')" />
                <div class="form-help-text">
                  {{ $t('uiAutomation.element.locatorTip.title') }}<br>
                  - {{ $t('uiAutomation.element.locatorTip.id') }}<br>
                  - {{ $t('uiAutomation.element.locatorTip.css') }}<br>
                  - {{ $t('uiAutomation.element.locatorTip.xpath') }}<br>
                  - {{ $t('uiAutomation.element.locatorTip.other') }}
                </div>
                <div class="locator-validation-controls">
                  <el-radio-group v-model="locatorValidationMode" size="small" :aria-label="$t('uiAutomation.element.validationModeLabel')">
                    <el-radio-button value="format">{{ $t('uiAutomation.element.validationFormat') }}</el-radio-button>
                    <el-radio-button value="live">{{ $t('uiAutomation.element.validationLive') }}</el-radio-button>
                  </el-radio-group>
                  <el-input
                    v-if="locatorValidationMode === 'live'"
                    v-model="locatorValidationTargetUrl"
                    type="url"
                    size="small"
                    :placeholder="selectedProjectBaseUrl || $t('uiAutomation.element.validationTargetUrlPlaceholder')"
                    :aria-label="$t('uiAutomation.element.validationTargetUrl')"
                  />
                  <p class="validation-mode-note">{{ $t(locatorValidationMode === 'live' ? 'uiAutomation.element.validationLiveNote' : 'uiAutomation.element.validationFormatNote') }}</p>
                  <p class="validation-mode-note">{{ $t('uiAutomation.element.validationSavedNote') }}</p>
                  <el-button size="small" :loading="validating" :disabled="validating || !normalizeUiId(selectedElement.id) || !selectedElement.locator_value?.trim()" @click="validateElement">
                    {{ $t('uiAutomation.element.validateElement') }}
                  </el-button>
                  <div v-if="locatorValidationResult" class="locator-validation-result" :class="`is-${locatorValidationResult.validation_status.toLowerCase()}`" role="status">
                    <div class="validation-result-heading">
                      <el-tag :type="locatorValidationTagType(locatorValidationResult.validation_status)">{{ $t(`uiAutomation.element.validationStatus.${locatorValidationResult.validation_status.toLowerCase()}`) }}</el-tag>
                      <span>{{ $t(`uiAutomation.element.validationMode.${locatorValidationResult.validation_mode}`) }}</span>
                    </div>
                    <p>{{ locatorValidationResult.validation_message }}</p>
                    <div class="validation-result-meta">
                      <span>{{ $t('uiAutomation.element.matchCount') }}: {{ locatorValidationResult.match_count ?? '-' }}</span>
                      <time v-if="locatorValidationResult.checked_at" :datetime="locatorValidationResult.checked_at">{{ formatLocatorValidationDate(locatorValidationResult.checked_at) }}</time>
                    </div>
                    <p v-if="locatorValidationResult.validation_status === 'UNKNOWN' && locatorValidationResult.validation_mode === 'live'" class="validation-live-help">
                      {{ $t('uiAutomation.element.validationLiveUnknownHelp') }}
                    </p>
                  </div>
                </div>
              </el-form-item>

              <el-form-item :label="$t('uiAutomation.common.description')">
                <el-input v-model="selectedElement.description" type="textarea" :rows="3" :placeholder="$t('uiAutomation.element.descriptionPlaceholder')" />
              </el-form-item>
            </el-form>
          </div>
        </div>
      </div>
    </div>

    <!-- 创建页面对话框 -->
    <el-dialog v-model="showCreatePageDialog" :title="$t('uiAutomation.element.createPageTitle')" width="500px" :close-on-click-modal="false">
      <el-form ref="pageFormRef" :model="pageForm" :rules="pageRules" label-width="100px">
        <el-form-item :label="$t('uiAutomation.element.pageName')" prop="name">
          <el-input v-model="pageForm.name" :placeholder="$t('uiAutomation.element.pageNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.element.parentPage')">
          <el-select v-model="pageForm.parent_page" :placeholder="$t('uiAutomation.element.selectParentPage')" clearable>
            <el-option
              v-for="page in getAllPages()"
              :key="page.id"
              :label="page.name"
              :value="page.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.common.description')" prop="description">
          <el-input v-model="pageForm.description" type="textarea" :rows="3" :placeholder="$t('uiAutomation.element.descriptionPlaceholder')" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="showCreatePageDialog = false">{{ $t('uiAutomation.common.cancel') }}</el-button>
        <el-button type="primary" @click="createPage">{{ $t('uiAutomation.common.confirm') }}</el-button>
      </template>
    </el-dialog>

    <!-- 右键菜单 -->
    <ul v-show="showContextMenu" class="context-menu" :style="{ left: contextMenuX + 'px', top: contextMenuY + 'px' }">
      <li @click="addContextElement">{{ $t('uiAutomation.element.contextMenu.addElement') }}</li>
      <!-- 只有在普通页面节点下才显示"新增子页面"选项 -->
      <li v-if="rightClickedNode && rightClickedNode.type === 'page' && rightClickedNode.id !== 'unassigned'" @click="addSubPage">
        {{ $t('uiAutomation.element.contextMenu.addSubPage') }}
      </li>
      <!-- "未关联页面"节点不显示编辑和删除选项 -->
      <li v-if="rightClickedNode && rightClickedNode.id !== 'unassigned'" @click="editNode">
        {{ $t('uiAutomation.element.contextMenu.edit') }}
      </li>
      <li v-if="rightClickedNode && rightClickedNode.id !== 'unassigned'" @click="deleteNode">
        {{ $t('uiAutomation.element.contextMenu.delete') }}
      </li>
    </ul>

    <!-- 编辑页面对话框 -->
    <el-dialog v-model="showEditPageDialog" :title="$t('uiAutomation.element.editPageTitle')" width="500px" :close-on-click-modal="false">
      <el-form ref="editPageFormRef" :model="editPageForm" :rules="pageRules" label-width="100px">
        <el-form-item :label="$t('uiAutomation.element.pageName')" prop="name">
          <el-input v-model="editPageForm.name" :placeholder="$t('uiAutomation.element.pageNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.element.parentPage')">
          <el-select v-model="editPageForm.parent_page" :placeholder="$t('uiAutomation.element.selectParentPage')" clearable>
            <el-option
              v-for="page in getAllPagesExceptCurrent(editPageForm.id)"
              :key="page.id"
              :label="page.name"
              :value="page.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('uiAutomation.common.description')" prop="description">
          <el-input v-model="editPageForm.description" type="textarea" :rows="3" :placeholder="$t('uiAutomation.element.descriptionPlaceholder')" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="showEditPageDialog = false">{{ $t('uiAutomation.common.cancel') }}</el-button>
        <el-button type="primary" @click="updatePage">{{ $t('uiAutomation.common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { useUiProjectContext } from '@/composables/useUiProjectContext'
import { normalizeUiId } from '@/utils/ui-project-context.mjs'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus, FolderAdd, Document, Search, Edit, Delete,
  Folder, Document as DocumentIcon, Operation, DocumentCopy, ArrowDown
} from '@element-plus/icons-vue'
import {
  createElement,
  getElementDetail,
  updateElement,
  deleteElement,
  getElementTree,
  getElementGroupTree,
  getElementGroups,
  createElementGroup,
  updateElementGroup,
  deleteElementGroup,
  getLocatorStrategies,
  validateElementLocator,
  generateElementSuggestions
} from '@/api/ui_automation'

// 国际化
const { t } = useI18n()
let hideMenuTimeout = null
let activeHideMenu = null

const route = useRoute()
const { projects, projectId: selectedProject, projectsLoading, projectsError, invalidProject, reloadProjects, userId } = useUiProjectContext()
const selectedProjectBaseUrl = computed(() => projects.value.find(project => String(project.id) === String(selectedProject.value))?.base_url || '')
const returnToCase = computed(() => {
  const caseId = normalizeUiId(route.query.caseId)
  if (!caseId || !selectedProject.value) return null
  return { name: 'UiTestCases', query: { project: String(selectedProject.value), caseId: String(caseId) } }
})
let projectScope = 0
let treeRequest = 0
let pagesRequest = 0
const pages = ref([])
const locatorStrategies = ref([])
const treeData = ref([])
const selectedElement = ref(null)
const expandedKeys = ref([])
const treeKey = ref(0) // 用于强制重新渲染树组件
const formKey = ref(0) // 用于强制重新渲染表单组件

// 表单引用
const treeRef = ref(null)
const pageFormRef = ref(null)
const editPageFormRef = ref(null)
const elementFormRef = ref(null)
const elementHeaderFormRef = ref(null)

// 对话框控制
const showCreatePageDialog = ref(false)
const showEditPageDialog = ref(false)

// 右键菜单
const showContextMenu = ref(false)
const contextMenuX = ref(0)
const contextMenuY = ref(0)
const rightClickedNode = ref(null)

// 表单数据
const pageForm = reactive({
  name: '',
  description: '',
  parent_page: null
})

const editPageForm = reactive({
  id: null,
  name: '',
  description: '',
  parent_page: null
})

// 树形组件配置
const treeProps = {
  children: 'children',
  label: 'name'
}

// 表单验证规则
const pageRules = computed(() => ({
  name: [
    { required: true, message: t('uiAutomation.element.rules.pageNameRequired'), trigger: 'blur' }
  ]
}))

// 元素表单头部验证规则（元素名称）
const elementHeaderRules = computed(() => ({
  name: [
    { required: true, message: t('uiAutomation.element.rules.nameRequired'), trigger: 'blur' },
    { min: 1, max: 200, message: t('uiAutomation.element.rules.nameLength'), trigger: 'blur' }
  ]
}))

// 元素表单验证规则
const elementRules = computed(() => ({
  locator_strategy_id: [
    { required: true, message: t('uiAutomation.element.rules.strategyRequired'), trigger: 'change' }
  ],
  locator_value: [
    { required: true, message: t('uiAutomation.element.rules.locatorRequired'), trigger: 'blur' },
    { min: 1, max: 500, message: t('uiAutomation.element.rules.locatorLength'), trigger: 'blur' }
  ]
}))

// 获取元素类型标签
const getElementTypeLabel = (type) => {
  const typeKey = type?.toLowerCase()
  const typeMap = {
    'button': t('uiAutomation.element.elementTypes.button'),
    'input': t('uiAutomation.element.elementTypes.input'),
    'link': t('uiAutomation.element.elementTypes.link'),
    'dropdown': t('uiAutomation.element.elementTypes.dropdown'),
    'checkbox': t('uiAutomation.element.elementTypes.checkbox'),
    'radio': t('uiAutomation.element.elementTypes.radio'),
    'text': t('uiAutomation.element.elementTypes.text'),
    'image': t('uiAutomation.element.elementTypes.image'),
    'table': t('uiAutomation.element.elementTypes.table'),
    'form': t('uiAutomation.element.elementTypes.form'),
    'modal': t('uiAutomation.element.elementTypes.modal')
  }
  return typeMap[typeKey] || type
}

// 获取所有页面
const getAllPages = () => {
  const allPages = []

  const traverse = (nodes) => {
    nodes.forEach(node => {
      if (node.type === 'page') {
        allPages.push({
          id: node.id,
          name: node.name
        })
      }
      if (node.children) {
        traverse(node.children)
      }
    })
  }

  traverse(treeData.value)
  return allPages
}

// 获取所有页面（除了指定ID的页面）
const getAllPagesExceptCurrent = (currentId) => {
  const allPages = []

  const traverse = (nodes) => {
    nodes.forEach(node => {
      if (node.type === 'page' && node.id !== currentId) {
        allPages.push({
          id: node.id,
          name: node.name
        })
      }
      if (node.children) {
        traverse(node.children)
      }
    })
  }

  traverse(treeData.value)
  return allPages
}

// 页面名称编辑相关
const editingNodeId = ref(null)
const editingNodeName = ref('')
const editInputRef = ref(null)

// 状态
const saving = ref(false)
const validating = ref(false)
const locatorValidationMode = ref('format')
const locatorValidationTargetUrl = ref('')
const locatorValidationResult = ref(null)
let locatorValidationRequest = 0
const generating = ref(false)
const suggestions = ref([])


// 将关键变量暴露到window对象，方便在控制台调试
const exposeToWindow = () => {
  if (typeof window !== 'undefined') {
    window.ELEMENTS_DEBUG = {
      treeData,
      projects,
      selectedElement,
      loadElementTree,
      treeRef: typeof treeRef !== 'undefined' ? treeRef : null,
      expandedKeys,
      pages,
      $vm: { // 当前组件实例
        treeData: treeData.value,
        projects: projects.value,
        pages: pages.value,
        expandedKeys: expandedKeys.value
      }
    }
    console.log('=== Vue组件调试信息已暴露 ===')
    console.log('Window可用调试变量已设置')
    console.log('控制台可直接访问:')
    console.log('  window.ELEMENTS_DEBUG.treeData')
    console.log('  window.ELEMENTS_DEBUG.projects')
    console.log('  window.ELEMENTS_DEBUG.selectedElement')
    console.log('==============================')
  }
}

onMounted(() => {
  void loadLocatorStrategies()
  exposeToWindow()
})

onBeforeUnmount(() => {
  projectScope += 1
  locatorValidationRequest += 1
  if (hideMenuTimeout) clearTimeout(hideMenuTimeout)
  if (activeHideMenu) document.removeEventListener('click', activeHideMenu)
  hideMenuTimeout = null
  activeHideMenu = null
})

// 提供控制台调试帮助函数
const debugTree = () => {
  if (typeof window !== 'undefined') {
    console.log('=== 树数据调试 ===')
    console.log('treeData:', treeData.value)
    console.log('页面对象:',
      treeData.value.map(p => ({
        id: p.id,
        name: p.name,
        type: p.type,
        children: p.children?.length || 0,
        elementChildren: p.children?.filter(c => c.type === 'element').map(e => e.name) || []
      }))
    )

    // 找出所有元素
    const allElements = []
    const findElements = (nodes, parent) => {
      nodes.forEach(node => {
        if (node.type === 'element') {
          allElements.push({
            name: node.name,
            id: node.id,
            parent: parent
          })
        } else if (node.type === 'page' && node.children) {
          findElements(node.children, node.name)
        }
      })
    }
    findElements(treeData.value, null)
    console.log('所有元素:', allElements)

    // 暴露到window
    window.debugTreeData = debugTree
    console.log('调试函数已挂载到 window.debugTreeData()')
    console.log('===============================')
  }
}

// 加载定位策略
const loadLocatorStrategies = async () => {
  try {
    const response = await getLocatorStrategies()
    locatorStrategies.value = response.data?.results || response.data || []
  } catch (error) {
    console.error('获取定位策略失败:', error)
  }
}

// 加载页面（分组）
const loadPages = async () => {
  const project = selectedProject.value
  const scope = projectScope
  const request = ++pagesRequest
  if (!project) return
  try {
    const response = await getElementGroups({ project })
    if (scope === projectScope && request === pagesRequest && project === selectedProject.value) {
      pages.value = response.data?.results || response.data || []
    }
  } catch (error) {
    console.error('获取页面失败:', error)
  }
}

// 加载元素树
const loadElementTree = async () => {
  const project = selectedProject.value
  const scope = projectScope
  const request = ++treeRequest
  const isCurrent = () => scope === projectScope && request === treeRequest && project === selectedProject.value
  if (!project) {
    treeData.value = []
    return
  }

  try {
    const [pageTreeResponse, elementsResponse] = await Promise.all([
      getElementGroupTree({ project }),
      getElementTree({ project })
    ])
    if (!isCurrent()) return

    // 构建完整的树形结构
    const buildTree = (groups) => {
      return groups.map(group => ({
        ...group,
        type: 'page',
        children: group.children ? buildTree(group.children) : []
      }))
    }

    const pageNodes = buildTree(pageTreeResponse.data || [])

    // 调试信息 - 检查API返回的完整响应结构
    console.log('=== 加载元素树调试 ===')
    console.log('页面树响应:', pageTreeResponse)
    console.log('元素响应:', elementsResponse)

    // 打印原始数据进行分析
    console.log('页面树原始数据:', JSON.parse(JSON.stringify(pageTreeResponse.data || []), null, 2))

    const elements = elementsResponse.data?.results || elementsResponse.data || []
    console.log('提取的元素列表:', elements)

    // 获取所有页面的ID，用于调试
    const pageIds = pageNodes.map(page => page.id)
    console.log('页面ID列表:', pageIds)

    // 将元素添加到对应页面下
    const attachedElementIds = new Set()

    const attachElementsToPages = (pages) => {
      pages.forEach(page => {
        // 找到属于当前页面的元素
        const pageElements = elements.filter(element => element.group_id === page.id)
        console.log(`页面 ${page.name} (ID: ${page.id}) 找到 ${pageElements.length} 个关联元素`, pageElements)

        const elementNodes = pageElements.map(element => {
          attachedElementIds.add(element.id)
          return {
            ...element,
            type: 'element'
          }
        })

        // 将元素添加到页面的子节点中
        page.children = page.children ? [...page.children, ...elementNodes] : [...elementNodes]
        console.log(`页面 ${page.name} 现在有 ${page.children.filter(c => c.type === 'element').length} 个子元素`)

        // 递归处理子页面
        if (page.children) {
          attachElementsToPages(page.children.filter(child => child.type === 'page'))
        }
      })
    }

    attachElementsToPages(pageNodes)

    // 添加未关联页面的元素到"未关联页面"节点
    // 包括：1. group_id 为 null/undefined 的元素
    //       2. group_id 指向的页面不存在的元素
    const unassignedElements = elements.filter(element => {
      // 如果没有group_id，肯定是未关联的
      if (!element.group_id) {
        return true
      }
      // 如果有group_id但没有被添加到任何页面（页面不存在），也算未关联
      return !attachedElementIds.has(element.id)
    })

    console.log('未关联页面的元素:', unassignedElements)

    if (unassignedElements.length > 0) {
      const unassignedPage = {
        id: 'unassigned',
        name: '未关联页面',
        type: 'page',
        children: unassignedElements.map(element => ({
          ...element,
          type: 'element'
        }))
      }
      pageNodes.unshift(unassignedPage) // 添加到列表最前面
      console.log(`已添加 ${unassignedElements.length} 个未关联元素到"未关联页面"节点`)
      // 默认展开未关联页面节点
      expandedKeys.value.push('unassigned')
    }

    console.log('最终treeData:', pageNodes)
    treeData.value = pageNodes

    // 将treeData暴露到window，方便在控制台调试
    if (typeof window !== 'undefined') {
      window.vue_treeData = treeData.value
      console.log('treeData已挂载到window.vue_treeData，可在控制台查看')
      console.log('当前treeData结构:', JSON.parse(JSON.stringify(treeData.value)).map(p => ({
        name: p.name,
        id: p.id,
        children: p.children?.filter(c => c.type === 'element').length || 0
      })))
    }
  } catch (error) {
    if (isCurrent()) {
      console.error('获取元素树失败:', error)
      treeData.value = []
    }
  }
}

watch([selectedProject, userId], async ([project]) => {
  const scope = ++projectScope
  locatorValidationTargetUrl.value = selectedProjectBaseUrl.value
  locatorValidationResult.value = null
  validating.value = false
  selectedElement.value = null
  treeData.value = []
  pages.value = []
  expandedKeys.value = []
  suggestions.value = []
  showCreatePageDialog.value = false
  showEditPageDialog.value = false
  showContextMenu.value = false
  if (!project) return
  await Promise.all([loadPages(), loadElementTree()])
  if (scope === projectScope && selectedProject.value === project) treeKey.value += 1
}, { immediate: true })

// 创建空元素
const createEmptyElement = () => {
  selectedElement.value = {
    name: '',
    element_type: 'BUTTON',
    page: '',
    component_name: '',
    locator_strategy_id: null, // 使用null而不是空字符串
    locator_value: '',
    wait_timeout: 5,
    force_action: false,  // 强制操作选项，默认禁用
    description: ''
  }
}

// 验证单个字段（用于失焦验证）
const validateField = async (field) => {
  if (!elementFormRef.value) return
  try {
    await elementFormRef.value.validateField(field)
  } catch (error) {
    // 验证失败，不需要做任何处理，错误会自动显示
  }
}

// 验证头部表单字段（元素名称）
const validateHeaderField = async (field) => {
  if (!elementHeaderFormRef.value) return
  try {
    await elementHeaderFormRef.value.validateField(field)
  } catch (error) {
    // 验证失败，不需要做任何处理，错误会自动显示
  }
}

// 验证整个元素表单
const validateElementForm = async () => {
  const results = await Promise.allSettled([
    elementHeaderFormRef.value?.validate() ?? Promise.resolve(),
    elementFormRef.value?.validate() ?? Promise.resolve()
  ])

  // 检查是否有验证失败的情况
  const hasFailed = results.some(result => result.status === 'rejected')
  return !hasFailed
}

// 创建页面
const createPage = async () => {
  const validate = await pageFormRef.value.validate()
  if (!validate) return

  try {
    // 构建创建页面的参数，正确处理父页面参数
    const pageData = {
      name: pageForm.name,
      description: pageForm.description,
      project: selectedProject.value
    }

    // 只有当父页面ID存在且不为空时才添加parent_group字段
    if (pageForm.parent_page) {
      pageData.parent_group = pageForm.parent_page
    }

    await createElementGroup(pageData)

    ElMessage.success(t('uiAutomation.element.messages.pageCreateSuccess'))
    showCreatePageDialog.value = false

    // 重置表单
    Object.assign(pageForm, {
      name: '',
      description: '',
      parent_page: null
    })

    // 重新加载页面和树
    await Promise.all([
      loadPages(),
      loadElementTree()
    ])

    // 强制刷新树组件
    treeKey.value += 1
  } catch (error) {
    console.error('创建页面失败:', error)
    ElMessage.error(t('uiAutomation.element.messages.pageCreateFailed'))
  }
}

// 节点点击
const onNodeClick = async (data) => {
  if (data.type === 'element') {
    const scope = projectScope
    try {
      const response = await getElementDetail(data.id)
      if (scope !== projectScope) return
      selectedElement.value = response.data

      // 强制刷新表单，确保下拉框正确显示
      formKey.value += 1
      console.log('点击节点时formKey更新为:', formKey.value)
    } catch (error) {
      console.error('获取元素详情失败:', error)
    }
  }
}

// 节点右键点击
const onNodeRightClick = (event, data) => {
  console.log('Node right click event:', event, 'Data:', data)
  event.preventDefault()

  // 隐藏现有菜单
  showContextMenu.value = false

  // 设置右键点击的节点
  rightClickedNode.value = data
  console.log('Set right clicked node:', data)

  // 设置菜单位置
  contextMenuX.value = event.clientX
  contextMenuY.value = event.clientY

  // 显示菜单
  showContextMenu.value = true
  console.log('Show context menu at:', contextMenuX.value, contextMenuY.value)

  // 添加全局点击监听器以隐藏菜单
  const hideMenu = () => {
    console.log('Hide context menu')
    showContextMenu.value = false
    document.removeEventListener('click', hideMenu)
    if (activeHideMenu === hideMenu) activeHideMenu = null
  }
  activeHideMenu = hideMenu

  // 延迟添加监听器，避免立即触发
  hideMenuTimeout = setTimeout(() => {
    hideMenuTimeout = null
    if (activeHideMenu === hideMenu) {
      document.addEventListener('click', hideMenu)
    }
  }, 100)
}

// 节点展开
const onNodeExpand = (data) => {
  if (!expandedKeys.value.includes(data.id)) {
    expandedKeys.value.push(data.id)
  }
}

// 节点收起
const onNodeCollapse = (data) => {
  const index = expandedKeys.value.indexOf(data.id)
  if (index > -1) {
    expandedKeys.value.splice(index, 1)
  }
}

// 保存元素
const saveElement = async () => {
  if (!selectedElement.value) return

  // 验证表单
  const isValid = await validateElementForm()
  if (!isValid) {
    ElMessage.error(t('uiAutomation.element.messages.saveFailed'))
    return
  }

  try {
    saving.value = true
    console.log('=== 保存元素调试 ===')
    console.log('当前选中的元素:', selectedElement.value)

    if (selectedElement.value.id) {
      // 更新元素 - 构建正确的API数据格式
      const elementUpdateData = {
        name: selectedElement.value.name,
        element_type: selectedElement.value.element_type,
        page: selectedElement.value.page,
        component_name: selectedElement.value.component_name,
        description: selectedElement.value.description,
        locator_strategy_id: selectedElement.value.locator_strategy_id,
        locator_value: selectedElement.value.locator_value,
        wait_timeout: selectedElement.value.wait_timeout,
        force_action: selectedElement.value.force_action,
        project_id: selectedProject.value
      }

      // 如果元素有分组（页面），确保传递正确的 group_id
      if (selectedElement.value.page) {
        console.log('更新元素 - 元素关联页面名称:', selectedElement.value.page)

        // 通过遍历树形结构查找对应的页面ID
        const findPageIdByName = (nodes, pageName) => {
          for (const node of nodes) {
            if (node.type === 'page' && node.name === pageName) {
              return node.id
            }
            if (node.children) {
              const foundId = findPageIdByName(node.children, pageName)
              if (foundId) return foundId
            }
          }
          return null
        }

        const pageId = findPageIdByName(treeData.value, selectedElement.value.page)
        if (pageId) {
          elementUpdateData.group_id = pageId
        }
      }

      console.log('更新元素数据:', elementUpdateData)
      await updateElement(selectedElement.value.id, elementUpdateData)

      // 重新获取完整的元素详情以确保所有关联字段正确显示
      const detailResponse = await getElementDetail(selectedElement.value.id)
      selectedElement.value = detailResponse.data
      console.log('更新后获取到完整元素详情:', selectedElement.value)
      console.log('locator_strategy_id值:', selectedElement.value.locator_strategy_id, '类型:', typeof selectedElement.value.locator_strategy_id)
      console.log('locator_strategy对象:', selectedElement.value.locator_strategy)
      console.log('当前locatorStrategies:', locatorStrategies.value)
      console.log('locatorStrategies中是否包含id=' + selectedElement.value.locator_strategy_id + ':',
        locatorStrategies.value.find(s => s.id === selectedElement.value.locator_strategy_id))

      // 强制刷新表单，确保下拉框正确显示
      formKey.value += 1
      console.log('formKey更新为:', formKey.value)

      // 使用nextTick确保DOM更新
      await nextTick()
      console.log('DOM已更新，当前下拉框绑定值:', selectedElement.value.locator_strategy_id)

      ElMessage.success(t('uiAutomation.element.messages.saveSuccess'))
    } else {
      // 创建元素
      // 确保传递正确的字段名 project_id 而不是 project
      const elementData = {
        ...selectedElement.value,
        project_id: selectedProject.value
      }

      // 如果元素有分组（页面），确保传递 group_id
      if (selectedElement.value.page) {
        console.log('元素关联页面名称:', selectedElement.value.page)
        console.log('当前treeData结构:', treeData.value)

        // 通过遍历树形结构查找对应的页面ID
        const findPageIdByName = (nodes, pageName) => {
          console.log(`在 ${nodes.length} 个节点中查找页面名称: ${pageName}`)
          for (const node of nodes) {
            console.log(`检查节点: ${node.name} (ID: ${node.id}, type: ${node.type})`)
            if (node.type === 'page' && node.name === pageName) {
              console.log(`找到页面! ID: ${node.id}`)
              return node.id
            }
            if (node.children) {
              console.log(`检查子节点:`, node.children.map(c => c.name))
              const foundId = findPageIdByName(node.children, pageName)
              if (foundId) return foundId
            }
          }
          console.log('未找到页面')
          return null
        }

        const pageId = findPageIdByName(treeData.value, selectedElement.value.page)
        console.log('找到的页面ID:', pageId)

        if (pageId) {
          elementData.group_id = pageId
          console.log('设置group_id为:', pageId)
        }
      }

      console.log('创建元素的数据:', elementData)
      const response = await createElement(elementData)
      console.log('创建响应:', response)

      // 重新获取完整的元素详情以确保所有关联字段正确显示
      const detailResponse = await getElementDetail(response.data.id)
      selectedElement.value = detailResponse.data
      console.log('获取到完整元素详情:', selectedElement.value)
      console.log('locator_strategy_id值:', selectedElement.value.locator_strategy_id, '类型:', typeof selectedElement.value.locator_strategy_id)
      console.log('locator_strategy对象:', selectedElement.value.locator_strategy)
      console.log('当前locatorStrategies:', locatorStrategies.value)
      console.log('locatorStrategies中是否包含id=' + selectedElement.value.locator_strategy_id + ':',
        locatorStrategies.value.find(s => s.id === selectedElement.value.locator_strategy_id))
      console.log('el-select绑定的值:', selectedElement.value.locator_strategy_id)

      // 强制刷新表单，确保下拉框正确显示
      formKey.value += 1
      console.log('formKey更新为:', formKey.value)

      // 使用nextTick确保DOM更新
      await nextTick()
      console.log('DOM已更新，当前下拉框绑定值:', selectedElement.value.locator_strategy_id)

      ElMessage.success(t('uiAutomation.element.messages.createSuccess'))
    }

    // 重新加载树
    console.log('开始重新加载元素树...')
    await loadElementTree()
    console.log('元素树重新加载完成')

    // 强制重新渲染树组件
    treeKey.value += 1
    console.log('树组件key更新为:', treeKey.value)

    // 强制触发Vue更新和树组件刷新
    nextTick(() => {
      console.log('nextTick - 检查treeData:', treeData.value)
      console.log('treeRef:', treeRef.value)

      // 展开新创建元素所在的页面节点
      if (selectedElement.value && selectedElement.value.group_id) {
        console.log('展开元素所在页面:', selectedElement.value.group_id)
        if (!expandedKeys.value.includes(selectedElement.value.group_id)) {
          expandedKeys.value.push(selectedElement.value.group_id)
        }
      }

      console.log('树数据更新完成，当前expandedKeys:', expandedKeys.value)
    })
  } catch (error) {
    console.error('保存元素失败:', error)
    ElMessage.error(t('uiAutomation.element.messages.saveFailed') + ': ' + (error.response?.data?.message || error.message || t('uiAutomation.messages.error.unknown')))
  } finally {
    saving.value = false
  }
}

const validateElement = async () => {
  const element = selectedElement.value
  const elementId = normalizeUiId(element?.id)
  const project = normalizeUiId(selectedProject.value)
  const actor = normalizeUiId(userId?.value ?? userId)
  const locatorValue = String(element?.locator_value || '')
  const locatorStrategyId = normalizeUiId(element?.locator_strategy_id)
  if (!elementId || !project || !locatorValue.trim()) return

  const request = ++locatorValidationRequest
  const scope = projectScope
  const mode = locatorValidationMode.value
  const targetUrl = String(locatorValidationTargetUrl.value || selectedProjectBaseUrl.value || '').trim()
  const isCurrent = () => request === locatorValidationRequest && scope === projectScope &&
    normalizeUiId(selectedProject.value) === project &&
    normalizeUiId(userId?.value ?? userId) === actor &&
    normalizeUiId(selectedElement.value?.id) === elementId &&
    selectedElement.value?.locator_value === locatorValue &&
    normalizeUiId(selectedElement.value?.locator_strategy_id) === locatorStrategyId &&
    locatorValidationMode.value === mode &&
    (mode !== 'live' || String(locatorValidationTargetUrl.value || selectedProjectBaseUrl.value || '').trim() === targetUrl)

  if (mode === 'live' && !targetUrl) {
    locatorValidationResult.value = {
      validation_status: 'UNKNOWN', validation_mode: mode,
      validation_message: t('uiAutomation.element.validationTargetUrlRequired'),
      match_count: null, checked_at: new Date().toISOString()
    }
    return
  }

  validating.value = true
  locatorValidationResult.value = null
  try {
    const payload = { mode, engine: 'playwright', browser: 'chrome', expected_locator_value: locatorValue }
    if (locatorStrategyId) payload.expected_locator_strategy_id = locatorStrategyId
    if (mode === 'live') payload.target_url = targetUrl
    const response = await validateElementLocator(elementId, payload)
    if (!isCurrent()) return
    const result = response?.data?.data ?? response?.data ?? response
    const candidate = String(result?.validation_status || '').toUpperCase()
    const validationStatus = ['VALID', 'INVALID', 'UNKNOWN'].includes(candidate) ? candidate : 'UNKNOWN'
    const matchCount = result?.match_count !== undefined && result?.match_count !== null && Number.isFinite(Number(result.match_count))
      ? Number(result.match_count)
      : null
    locatorValidationResult.value = {
      validation_status: validationStatus,
      validation_mode: result?.validation_mode === 'live' || result?.validation_mode === 'format' ? result.validation_mode : mode,
      validation_message: result?.validation_message || t(validationStatus === 'UNKNOWN' ? 'uiAutomation.element.validationUnknown' : validationStatus === 'VALID' ? 'uiAutomation.element.validationPassed' : 'uiAutomation.element.validationFailed'),
      match_count: matchCount,
      checked_at: result?.checked_at || new Date().toISOString()
    }
  } catch (error) {
    if (!isCurrent()) return
    const body = error.response?.data?.data ?? error.response?.data ?? {}
    locatorValidationResult.value = {
      validation_status: 'UNKNOWN',
      validation_mode: mode,
      validation_message: body.validation_message || body.message || body.error || body.detail || error.message || t('uiAutomation.element.validationUnknown'),
      match_count: body.match_count ?? null,
      checked_at: body.checked_at || new Date().toISOString()
    }
  } finally {
    if (request === locatorValidationRequest) validating.value = false
  }
}

function locatorValidationTagType(status) {
  return ({ VALID: 'success', INVALID: 'danger', UNKNOWN: 'info' })[status] || 'info'
}

function formatLocatorValidationDate(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString()
}

watch(selectedProjectBaseUrl, value => {
  locatorValidationTargetUrl.value = value
}, { immediate: true })

watch([selectedProject, () => selectedElement.value?.id, () => selectedElement.value?.locator_value, () => selectedElement.value?.locator_strategy_id, locatorValidationMode, locatorValidationTargetUrl], () => {
  locatorValidationRequest += 1
  validating.value = false
  locatorValidationResult.value = null
}, { flush: 'sync' })

// 生成建议
const generateSuggestions = async () => {
  if (!selectedElement.value) return

  try {
    generating.value = true
    const response = await generateElementSuggestions(selectedElement.value.id)
    suggestions.value = response.data.suggestions
  } catch (error) {
    console.error('生成建议失败:', error)
  } finally {
    generating.value = false
  }
}

// 保存页面名称
const savePageName = () => {
  // TODO: 实现页面名称保存
  editingNodeId.value = null
}

// 取消编辑
const cancelEdit = () => {
  editingNodeId.value = null
}

// 右键菜单操作函数
// 新增元素
const addContextElement = () => {
  console.log('Add context element clicked')
  showContextMenu.value = false
  createEmptyElement()

  // 如果右键点击的是页面节点，设置元素的页面
  if (rightClickedNode.value && rightClickedNode.value.type === 'page') {
    // 特殊处理：如果是"未关联页面"节点，不设置page和group_id
    if (rightClickedNode.value.id === 'unassigned') {
      console.log('在未关联页面节点下添加元素，不设置page和group_id')
      return
    }

    if (selectedElement.value) {
      selectedElement.value.page = rightClickedNode.value.name
      // 同时设置group_id，确保元素能正确关联到页面
      selectedElement.value.group_id = rightClickedNode.value.id
    }
  }
}

// 新增子页面
const addSubPage = () => {
  console.log('Add sub page clicked')
  showContextMenu.value = false

  // 禁止在"未关联页面"节点下创建子页面
  if (rightClickedNode.value && rightClickedNode.value.id === 'unassigned') {
    ElMessage.warning('未关联页面节点下不能创建子页面')
    return
  }

  showCreatePageDialog.value = true

  // 如果右键点击的是页面节点，设置父页面
  if (rightClickedNode.value && rightClickedNode.value.type === 'page') {
    pageForm.parent_page = rightClickedNode.value.id
  }
}

// 编辑节点
const editNode = async () => {
  console.log('Edit node clicked, rightClickedNode:', rightClickedNode.value)
  showContextMenu.value = false

  if (!rightClickedNode.value) {
    console.log('No right clicked node')
    return
  }

  console.log('Editing node:', rightClickedNode.value)
  console.log('Node type:', rightClickedNode.value.type)

  // 禁止编辑"未关联页面"节点
  if (rightClickedNode.value.id === 'unassigned') {
    ElMessage.warning('未关联页面节点不能编辑')
    return
  }

  if (rightClickedNode.value.type === 'page') {
    // 编辑页面
    console.log('Editing page node')
    editPageForm.id = rightClickedNode.value.id
    editPageForm.name = rightClickedNode.value.name
    editPageForm.description = rightClickedNode.value.description || ''
    editPageForm.parent_page = rightClickedNode.value.parent_group || null
    console.log('Set edit page form data:', editPageForm)
    console.log('Setting showEditPageDialog to true')
    showEditPageDialog.value = true
    console.log('showEditPageDialog value:', showEditPageDialog.value)
  } else if (rightClickedNode.value.type === 'element') {
    console.log('Editing element node')
    // 编辑元素 - 通过API获取完整的元素详情，避免使用树节点的复杂数据
    try {
      const response = await getElementDetail(rightClickedNode.value.id)
      selectedElement.value = response.data
      console.log('Set selected element for editing via API:', selectedElement.value)

      // 强制刷新表单，确保下拉框正确显示
      formKey.value += 1
      console.log('编辑时formKey更新为:', formKey.value)
    } catch (error) {
      console.error('获取元素详情失败:', error)
      ElMessage.error(t('uiAutomation.element.messages.getDetailFailed'))
    }
  } else {
    console.log('Unknown node type:', rightClickedNode.value.type)
  }
}

// 删除节点
const deleteNode = async () => {
  console.log('Delete node clicked, rightClickedNode:', rightClickedNode.value)
  showContextMenu.value = false

  if (!rightClickedNode.value) return

  // 禁止删除"未关联页面"节点
  if (rightClickedNode.value.id === 'unassigned') {
    ElMessage.warning('未关联页面节点不能删除')
    return
  }

  try {
    await ElMessageBox.confirm(
      t('uiAutomation.element.messages.confirmDeleteNode', { name: rightClickedNode.value.name }),
      t('uiAutomation.common.confirmDelete'),
      {
        type: 'warning',
        confirmButtonText: t('uiAutomation.common.confirm'),
        cancelButtonText: t('uiAutomation.common.cancel')
      }
    )

    console.log('Deleting node:', rightClickedNode.value)

    if (rightClickedNode.value.type === 'page') {
      // 删除页面（分组）
      console.log('Calling deleteElementGroup with id:', rightClickedNode.value.id)
      await deleteElementGroup(rightClickedNode.value.id)
      ElMessage.success(t('uiAutomation.element.messages.pageDeleteSuccess'))
    } else if (rightClickedNode.value.type === 'element') {
      // 删除元素
      console.log('Calling deleteElement with id:', rightClickedNode.value.id)
      await deleteElement(rightClickedNode.value.id)
      ElMessage.success(t('uiAutomation.element.messages.deleteSuccess'))
      // 如果当前选中的是被删除的元素，清空选中
      if (selectedElement.value && selectedElement.value.id === rightClickedNode.value.id) {
        selectedElement.value = null
      }
    }

    console.log('Reload data after deletion')

    // 重新加载数据
    await Promise.all([
      loadPages(),
      loadElementTree()
    ])

    // 强制刷新树组件
    treeKey.value += 1
  } catch (error) {
    if (error !== 'cancel') {
      console.error('删除失败:', error)
      ElMessage.error(t('uiAutomation.element.messages.deleteFailed'))
    }
  }
}

// 更新页面
const updatePage = async () => {
  console.log('Update page function called')
  console.log('Edit page form ref:', editPageFormRef.value)

  if (!editPageFormRef.value) {
    console.log('No edit page form ref')
    return
  }

  const validate = await editPageFormRef.value.validate()
  console.log('Validation result:', validate)
  if (!validate) {
    console.log('Validation failed')
    return
  }

  console.log('Updating page with data:', editPageForm)

  try {
    // 构建更新页面的参数，正确处理父页面参数
    const pageData = {
      name: editPageForm.name,
      description: editPageForm.description,
      project: selectedProject.value
    }

    // 只有当父页面ID存在且不为空时才添加parent_group字段
    // 如果父页面ID为null，表示取消父页面关联
    if (editPageForm.parent_page !== undefined) {
      pageData.parent_group = editPageForm.parent_page
    }

    await updateElementGroup(editPageForm.id, pageData)

    ElMessage.success(t('uiAutomation.element.messages.pageUpdateSuccess'))
    showEditPageDialog.value = false

    // 重新加载页面和树
    await Promise.all([
      loadPages(),
      loadElementTree()
    ])

    // 强制刷新树组件
    treeKey.value += 1
  } catch (error) {
    console.error('更新页面失败:', error)
    ElMessage.error(t('uiAutomation.element.messages.pageUpdateFailed'))
  }
}
</script>

<style scoped>
.element-manager {
  height: 100vh;
  display: flex;
  flex-direction: column;
}

.element-setup-guide {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  padding: 10px 14px;
  background: var(--el-color-primary-light-9);
  color: var(--el-text-color-regular);
}

.element-setup-guide p {
  margin: 0;
}

.element-layout {
  display: flex;
  flex: 1;
  overflow: hidden;
}

.sidebar {
  width: 300px;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}

.sidebar-header {
  padding: 15px;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  gap: 10px;
}

.header-actions {
  display: flex;
  gap: 5px;
  margin-left: auto;
}

.page-tree {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
}

.tree-node {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 5px 0;
}

.node-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.element-type-tag {
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 4px;
  background-color: #ecf5ff;
  color: #409eff;
}

.main-content {
  flex: 1;
  overflow: auto;
  padding: 20px;
}

.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.element-header {
  margin-bottom: 20px;
  padding-bottom: 20px;
  border-bottom: 1px solid #e4e7ed;
}

.element-info {
  display: flex;
  align-items: center;
  gap: 10px;
}

.element-form {
  margin-top: 20px;
}

.form-help-text {
  font-size: 12px;
  color: #909399;
  margin-top: 5px;
}

.locator-validation-controls {
  display: grid;
  justify-items: start;
  gap: 8px;
  max-width: 560px;
  margin-top: 12px;
}

.validation-mode-note,
.validation-live-help {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.locator-validation-result {
  display: grid;
  gap: 6px;
  width: 100%;
  padding: 10px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
}

.locator-validation-result > p {
  margin: 0;
  overflow-wrap: anywhere;
}

.validation-result-heading,
.validation-result-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.validation-result-meta {
  justify-content: space-between;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

/* 右键菜单样式 */
.context-menu {
  position: fixed;
  z-index: 9999;
  background: white;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
  padding: 5px 0;
  margin: 0;
  list-style: none;
  min-width: 120px;
}

.context-menu li {
  padding: 8px 15px;
  cursor: pointer;
  font-size: 14px;
  color: #606266;
}

.context-menu li:hover {
  background-color: #f5f7fa;
  color: #409eff;
}
</style>