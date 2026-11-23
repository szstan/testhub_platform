import request from '../utils/api'

export function runSavedSceneStep(caseId, data) {
  return request({ url: `/app-automation/test-cases/${caseId}/run-step/`, method: 'post', data })
}
