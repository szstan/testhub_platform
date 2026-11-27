import request from '../utils/api'

export function recoverPendingDeviceAction(id, expectedUserId) {
  return request({
    url: `/app-automation/devices/${id}/recover-pending-action/`,
    method: 'post',
    data: { confirm_no_active_worker: true, expected_user_id: expectedUserId }
  })
}

export function clearDeviceMaintenanceMode(id, expectedUserId) {
  return request({
    url: `/app-automation/devices/${id}/`,
    method: 'patch',
    data: { maintenance_mode: false, expected_user_id: expectedUserId }
  })
}
