import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/utils/api'
import { track } from '@/utils/tracker'

const removeLegacySession = () => {
  for (const key of ['access_token', 'refresh_token', 'token_expires_at', 'user']) {
    localStorage.removeItem(key)
  }
}

const accessTokenExpiry = (token) => {
  const payload = token?.split('.')[1]
  try {
    const { exp } = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')))
    if (typeof exp === 'number' && Number.isFinite(exp) && exp > 0) return exp * 1000
  } catch {
    // A malformed token must never acquire a fabricated lifetime.
  }
  throw new Error('Invalid access token expiry')
}

export const useUserStore = defineStore('user', () => {
  const user = ref(null)
  // The refresh token remains in its HttpOnly cookie; never persist either JWT.
  const accessToken = ref('')
  const refreshToken = ref('')
  const tokenExpiresAt = ref(0)
  const authInitialized = ref(false)
  let sessionGeneration = 0
  let refreshTimer = null
  let refreshPromise = null
  let initPromise = null
  let logoutPromise = null

  const isAuthenticated = computed(() => !!accessToken.value && !!user.value)
  const isAdmin = computed(() => Boolean(
    user.value?.is_admin || user.value?.is_staff || user.value?.is_superuser
  ))
  // Use methods: a computed that only depends on expiresAt caches Date.now().
  const isTokenExpiringSoon = () => Boolean(
    tokenExpiresAt.value && tokenExpiresAt.value - Date.now() < 5 * 60 * 1000
  )
  const isTokenExpired = () => Boolean(
    tokenExpiresAt.value && Date.now() >= tokenExpiresAt.value
  )

  const stopAutoRefresh = () => {
    if (refreshTimer !== null) clearInterval(refreshTimer)
    refreshTimer = null
  }

  const startAutoRefresh = () => {
    stopAutoRefresh()
    refreshTimer = setInterval(() => {
      if (accessToken.value && isTokenExpiringSoon()) {
        // Failure clears the session and timer; there is no recursive logout.
        refreshAccessToken().catch(() => {})
      }
    }, 60 * 1000)
  }

  const clearSession = ({ notify = false } = {}) => {
    const hadSession = Boolean(accessToken.value || user.value)
    sessionGeneration += 1
    refreshPromise = null
    stopAutoRefresh()
    accessToken.value = ''
    refreshToken.value = ''
    user.value = null
    tokenExpiresAt.value = 0
    authInitialized.value = true
    removeLegacySession()
    if (notify && hadSession) window.dispatchEvent(new Event('testhub:session-expired'))
  }

  const applySession = (data) => {
    const expiresAt = accessTokenExpiry(data.access)
    sessionGeneration += 1
    refreshPromise = null
    accessToken.value = data.access
    user.value = data.user
    tokenExpiresAt.value = expiresAt
    authInitialized.value = true
    removeLegacySession()
    startAutoRefresh()
  }

  const login = async (credentials) => {
    if (logoutPromise) await logoutPromise
    const response = await api.post('/auth/login/', credentials)
    applySession(response.data)
    track('login_success', {
      event_type: 'business', module: 'auth', page_path: '/login', success: true,
      metadata: { login_type: 'password' }
    })
    return response.data
  }

  const smsLogin = async (data) => {
    if (logoutPromise) await logoutPromise
    const response = await api.post('/auth/sms-login/', data)
    applySession(response.data)
    track('login_success', {
      event_type: 'business', module: 'auth', page_path: '/login', success: true,
      metadata: { login_type: 'sms' }
    })
    return response.data
  }

  const register = async (userData) => {
    if (logoutPromise) await logoutPromise
    const response = await api.post('/auth/test-register/', userData)
    applySession(response.data)
    track('register_success', {
      event_type: 'business', module: 'auth', page_path: '/register', success: true
    })
    return response.data
  }

  const logout = () => {
    if (logoutPromise) return logoutPromise
    const inFlightRefresh = refreshPromise
    clearSession()
    logoutPromise = (async () => {
      try {
        // Clear the cookie after any earlier rotation response has settled.
        if (inFlightRefresh) await inFlightRefresh.catch(() => {})
        await api.post('/auth/logout/', {})
      } catch {
        // Local logout is complete even if the network is unavailable.
      } finally {
        logoutPromise = null
      }
    })()
    return logoutPromise
  }

  const refreshAccessToken = () => {
    if (refreshPromise) return refreshPromise
    const generation = sessionGeneration
    const pending = (async () => {
      try {
        const response = await api.post('/auth/token/refresh/')
        if (generation !== sessionGeneration) throw new Error('Session changed during refresh')
        const expiresAt = accessTokenExpiry(response.data.access)
        accessToken.value = response.data.access
        tokenExpiresAt.value = expiresAt
        startAutoRefresh()
        return response.data.access
      } catch (error) {
        if (generation === sessionGeneration) clearSession({ notify: true })
        throw error
      } finally {
        if (refreshPromise === pending) refreshPromise = null
      }
    })()
    refreshPromise = pending
    return pending
  }

  const fetchUser = async () => {
    const generation = sessionGeneration
    const response = await api.get('/users/me/')
    if (generation === sessionGeneration) user.value = response.data
    return response.data
  }

  const fetchProfile = async () => {
    const generation = sessionGeneration
    const response = await api.get('/auth/profile/')
    if (generation === sessionGeneration) user.value = response.data
    return response.data
  }

  const initAuth = () => {
    if (initPromise) return initPromise
    if (authInitialized.value) return Promise.resolve(user.value)
    removeLegacySession()
    const generation = sessionGeneration
    const pending = (async () => {
      try {
        if (!accessToken.value || isTokenExpired()) await refreshAccessToken()
        if (generation !== sessionGeneration) return user.value
        await fetchProfile()
        if (generation === sessionGeneration) startAutoRefresh()
      } catch {
        // An old startup failure cannot erase a newer login.
        if (generation === sessionGeneration) clearSession()
      } finally {
        authInitialized.value = true
        if (initPromise === pending) initPromise = null
      }
      return user.value
    })()
    initPromise = pending
    return pending
  }

  return {
    user, accessToken, refreshToken, tokenExpiresAt, authInitialized,
    isAuthenticated, isAdmin, isTokenExpiringSoon, isTokenExpired,
    applySession, clearSession, getSessionGeneration: () => sessionGeneration,
    login, smsLogin, register, logout,
    refreshAccessToken, fetchUser, fetchProfile, initAuth,
    startAutoRefresh, stopAutoRefresh
  }
})
