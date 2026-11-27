const sameId = (left, right) => left != null && right != null && String(left) === String(right)

const deviceOwnerId = device => {
  const owner = device?.locked_by_id ?? device?.locked_by
  return owner && typeof owner === 'object' ? owner.id : owner
}

export function normalizeAppDeviceList(payload) {
  const data = payload?.data ?? payload
  const result = data?.success !== undefined ? data.data : data
  if (Array.isArray(result)) return result
  if (Array.isArray(result?.results)) return result.results
  return []
}

export function normalizeAppDevice(payload) {
  let data = payload?.data ?? payload
  if (data?.success !== undefined) {
    if (data.device) return data.device
    data = data.data ?? data
  }
  return data?.device ?? data
}

export function findAppDevice(devices, selectedId, selectedKey = 'id') {
  if (!Array.isArray(devices) || (selectedKey !== 'id' && selectedKey !== 'device_id')) return undefined
  return devices.find(device => sameId(device[selectedKey], selectedId))
}

export function isAppDeviceLockedByUser(device, userId) {
  const ownerId = deviceOwnerId(device)
  return ownerId != null && sameId(ownerId, userId)
}

export function canSelectAppExecutionDevice(device, userId) {
  if (!device || device.status === 'offline' || device.maintenance_mode || device.lease_requires_token === true) return false
  const ownerId = deviceOwnerId(device)
  if (ownerId != null) {
    return device.status === 'locked' && isAppDeviceLockedByUser(device, userId)
  }
  return device.status === 'available' || device.status === 'online'
}

export async function prepareAppDeviceForExecution({
  selectedId,
  selectedKey = 'id',
  userId,
  sessionGeneration,
  getCurrentUserId = () => userId,
  getCurrentSessionGeneration = () => sessionGeneration,
  getDevice,
  onDeviceRefreshed,
  t,
}) {
  const message = (key, fallback) => {
    const translationKey = `appAutomation.device.messages.${key}`
    const translated = typeof t === 'function' ? t(translationKey) : ''
    return typeof translated === 'string' && translated.trim() && translated !== translationKey
      ? translated
      : fallback
  }
  const verifyUser = () => {
    if (
      !sameId(userId, getCurrentUserId()) ||
      (sessionGeneration !== undefined && getCurrentSessionGeneration() !== sessionGeneration)
    ) {
      throw new Error(message('executionUserChanged', '登录用户已变化，请重新选择设备'))
    }
  }
  verifyUser()

  const response = await getDevice(selectedId)
  verifyUser()
  const device = normalizeAppDevice(response)
  if (!device || !sameId(device[selectedKey], selectedId)) {
    throw new Error(message('executionDeviceUnavailable', '设备不可用，请刷新设备列表'))
  }
  onDeviceRefreshed?.(device)
  if (device.lease_requires_token === true) {
    throw new Error(message(
      'executionTokenLeaseUnavailable',
      '该设备的租约需要 API 令牌，网页端无法使用。请释放此租约，或使用已授权的 API 客户端执行。'
    ))
  }
  if (!canSelectAppExecutionDevice(device, userId)) {
    throw new Error(message('executionDeviceUnavailable', '设备不可用，请刷新设备列表'))
  }
  return device
}

export function getAppExecutionErrorMessage(error, fallback) {
  const text = [error?.response?.data?.message, error?.response?.data?.detail, error?.message]
    .find(value => typeof value === 'string' && value.trim())
  return text ? text.trim().slice(0, 500) : fallback
}
