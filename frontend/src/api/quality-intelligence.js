import request from '@/utils/api'

const baseUrl = '/quality-intelligence'

const list = (resource, params) => request({ url: `${baseUrl}/${resource}/`, method: 'get', params })
const detail = (resource, id, params) => request({ url: `${baseUrl}/${resource}/${id}/`, method: 'get', params })

export function getQualitySummary(params) {
  return request({ url: `${baseUrl}/summary/`, method: 'get', params })
}

export function getQualityTrend(params) {
  return request({ url: `${baseUrl}/trend/`, method: 'get', params })
}

export function getQualityRuns(params) {
  return list('runs', params)
}

export function getQualityRun(id) {
  return detail('runs', id)
}

export function getQualityRunItems(id, params) {
  return request({ url: `${baseUrl}/runs/${id}/items/`, method: 'get', params })
}

export function getQualityRunEvidence(id, params) {
  return request({ url: `${baseUrl}/runs/${id}/evidence/`, method: 'get', params })
}

export function recomputeQualityRun(id) {
  return request({ url: `${baseUrl}/runs/${id}/recompute/`, method: 'post' })
}

export function ingestQuality(data) {
  return request({ url: `${baseUrl}/ingest/`, method: 'post', data })
}

export function getQualityItems(params) {
  return list('items', params)
}

export function getQualityEvidence(params) {
  return list('evidence', params)
}

export function getQualityFailures(params) {
  return list('failures', params)
}

export function reviewQualityFailure(id, data) {
  return request({ url: `${baseUrl}/failures/${id}/review/`, method: 'patch', data })
}

export function getQualityFailureClusters(params) {
  return list('failure-clusters', params)
}

export function mapQualityFailureClusterProject(id, data) {
  return request({ url: `${baseUrl}/failure-clusters/${id}/map-project/`, method: 'post', data })
}

export function assignQualityFailureCluster(id, data) {
  return request({ url: `${baseUrl}/failure-clusters/${id}/owner/`, method: 'patch', data })
}


export function createQualityFailureDefect(id, data = {}) {
  return request({ url: `${baseUrl}/failure-clusters/${id}/create-defect/`, method: 'post', data })
}

export function linkQualityFailureDefect(id, data) {
  return request({ url: `${baseUrl}/failure-clusters/${id}/link-defect/`, method: 'post', data })
}

export function linkQualityFailureExternal(id, data) {
  return request({ url: `${baseUrl}/failure-clusters/${id}/link-external/`, method: 'post', data })
}


export function getQualityProfiles(params) {
  return list('profiles', params)
}

export function getQualityProfile(id) {
  return detail('profiles', id)
}

export function getQualityProfileHistory(id, params) {
  return request({ url: `${baseUrl}/profiles/${id}/history/`, method: 'get', params })
}

export function createQualityQuarantine(profileId, data) {
  return request({ url: `${baseUrl}/profiles/${profileId}/quarantine/`, method: 'post', data })
}

export function getQualityQuarantines(params) {
  return list('quarantines', params)
}

export function approveQualityQuarantine(id, data = {}) {
  return request({ url: `${baseUrl}/quarantines/${id}/approve/`, method: 'post', data })
}

export function rejectQualityQuarantine(id, data = {}) {
  return request({ url: `${baseUrl}/quarantines/${id}/reject/`, method: 'post', data })
}

export function releaseQualityQuarantine(id, data = {}) {
  return request({ url: `${baseUrl}/quarantines/${id}/release/`, method: 'post', data })
}

export function getQualityBaselines(params) {
  return list('baselines', params)
}

export function createQualityBaseline(data) {
  return request({ url: `${baseUrl}/baselines/`, method: 'post', data })
}

export function updateQualityBaseline(id, data) {
  return request({ url: `${baseUrl}/baselines/${id}/`, method: 'patch', data })
}

export function getQualityComparisons(params) {
  return list('comparisons', params)
}
