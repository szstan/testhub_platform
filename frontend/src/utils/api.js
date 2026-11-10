import axios from 'axios'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
  withCredentials: true,
  xsrfCookieName: 'csrftoken',
  xsrfHeaderName: 'X-CSRFToken',
  headers: { 'Content-Type': 'application/json' },
})

// These endpoints must settle independently of a pending session refresh.
const sessionEndpoints = new Set([
  '/auth/login/', '/auth/sms-login/', '/auth/register/', '/auth/test-register/',
  '/auth/token/refresh/', '/auth/logout/', '/auth/exchange-token/',
  '/auth/captcha/', '/auth/send-register-code/',
])
const isSessionRequest = (config) => {
  const path = new URL(config.url || '', 'http://testhub.local').pathname.replace(/^\/api\//, '/')
  return sessionEndpoints.has(path)
}

api.interceptors.request.use(async (config) => {
  const userStore = useUserStore()
  if (!isSessionRequest(config) && !config._retry && userStore.accessToken && userStore.isTokenExpiringSoon()) {
    await userStore.refreshAccessToken()
  }
  // Read the token after awaiting refresh: concurrent callers share the store promise.
  if (userStore.accessToken) config.headers.Authorization = `Bearer ${userStore.accessToken}`
  config._sessionGeneration = userStore.getSessionGeneration()
  return config
})

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config
    const userStore = useUserStore()
    if (error.response?.status === 401 && originalRequest && !isSessionRequest(originalRequest)) {
      // A response from before logout/account switching cannot invalidate the new session.
      if (originalRequest._sessionGeneration !== userStore.getSessionGeneration()) return Promise.reject(error)
      if (!originalRequest._retry && userStore.accessToken) {
        originalRequest._retry = true
        const rejectedToken = originalRequest.headers?.Authorization
        // A late sibling 401 may arrive after the shared refresh already completed.
        // In that case retry with the new token instead of rotating the cookie again.
        if (rejectedToken === `Bearer ${userStore.accessToken}`) {
          await userStore.refreshAccessToken()
        }
        originalRequest.headers.Authorization = `Bearer ${userStore.accessToken}`
        return api(originalRequest)
      }
      userStore.clearSession({ notify: true })
    } else if (error.response?.status >= 500) {
      ElMessage.error('服务器错误，请稍后重试')
    }
    // Auth endpoint failures go directly to their caller, without logout or reload.
    return Promise.reject(error)
  }
)

export default api
