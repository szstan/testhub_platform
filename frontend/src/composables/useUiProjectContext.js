import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getUiProjects } from '@/api/ui_automation'
import { useUserStore } from '@/stores/user'
import { loadUiProjectOptions, normalizeUiId, selectUiProject } from '@/utils/ui-project-context.mjs'

function projectPreference(userId, projectId) {
  if (!userId) return undefined
  const key = `testhub:ui-project:${userId}`
  try {
    if (projectId) globalThis.localStorage?.setItem(key, String(projectId))
    else return globalThis.localStorage?.getItem(key)
  } catch {
    // The route still carries the selection when browser storage is unavailable.
  }
}

export function useUiProjectContext() {
  const route = useRoute()
  const router = useRouter()
  const user = useUserStore()
  const page = route.name
  const userId = computed(() => user.user?.id)
  const projects = ref([])
  const projectsLoading = ref(true)
  const projectsError = ref(false)
  const ready = ref(false)
  let generation = 0

  const selection = computed(() => {
    if (!ready.value || route.name !== page) return { id: '', invalid: false }
    return selectUiProject(projects.value, route.query.project, projectPreference(userId.value))
  })
  const invalidProject = computed(() => selection.value.invalid)
  const projectId = computed({
    get: () => selection.value.id,
    set(value) {
      const next = normalizeUiId(value)
      if (!next || next === selection.value.id || route.name !== page) return
      if (!projects.value.some(project => project.id === next)) return
      const query = { ...route.query, project: String(next) }
      delete query.executionId
      delete query.caseId
      delete query.setup
      void router.push({ name: page, query, hash: route.hash })
    },
  })

  async function reloadProjects() {
    const request = ++generation
    const viewer = userId.value
    projects.value = []
    projectsLoading.value = true
    projectsError.value = false
    ready.value = false
    try {
      const options = await loadUiProjectOptions(params => {
        if (request !== generation || userId.value !== viewer || route.name !== page) {
          throw new Error('UI project context changed')
        }
        return getUiProjects(params)
      })
      if (request !== generation || userId.value !== viewer || route.name !== page) return
      projects.value = options
      ready.value = true
    } catch {
      if (request === generation && userId.value === viewer && route.name === page) projectsError.value = true
    } finally {
      if (request === generation) projectsLoading.value = false
    }
  }

  watch([projectId, userId, () => route.query.project], ([id, viewer, explicit]) => {
    if (!id || route.name !== page) return
    projectPreference(viewer, id)
    if (explicit === undefined) {
      void router.replace({ name: page, query: { ...route.query, project: String(id) }, hash: route.hash })
    }
  })
  watch(userId, reloadProjects, { flush: 'sync' })
  onMounted(reloadProjects)
  onScopeDispose(() => { generation += 1 })

  return { projects, projectId, projectsLoading, projectsError, invalidProject, reloadProjects }
}
