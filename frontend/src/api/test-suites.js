import request from '@/utils/api'

export function listTestSuites(params) {
  return request({
    url: '/testsuites/',
    method: 'get',
    params
  })
}

export function createTestSuite(data) {
  return request({
    url: '/testsuites/',
    method: 'post',
    data
  })
}

export function updateTestSuite(id, data) {
  return request({
    url: `/testsuites/${id}/`,
    method: 'patch',
    data
  })
}

export function deleteTestSuite(id) {
  return request({
    url: `/testsuites/${id}/`,
    method: 'delete'
  })
}

export function listTestSuiteCases(id) {
  return request({
    url: `/testsuites/${id}/test_cases/`,
    method: 'get'
  })
}

export function addTestSuiteCases(id, testCaseIds) {
  return request({
    url: `/testsuites/${id}/add_test_cases/`,
    method: 'post',
    data: { test_case_ids: testCaseIds }
  })
}

export function removeTestSuiteCase(id, testCaseId) {
  return request({
    url: `/testsuites/${id}/testcases/${testCaseId}/`,
    method: 'delete'
  })
}

export function reorderTestSuiteCases(id, testCaseOrders) {
  return request({
    url: `/testsuites/${id}/update_test_case_order/`,
    method: 'post',
    data: { test_case_orders: testCaseOrders }
  })
}
