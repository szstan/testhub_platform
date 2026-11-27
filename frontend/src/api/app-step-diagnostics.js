import request from '../utils/api'

export function runSavedSceneStep(caseId, data) {
  return request({ url: `/app-automation/test-cases/${caseId}/run-step/`, method: 'post', data })
}

export function getFlowFailureScreenshot(executionId) {
  return request({
    url: `/app-automation/executions/${executionId}/flow-failure-evidence/`,
    method: 'get',
    responseType: 'blob',
  })
}
