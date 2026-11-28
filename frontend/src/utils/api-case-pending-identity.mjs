const STORAGE_KEY = 'testhub.api-case-pending-identities.v2'
const sharedMemory = new Map()

function normalizeId(value) {
  const id = value && typeof value === 'object' ? value.id : value
  return id == null || id === '' ? '' : String(id)
}

function normalizeIdentity(identity) {
  const normalized = {
    actorId: normalizeId(identity?.actorId),
    projectId: normalizeId(identity?.projectId),
    caseId: normalizeId(identity?.caseId),
    fingerprint: normalizeId(identity?.fingerprint),
  }
  if (Object.values(normalized).some(value => !value)) return null
  return normalized
}

function identityKey(identity) {
  return JSON.stringify([identity.actorId, identity.projectId, identity.caseId, identity.fingerprint])
}

function readRecords(storage) {
  try {
    const value = storage?.getItem(STORAGE_KEY)
    if (value == null || value === '') return []
    const parsed = JSON.parse(value)
    if (!Array.isArray(parsed)) return null
    return parsed.filter(record => (
      record && typeof record.actorId === 'string' && record.actorId &&
      typeof record.projectId === 'string' && record.projectId &&
      typeof record.caseId === 'string' && record.caseId &&
      typeof record.fingerprint === 'string' && record.fingerprint &&
      typeof record.key === 'string' && record.key
    ))
  } catch {
    return null
  }
}

function sameIdentity(record, identity) {
  return record.actorId === identity.actorId &&
    record.projectId === identity.projectId &&
    record.caseId === identity.caseId &&
    record.fingerprint === identity.fingerprint
}

export function createApiCasePendingIdentityStore({ storage = () => globalThis.sessionStorage, memory = sharedMemory } = {}) {
  function get(identityValue) {
    const identity = normalizeIdentity(identityValue)
    if (!identity) return null
    const key = identityKey(identity)
    if (memory.has(key)) return memory.get(key)?.key || null

    let source
    try { source = typeof storage === 'function' ? storage() : storage } catch { return null }
    const records = readRecords(source)
    const record = records?.find(item => sameIdentity(item, identity))
    if (!record) return null
    memory.set(key, { identity, key: record.key })
    return record.key
  }

  function remember(identityValue, pendingKey) {
    const identity = normalizeIdentity(identityValue)
    if (!identity || typeof pendingKey !== 'string' || !pendingKey) return null
    const key = identityKey(identity)
    const current = get(identity)
    if (current) return current

    const entry = { identity, key: pendingKey }
    memory.set(key, entry)
    let source
    try { source = typeof storage === 'function' ? storage() : storage } catch { return pendingKey }
    const records = readRecords(source)
    if (!records) return pendingKey
    const existing = records.find(item => sameIdentity(item, identity))
    if (existing) {
      memory.set(key, { identity, key: existing.key })
      return existing.key
    }
    try { source.setItem(STORAGE_KEY, JSON.stringify([...records, { ...identity, key: pendingKey }])) } catch {
      // Keep the identity in memory when session storage is unavailable or full.
    }
    return pendingKey
  }

  function forget(identityValue, expectedKey) {
    const identity = normalizeIdentity(identityValue)
    if (!identity || typeof expectedKey !== 'string' || !expectedKey) return false
    const key = identityKey(identity)
    if (get(identity) !== expectedKey) return false

    // A tombstone prevents stale storage from restoring a cleared key in this page.
    memory.set(key, { identity, key: null })
    let source
    try { source = typeof storage === 'function' ? storage() : storage } catch { return true }
    const records = readRecords(source)
    if (!records) return true
    const remaining = records.filter(record => !sameIdentity(record, identity) || record.key !== expectedKey)
    if (remaining.length !== records.length) {
      try { source.setItem(STORAGE_KEY, JSON.stringify(remaining)) } catch {
        // The in-memory tombstone still prevents reuse until the page is closed.
      }
    }
    return true
  }

  return { get, remember, forget }
}

export async function apiCaseExecutionFingerprint(context) {
  const fingerprint = context?.execution_fingerprint
  return typeof fingerprint === 'string' && /^[0-9a-f]{64}$/.test(fingerprint) ? fingerprint : ''
}
