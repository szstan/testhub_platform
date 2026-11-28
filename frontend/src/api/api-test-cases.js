import request from '@/utils/api'

const collectionPath = '/api-testing/test-cases/'

export function listApiTestCases(params = {}) {
  return request({ url: collectionPath, method: 'get', params })
}

export function getApiTestCase(id) {
  return request({ url: `${collectionPath}${id}/`, method: 'get' })
}

export function createApiTestCase(data) {
  return request({ url: collectionPath, method: 'post', data })
}

export function updateApiTestCase(id, data) {
  return request({ url: `${collectionPath}${id}/`, method: 'patch', data })
}

export function deleteApiTestCase(id) {
  return request({ url: `${collectionPath}${id}/`, method: 'delete' })
}

export function duplicateApiTestCase(id) {
  return request({ url: `${collectionPath}${id}/duplicate/`, method: 'post' })
}

export function executeApiTestCase(id, data) {
  return request({ url: `${collectionPath}${id}/execute/`, method: 'post', data })
}

export function getApiCaseExecution(id) {
  return request({ url: `/api-testing/case-executions/${id}/`, method: 'get' })
}

export function getApiTestCaseExecutions(id, params = {}) {
  return request({ url: `${collectionPath}${id}/executions/`, method: 'get', params })
}
