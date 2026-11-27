const SELECTOR_PREFIXES = {
  selector: '', fallback: 'fallback_', click: 'click_', ocr: 'ocr_',
  start: 'start_', end: 'end_', target: 'target_', expected: '',
}

export const elementLinkTargets = Object.keys(SELECTOR_PREFIXES)

const DRAG_IMAGE_SCOPES = {
  start_image_scope: 'start_selector_type',
  end_image_scope: 'end_selector_type',
}

function isDragStep(step) {
  return step?.type === 'drag' && step.kind !== 'custom'
}

export function elementLinkFields(target) {
  if (!Object.hasOwn(SELECTOR_PREFIXES, target)) return null
  if (target === 'expected') {
    return { id: 'element_id', type: 'assert_type', value: 'expected', scope: 'expected_image_scope', threshold: 'expected_image_threshold' }
  }
  const prefix = SELECTOR_PREFIXES[target]
  return {
    id: `${prefix}element_id`,
    type: `${prefix}selector_type`,
    value: `${prefix}selector`,
    scope: `${prefix}image_scope`,
    match: `${prefix}selector_match`,
    threshold: `${prefix}image_threshold`,
  }
}

function effectiveField(step, field) {
  // Older flows may still store fields at the top level, where the runner gives them precedence.
  return Object.hasOwn(step, field) ? step[field] : step.config?.[field]
}

function isImageAssertion(step) {
  return ['assert', 'foreach_assert'].includes(step.type) && effectiveField(step, 'assert_type') === 'image'
}

function locatorTargetForField(step, field) {
  if (!step || step.kind === 'custom') return null
  return elementLinkTargets.find(target => {
    if (target === 'expected' && !isImageAssertion(step)) return false
    const { type, value, scope, match, threshold } = elementLinkFields(target)
    return [type, value, scope, match, threshold].includes(field)
  })
}

export function withDragImageScopeFields(step, fields) {
  if (!isDragStep(step)) return fields

  const scopeFields = Object.entries(DRAG_IMAGE_SCOPES).filter(([field, selectorType]) => (
    (fields.includes(field) || (fields.includes('image_scope') && fields.includes(selectorType)))
    && (effectiveField(step, selectorType) ?? 'image') === 'image'
  )).map(([field]) => field)
  return [...fields.filter(field => field !== 'image_scope' && !Object.hasOwn(DRAG_IMAGE_SCOPES, field)), ...scopeFields]
}

export function getStepConfigField(step, field) {
  if (isDragStep(step) && Object.hasOwn(DRAG_IMAGE_SCOPES, field)) {
    return effectiveField(step, field) || (effectiveField(step, 'image_scope') ?? 'common')
  }
  return locatorTargetForField(step, field) ? effectiveField(step, field) : step?.config?.[field]
}

export function linkedElementId(step, target) {
  const fields = elementLinkFields(target)
  if (!step || !fields || (target === 'expected' && !isImageAssertion(step))) return null
  return effectiveField(step, fields.id) || null
}

export function unlinkStepElement(step, target) {
  const fields = elementLinkFields(target)
  if (!step || !fields) return
  delete step[fields.id]
  if (step.config) delete step.config[fields.id]
}

export const stepElementLinks = Object.fromEntries(elementLinkTargets.map(target => {
  const { id, value, type, scope, match, threshold } = elementLinkFields(target)
  const fields = [value, type, scope, match, threshold].filter(Boolean)
  return [target, { idField: id, fields }]
}))

export { linkedElementId as getStepElementReferenceId, unlinkStepElement as clearStepElementReference }

export function updateStepConfigField(step, field, value) {
  if (!step || Object.is(getStepConfigField(step, field), value)) return
  const target = locatorTargetForField(step, field)
  if (target) {
    unlinkStepElement(step, target)
    delete step[field]
  }
  step.config ||= {}
  step.config[field] = value
}
