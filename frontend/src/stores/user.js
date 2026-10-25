import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/utils/api'
import { track } from '@/utils/tracker'

export const useUserStore = defineStore('user', () => {
  const user = ref(null)
  // Access tokens live only in memory. The refresh token is an HttpOnly cookie.
  const accessToken = ref('')
  const refreshToken = ref('')
  const tokenExpiresAt = ref(0)

  let refreshTimer = null
  let refreshPromise = null

  const isAuthenticated = computed(() => !!accessToken.value && !!user.value)

  const isAdmin = computed(() => Boolean(
    user.value?.is_admin || user.value?.is_staff || user.value?.is_superuser
  ))

  const isTokenExpiringSoon = computed(() => {
    if (!tokenExpiresAt.value) return false
    return tokenExpiresAt.value - Date.now() < 5 * 60 * 1000
  })

  const isTokenExpired = computed(() => {
    if (!tokenExpiresAt.value) return false
    return Date.now() > tokenExpiresAt.value
  })

  const startAutoRefresh = () => {
    if (refreshTimer) clearInterval(refreshTimer)
    refreshTimer = setInterval(async () => {
      if (isTokenExpiringSoon.value && accessToken.value) {
        try {
          await refreshAccessToken()
        } catch (error) {
          console.error('自动刷新token失败:', error)
        }
      }
    }, 2 * 60 * 1000)
  }

  const stopAutoRefresh = () => {
    if (refreshTimer) {
      clearInterval(refreshTimer)
      refreshTimer = null
    }
  }

  const applyLoginResponse = (data) => {
    accessToken.value = data.access
    user.value = data.user
    tokenExpiresAt.value = Date.now() + 30 * 60 * 1000
    // Remove legacy browser-stored tokens left by older releases.
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    localStorage.removeItem('token_expires_at')
    localStorage.removeItem('user')
    startAutoRefresh()
  }

  const login = async (credentials) => {
    const response = await api.post('/auth/login/', credentials)
    applyLoginResponse(response.data)
    track('login_success', {
      event_type: 'business',
      module: 'auth',
      page_path: '/login',
      success: true,
      metadata: { login_type: 'password' }
    })
    return response.data
  }

  const smsLogin = async (data) => {
    const response = await api.post('/auth/sms-login/', data)
    applyLoginResponse(response.data)
    track('login_success', {
      event_type: 'business',
      module: 'auth',
      page_path: '/login',
      success: true,
      metadata: { login_type: 'sms' }
    })
    return response.data
  }

  const register = async (userData) => {
    const response = await api.post('/auth/test-register/', userData)
    applyLoginResponse(response.data)
    track('register_success', {
      event_type: 'business',
      module: 'auth',
      page_path: '/register',
      success: true
    })
    return response.data
  }

  let isLoggingOut = false

  const logout = async () => {
    if (isLoggingOut) return
    isLoggingOut = true
    stopAutoRefresh()

    try {
      if (accessToken.value || user.value) {
        try {
          await api.post('/auth/logout/', {})
        } catch (apiError) {
          console.error('Logout请求失败:', apiError)
        }
      }
    } finally {
      accessToken.value = ''
      refreshToken.value = ''
      user.value = null
      tokenExpiresAt.value = 0
      localStorage.removeItem('access_token')
      localStorage.removeItem('refresh_token')
      localStorage.removeItem('token_expires_at')
      localStorage.removeItem('user')
      isLoggingOut = false
      window.location.href = '/login'
    }
  }

  const refreshAccessToken = async () => {
    if (refreshPromise) return refreshPromise

    refreshPromise = (async () => {
      try {
        // The browser automatically sends the HttpOnly refresh cookie.
        const response = await api.post('/auth/token/refresh/')
        accessToken.value = response.data.access
        tokenExpiresAt.value = Date.now() + 30 * 60 * 1000
        return response.data.access
      } catch (error) {
        console.error('Token refresh failed:', error)
        accessToken.value = ''
        refreshToken.value = ''
        tokenExpiresAt.value = 0
        if (user.value) user.value = null
        throw error
      }
    })()

    try {
      return await refreshPromise
    } finally {
      refreshPromise = null
    }
  }

  const fetchUser = async () => {
    const response = await api.get('/users/me/')
    user.value = response.data
    return response.data
  }

  const fetchProfile = async () => {
    try {
      const response = await api.get('/auth/profile/')
      user.value = response.data
      return response.data
    } catch (error) {
      if (error.response?.status === 401) {
        accessToken.value = ''
        user.value = null
      }
      throw error
    }
  }

  const initAuth = async () => {
    try {
      if (!accessToken.value || isTokenExpired.value) {
        await refreshAccessToken()
      }
      await fetchProfile()
      startAutoRefresh()
    } catch {
      accessToken.value = ''
      refreshToken.value = ''
      user.value = null
      tokenExpiresAt.value = 0
      stopAutoRefresh()
    }
  }

  return {
    user,
    accessToken,
    refreshToken,
    tokenExpiresAt,
    isAuthenticated,
    isAdmin,
    isTokenExpiringSoon,
    isTokenExpired,
    login,
    smsLogin,
    register,
    logout,
    refreshAccessToken,
    fetchUser,
    fetchProfile,
    initAuth,
    startAutoRefresh,
    stopAutoRefresh
  }
})
