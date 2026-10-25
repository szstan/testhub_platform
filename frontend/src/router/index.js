import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { trackPageView } from '@/utils/tracker'
import api from '@/utils/api'

// Keep the shell and initial auth/home views eager; feature pages are lazy-loaded.
const Login = () => import('@/views/auth/Login.vue')
const Register = () => import('@/views/auth/Register.vue')
const Layout = () => import('@/layout/index.vue')
const ProjectList = () => import('@/views/projects/ProjectList.vue')
const Home = () => import('@/views/Home.vue')

const QUALITY_INTELLIGENCE_ENABLED = String(import.meta.env.VITE_QUALITY_INTELLIGENCE_ENABLED || '').toLowerCase() === 'true'

/** @type {import('vue-router').RouteRecordRaw[]} */
const routes = [
    {
        path: '/',
        redirect: to => ({ path: '/home', query: to.query })
    },
    {
        path: '/home',
        name: 'Home',
        component: Home,
        meta: { requiresAuth: true }
    },
    {
        path: '/login',
        name: 'Login',
        component: Login,
        meta: { requiresGuest: true }
    },
    {
        path: '/register',
        name: 'Register',
        component: Register,
        meta: { requiresGuest: true }
    },
    {
        path: '/ai-generation/assistant',
        name: 'Assistant',
        component: () => import('@/views/assistant/AssistantView.vue'),
        meta: { requiresAuth: true }
    },
    {
        path: '/ai-generation',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'requirement-analysis'
            },
            {
                path: 'requirement-analysis',
                name: 'RequirementAnalysis',
                component: () => import('@/views/requirement-analysis/RequirementAnalysisView.vue')
            },
            {
                path: 'projects',
                name: 'Projects',
                component: ProjectList
            },
            {
                path: 'projects/:id',
                name: 'ProjectDetail',
                component: () => import('@/views/projects/ProjectDetail.vue')
            },
            {
                path: 'testcases',
                name: 'TestCases',
                component: () => import('@/views/testcases/TestCaseList.vue')
            },
            {
                path: 'testcases/sync',
                name: 'TestCaseSync',
                component: () => import('@/views/testcases/TestCaseSync.vue')
            },
            {
                path: 'testcases/import-records',
                name: 'TestCaseImportRecords',
                component: () => import('@/views/testcases/TestCaseImportRecordList.vue')
            },
            {
                path: 'testcases/create',
                name: 'CreateTestCase',
                component: () => import('@/views/testcases/TestCaseForm.vue')
            },
            {
                path: 'testcases/:id',
                name: 'TestCaseDetail',
                component: () => import('@/views/testcases/TestCaseDetail.vue')
            },
            {
                path: 'testcases/:id/edit',
                name: 'EditTestCase',
                component: () => import('@/views/testcases/TestCaseEdit.vue')
            },
            {
                path: 'versions',
                name: 'Versions',
                component: () => import('@/views/versions/VersionList.vue')
            },
            {
                path: 'reviews',
                name: 'Reviews',
                component: () => import('@/views/reviews/ReviewList.vue')
            },
            {
                path: 'reviews/create',
                name: 'CreateReview',
                component: () => import('@/views/reviews/ReviewForm.vue')
            },
            {
                path: 'reviews/:id',
                name: 'ReviewDetail',
                component: () => import('@/views/reviews/ReviewDetail.vue')
            },
            {
                path: 'reviews/:id/edit',
                name: 'EditReview',
                component: () => import('@/views/reviews/ReviewForm.vue')
            },
            {
                path: 'review-templates',
                name: 'ReviewTemplates',
                component: () => import('@/views/reviews/ReviewTemplateList.vue')
            },
            {
                path: 'testsuites',
                name: 'TestSuites',
                component: () => import('@/views/testsuites/TestSuiteList.vue')
            },
            {
                path: 'executions',
                name: 'Executions',
                component: () => import('@/views/executions/ExecutionListView.vue')
            },
            {
                path: 'task-center',
                name: 'UnifiedTaskCenter',
                component: () => import('@/views/executions/TaskCenter.vue')
            },
            {
                path: 'failure-review',
                name: 'ExecutionFailureReview',
                component: () => import('@/views/executions/FailureReview.vue')
            },
            {
                path: 'quality-loop',
                name: 'ExecutionQualityLoop',
                component: () => import('@/views/executions/QualityLoop.vue')
            },
            {
                path: 'traceability',
                name: 'ExecutionTraceability',
                component: () => import('@/views/executions/TraceabilityView.vue')
            },
            {
                path: 'ci-profiles',
                name: 'CIExecutionProfiles',
                component: () => import('@/views/executions/CIProfiles.vue')
            },
            {
                path: 'datasets-plugins',
                name: 'ExecutionDatasetsPlugins',
                component: () => import('@/views/executions/DatasetsPlugins.vue')
            },
            {
                path: 'executions/:id',
                name: 'ExecutionDetail',
                component: () => import('@/views/executions/ExecutionDetailView.vue')
            },
            {
                path: 'reports',
                name: 'AiTestReport',
                component: () => import('@/views/reports/AiTestReport.vue')
            },
            {
                path: 'generated-testcases',
                name: 'GeneratedTestCases',
                component: () => import('@/views/requirement-analysis/GeneratedTestCaseList.vue')
            },
            {
                path: 'task-detail/:taskId',
                name: 'TaskDetail',
                component: () => import('@/views/requirement-analysis/TaskDetail.vue')
            },
            {
                path: 'profile',
                name: 'Profile',
                component: () => import('@/views/profile/UserProfile.vue')
            }
        ]
    },
    {
        path: '/api-testing',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'dashboard'
            },
            {
                path: 'dashboard',
                name: 'ApiDashboard',
                component: () => import('@/views/api-testing/Dashboard.vue')
            },
            {
                path: 'projects',
                name: 'ApiProjects',
                component: () => import('@/views/api-testing/ProjectManagement.vue')
            },
            {
                path: 'interfaces',
                name: 'ApiInterfaces',
                component: () => import('@/views/api-testing/InterfaceManagement.vue')
            },
            {
                path: 'automation',
                name: 'ApiAutomation',
                component: () => import('@/views/api-testing/AutomationTesting.vue')
            },
            {
                path: 'history',
                name: 'ApiHistory',
                component: () => import('@/views/api-testing/RequestHistory.vue')
            },
            {
                path: 'environments',
                name: 'ApiEnvironments',
                component: () => import('@/views/api-testing/EnvironmentManagement.vue')
            },
            {
                path: 'reports',
                name: 'ApiReports',
                component: () => import('@/views/api-testing/ReportView.vue')
            },
            {
                path: 'scheduled-tasks',
                name: 'ApiScheduledTasks',
                component: () => import('@/views/api-testing/ScheduledTasks.vue')
            },
            {
                path: 'ai-service-config',
                name: 'ApiAIServiceConfig',
                component: () => import('@/views/api-testing/AIServiceConfig.vue')
            },
            {
                path: 'notification-logs',
                name: 'ApiNotificationLogs',
                component: () => import('@/views/notification/NotificationLogs.vue')
            }
        ]
    },
    {
        path: '/ui-automation',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'dashboard'
            },
            {
                path: 'dashboard',
                name: 'UiDashboard',
                component: () => import('@/views/ui-automation/dashboard/Dashboard.vue')
            },
            {
                path: 'projects',
                name: 'UiProjects',
                component: () => import('@/views/ui-automation/projects/ProjectList.vue')
            },
            {
                path: 'elements-enhanced',
                name: 'UiElementsEnhanced',
                component: () => import('@/views/ui-automation/elements/ElementManagerEnhanced.vue')
            },
            {
                path: 'test-cases',
                name: 'UiTestCases',
                component: () => import('@/views/ui-automation/test-cases/TestCaseManager.vue')
            },
            {
                path: 'scripts-enhanced',
                name: 'UiScriptsEnhanced',
                component: () => import('@/views/ui-automation/scripts/ScriptEditorEnhanced.vue')
            },
            {
                path: 'scripts/editor',
                name: 'UiScriptEditor',
                component: () => import('@/views/ui-automation/scripts/ScriptEditorEnhanced.vue')
            },
            {
                path: 'scripts',
                name: 'UiScripts',
                component: () => import('@/views/ui-automation/scripts/ScriptList.vue')
            },
            {
                path: 'suites',
                name: 'UiSuites',
                component: () => import('@/views/ui-automation/suites/SuiteList.vue')
            },
            {
                path: 'executions',
                name: 'UiExecutions',
                component: () => import('@/views/ui-automation/executions/ExecutionList.vue')
            },
            {
                path: 'reports',
                name: 'UiReports',
                component: () => import('@/views/ui-automation/reports/ReportList.vue')
            },
            {
                path: 'scheduled-tasks',
                name: 'UiScheduledTasks',
                component: () => import('@/views/ui-automation/scheduled-tasks/ScheduledTasks.vue')
            },
            {
                path: 'notification-logs',
                name: 'UiNotificationLogs',
                component: () => import('@/views/ui-automation/notification/NotificationLogs.vue')
            }
        ]
    },
    {
        path: '/ai-intelligent-mode',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'testing'
            },
            {
                path: 'testing',
                name: 'AITesting',
                component: () => import('@/views/ui-automation/ai/AITesting.vue')
            },
            {
                path: 'cases',
                name: 'AICaseList',
                component: () => import('@/views/ui-automation/ai/AICaseList.vue')
            },
            {
                path: 'execution-records',
                name: 'AIExecutionRecords',
                component: () => import('@/views/ui-automation/ai/AIExecutionRecords.vue')
            }
        ]
    },
    {
        path: '/defects',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'dashboard'
            },
            {
                path: 'dashboard',
                name: 'DefectDashboard',
                component: () => import('@/views/defects/DefectDashboard.vue')
            },
            {
                path: 'list',
                name: 'DefectList',
                component: () => import('@/views/defects/DefectList.vue')
            },
            {
                path: 'create',
                name: 'DefectCreate',
                component: () => import('@/views/defects/DefectForm.vue')
            },
            {
                path: 'reports',
                name: 'DefectReport',
                component: () => import('@/views/defects/DefectReport.vue')
            },
            {
                path: ':id',
                name: 'DefectDetail',
                component: () => import('@/views/defects/DefectDetail.vue')
            },
            {
                path: ':id/edit',
                name: 'DefectEdit',
                component: () => import('@/views/defects/DefectForm.vue')
            }
        ]
    },
    {
        path: '/data-factory',
        name: 'DataFactory',
        component: () => import('@/views/data-factory/DataFactory.vue'),
        meta: { requiresAuth: true }
    },
    {
        path: '/configuration',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                component: () => import('@/views/configuration/ConfigurationCenter.vue'),
                children: [
                    {
                        path: '',
                        redirect: 'ai-model'
                    },
                    {
                        path: 'ai-model',
                        name: 'ConfigAIModel',
                        component: () => import('@/views/requirement-analysis/AIModelConfig.vue')
                    },
                    {
                        path: 'prompt-config',
                        name: 'ConfigPromptConfig',
                        component: () => import('@/views/requirement-analysis/PromptConfig.vue')
                    },
                    {
                        path: 'generation-config',
                        name: 'ConfigGenerationConfig',
                        component: () => import('@/views/requirement-analysis/GenerationConfigView.vue'),
                        meta: { requiresAdmin: true }
                    },
                    {
                        path: 'ui-env',
                        name: 'ConfigUIEnv',
                        component: () => import('@/views/configuration/UIEnvironmentConfig.vue')
                    },
                    {
                        path: 'app-env',
                        name: 'ConfigAppEnv',
                        component: () => import('@/views/app-automation/settings/AppSettings.vue')
                    },
                    {
                        path: 'ai-mode',
                        name: 'ConfigAIMode',
                        component: () => import('@/views/configuration/AIIntelligentModeConfig.vue')
                    },
                    {
                        path: 'scheduled-task',
                        name: 'ConfigScheduledTask',
                        component: () => import('@/views/ui-automation/notification/NotificationConfigs.vue')
                    },
                    {
                        path: 'dify',
                        name: 'DifyConfig',
                        component: () => import('@/views/configuration/DifyConfig.vue')
                    }
                ]
            }
        ]
    },
    // 智能评分器路由
    {
        path: '/llm-judge',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'dashboard'
            },
            {
                path: 'dashboard',
                name: 'JudgeDashboard',
                component: () => import('@/views/llm-judge/Dashboard.vue')
            },
            {
                path: 'single',
                name: 'JudgeSingle',
                component: () => import('@/views/llm-judge/SingleJudge.vue')
            },
            {
                path: 'batch',
                name: 'JudgeBatch',
                component: () => import('@/views/llm-judge/BatchJudge.vue')
            },
            {
                path: 'history',
                name: 'JudgeHistory',
                component: () => import('@/views/llm-judge/HistoryList.vue')
            },
            {
                path: 'rubrics',
                name: 'JudgeRubrics',
                component: () => import('@/views/llm-judge/RubricList.vue')
            },
            {
                path: 'knowledge',
                name: 'JudgeKnowledgeBase',
                component: () => import('@/views/llm-judge/KnowledgeBase.vue')

            }
        ]
    },
    // APP自动化测试路由
    {
        path: '/app-automation',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'dashboard'
            },
            {
                path: 'dashboard',
                name: 'AppAutomationDashboard',
                component: () => import('@/views/app-automation/dashboard/Dashboard.vue')
            },
            {
                path: 'projects',
                name: 'AppProjectList',
                component: () => import('@/views/app-automation/projects/ProjectList.vue')
            },
            {
                path: 'devices',
                name: 'AppDeviceList',
                component: () => import('@/views/app-automation/devices/DeviceList.vue')
            },
            {
                path: 'packages',
                name: 'AppPackageList',
                component: () => import('@/views/app-automation/packages/PackageList.vue')
            },
            {
                path: 'elements',
                name: 'AppElementList',
                component: () => import('@/views/app-automation/elements/ElementList.vue')
            },
            {
                path: 'scene-builder',
                name: 'AppSceneBuilder',
                component: () => import('@/views/app-automation/test-cases/SceneBuilder.vue'),
                meta: { title: '用例编排' }
            },
            {
                path: 'test-cases',
                name: 'AppTestCaseList',
                component: () => import('@/views/app-automation/test-cases/TestCaseList.vue')
            },
            {
                path: 'test-suites',
                name: 'AppTestSuiteList',
                component: () => import('@/views/app-automation/suites/SuiteList.vue')
            },
            {
                path: 'scheduled-tasks',
                name: 'AppScheduledTasks',
                component: () => import('@/views/app-automation/scheduled-tasks/ScheduledTasks.vue')
            },
            {
                path: 'notification-logs',
                name: 'AppNotificationLogs',
                component: () => import('@/views/app-automation/notification/NotificationLogs.vue')
            },
            {
                path: 'executions',
                name: 'AppExecutionList',
                component: () => import('@/views/app-automation/executions/ExecutionList.vue')
            },
            {
                path: 'reports',
                name: 'AppReportList',
                component: () => import('@/views/app-automation/reports/ReportList.vue')
            }
        ]
    },
    {
        path: '/monitor',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'dashboard'
            },
            {
                path: 'dashboard',
                name: 'MonitorDashboard',
                component: () => import('@/views/monitor/Dashboard.vue')
            },
            {
                path: 'checks',
                name: 'MonitorCheckLogs',
                component: () => import('@/views/monitor/CheckLogs.vue')
            },
            {
                path: 'alerts',
                name: 'MonitorAlerts',
                component: () => import('@/views/monitor/Alerts.vue')
            },
            {
                path: 'targets',
                name: 'MonitorTargets',
                component: () => import('@/views/monitor/MonitorTargets.vue')
            },
            {
                path: 'channels',
                name: 'NotificationChannels',
                component: () => import('@/views/monitor/NotificationChannels.vue')
            }
        ]
    },
    {
        path: '/performance-testing',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'dashboard'
            },
            {
                path: 'dashboard',
                name: 'PerfDashboard',
                component: () => import('@/views/performance-testing/Dashboard.vue')
            },
            {
                path: 'projects',
                name: 'PerfProjects',
                component: () => import('@/views/performance-testing/ProjectManagement.vue')
            },
            {
                path: 'scenarios',
                name: 'PerfScenarios',
                component: () => import('@/views/performance-testing/ScenarioList.vue')
            },
            {
                path: 'scenarios/:id',
                name: 'PerfScenarioEditor',
                component: () => import('@/views/performance-testing/ScenarioEditor.vue')
            },
            {
                path: 'executions',
                name: 'PerfExecutions',
                component: () => import('@/views/performance-testing/ExecutionList.vue')
            },
            {
                path: 'executions/:id',
                name: 'PerfExecutionReport',
                component: () => import('@/views/performance-testing/ExecutionReport.vue')
            },
            {
                path: 'executions/:id/monitor',
                name: 'PerfExecutionMonitor',
                component: () => import('@/views/performance-testing/ExecutionMonitor.vue')
            },
            {
                path: 'comparison',
                name: 'PerfComparison',
                component: () => import('@/views/performance-testing/Comparison.vue')
            },
            {
                path: 'comparison-reports',
                name: 'PerfComparisonReports',
                component: () => import('@/views/performance-testing/ComparisonReports.vue')
            },
            {
                path: 'scheduled',
                name: 'PerfScheduled',
                component: () => import('@/views/performance-testing/ScheduledTasks.vue')
            }
        ]
    },
    {
        path: '/quality-intelligence',
        component: Layout,
        meta: { requiresAuth: true, featureFlag: 'quality-intelligence' },
        children: [
            {
                path: '',
                redirect: 'dashboard'
            },
            {
                path: 'dashboard',
                name: 'QualityDashboard',
                component: () => import('@/views/quality-intelligence/QualityDashboard.vue')
            },
            {
                path: 'runs',
                name: 'QualityRuns',
                component: () => import('@/views/quality-intelligence/QualityRunList.vue')
            },
            {
                path: 'runs/:id',
                name: 'QualityRunDetail',
                component: () => import('@/views/quality-intelligence/QualityRunDetail.vue')
            },
            {
                path: 'failures',
                name: 'QualityFailures',
                component: () => import('@/views/quality-intelligence/FailureExplorer.vue')
            },
            {
                path: 'profiles',
                name: 'QualityProfiles',
                component: () => import('@/views/quality-intelligence/ProfileQuarantine.vue')
            },
            {
                path: 'baselines',
                name: 'QualityBaselines',
                component: () => import('@/views/quality-intelligence/BaselineView.vue')
            }
        ]
    },
    {
        path: '/mcp',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                redirect: 'console'
            },
            {
                path: 'console',
                name: 'McpConsole',
                component: () => import('@/views/mcp/McpConsole.vue')
            }
        ]
    },
    {
        path: '/docs-center',
        component: Layout,
        meta: { requiresAuth: true },
        children: [
            {
                path: '',
                name: 'DocsCenter',
                component: () => import('@/views/docs-center/DocsCenter.vue')
            }
        ]
    }
]

