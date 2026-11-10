import api from '@/utils/api'

const baseUrl = '/api-testing/openapi-contracts'

export const listOpenApiContractSpecs = (params = {}) => api.get(`${baseUrl}/`, { params })
export const getOpenApiContractSpec = (id) => api.get(`${baseUrl}/${id}/`)
export const createOpenApiContractSpec = (data) => api.post(`${baseUrl}/`, data)
export const updateOpenApiContractSpec = (id, data) => api.patch(`${baseUrl}/${id}/`, data)
export const deleteOpenApiContractSpec = (id) => api.delete(`${baseUrl}/${id}/`)
export const diffOpenApiContractSpecs = (id, baselineId) => api.post(`${baseUrl}/${id}/diff/`, { baseline_id: baselineId })
export const getOpenApiContractCoverage = (id) => api.get(`${baseUrl}/${id}/coverage/`)
export const listGeneratedOpenApiContractCases = (id) => api.get(`${baseUrl}/${id}/generate-cases/`)
export const materializeOpenApiContractCases = (id, data = {}) => api.post(`${baseUrl}/${id}/materialize-cases/`, data)
export const executeOpenApiContractSpec = (id, data) => api.post(`${baseUrl}/${id}/execute/`, data)
