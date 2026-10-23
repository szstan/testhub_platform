import request from '@/utils/api'

export const listTestCaseSyncProfiles = (params = {}) => request.get('/testcases/sync-profiles/', { params })
export const createTestCaseSyncProfile = (data) => request.post('/testcases/sync-profiles/', data)
export const updateTestCaseSyncProfile = (id, data) => request.patch(`/testcases/sync-profiles/${id}/`, data)
export const deleteTestCaseSyncProfile = (id) => request.delete(`/testcases/sync-profiles/${id}/`)
export const syncTestCaseSyncProfile = (id) => request.post(`/testcases/sync-profiles/${id}/sync/`)