const router = createRouter({
    history: createWebHistory(),
    routes
})

router.beforeEach(async (to, _from, next) => {
    const userStore = useUserStore()

    // SSO 授权码交换：在登录态检查之前，先把 URL 中的 code 换成 JWT
    const code = to.query.code
    if (code && !userStore.accessToken) {
        try {

            const response = await api.post('/auth/exchange-token/', {
                action: 'redeem',
                code: code,
            })
            const expiresAt = Date.now() + 30 * 60 * 1000
            userStore.accessToken = response.data.access
            userStore.refreshToken = ''
            userStore.tokenExpiresAt = expiresAt
            userStore.user = response.data.user

            // 清除 URL 中的 code，使用 replace 避免产生多余历史记录
            const query = { ...to.query }
            delete query.code
            next({ path: to.path, query, replace: true })
            return
        } catch {
            // code 无效或已过期，继续正常流程
        }
    }

    // 有内存 token 或受保护路由时初始化认证；刷新 cookie 由 store 负责恢复 access token
    if (to.meta.requiresAuth && !userStore.user && !userStore.accessToken) {
        try {
            await userStore.initAuth()
        } catch (error) {
            console.error('认证初始化失败:', error)
        }
    } else if (!userStore.user && userStore.accessToken) {
        try {
            await userStore.initAuth()
        } catch (error) {
            console.error('认证初始化失败:', error)
        }
    }

    if (to.meta.featureFlag === 'quality-intelligence' && !QUALITY_INTELLIGENCE_ENABLED) {
        next('/home')
    } else if (to.meta.requiresAuth && !userStore.isAuthenticated) {
        next('/login')
    } else if (to.meta.requiresGuest && userStore.isAuthenticated) {
        next('/home')
    } else if (to.meta.requiresAdmin && !userStore.isAdmin) {
        next({ name: 'Home', query: { forbidden: 'generation-config' } })
    } else {
        next()
    }
})

router.afterEach((to, from) => {
    trackPageView(to, from)
})

export default router
