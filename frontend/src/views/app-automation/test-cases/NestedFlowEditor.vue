<template>
    <section class="nested-flow-editor" :class="{ 'is-nested': depth > 0 }">
        <div class="nested-flow-toolbar">
            <span class="nested-flow-label">{{ label }}</span>
            <el-select
                v-model="newStepType"
                class="nested-component-select"
                size="small"
                filterable
                clearable
                :placeholder="$t('appAutomation.sceneBuilder.nested.selectComponent')"
            >
                <el-option
                    v-for="item in components"
                    :key="`${item.kind || 'atomic'}:${item.optionKey || item.type}`"
                    :label="item.name || item.type"
                    :value="item.optionKey || item.type"
                />
            </el-select>
            <el-button size="small" type="primary" :disabled="!newStepType" @click="addStep">
                {{ $t('appAutomation.sceneBuilder.nested.addStep') }}
            </el-button>
        </div>

        <div v-if="steps.length === 0" class="nested-flow-empty">
            {{ $t('appAutomation.sceneBuilder.nested.empty') }}
        </div>

        <draggable
            v-else
            v-model="steps"
            class="nested-flow-list"
            :group="{ name: 'nested-ui-components', pull: false, put: false }"
            :animation="150"
            item-key="id"
        >
            <template #item="{ element, index }">
                <article class="nested-flow-step">
                    <div class="nested-flow-step-heading">
                        <button class="nested-step-toggle" type="button" @click="toggleStep(element)">
                            <span class="nested-step-number">{{ index + 1 }}</span>
                            <span class="nested-step-name">{{ element.name || element.type }}</span>
                            <span class="nested-step-type">{{ element.type }}</span>
                            <span class="nested-step-caret">{{ expandedStepId === element.id ? '−' : '+' }}</span>
                        </button>
                        <el-button link type="danger" size="small" @click.stop="removeStep(index)">
                            {{ $t('appAutomation.common.delete') }}
                        </el-button>
                    </div>

                    <div v-if="expandedStepId === element.id" class="nested-flow-step-config">
                        <el-form-item :label="$t('appAutomation.sceneBuilder.config.stepName')">
                            <el-input v-model.trim="element.name" size="small" />
                        </el-form-item>
                        <el-alert v-if="linkedElementId(element, 'selector') != null"
                            type="info" :closable="false"
                            :title="$t('appAutomation.sceneBuilder.config.linkedElementReference', { id: linkedElementId(element, 'selector') })"
                        />
                        <template v-if="getSchemaFields(element).length">
                            <el-form-item
                                v-for="field in getSchemaFields(element)"
                                :key="field"
                                :label="getFieldLabel(field, element)"
                                :required="isRequired(element, field)"
                            >
                                <div
                                    v-if="isVersionedTypedParameter(element, field)"
                                    class="typed-parameter-mode"
                                >
                                    <el-switch
                                        :model-value="isTypedParameterExpressionMode(element, field)"
                                        :active-text="$t('appAutomation.sceneBuilder.config.expressionMode')"
                                        :inactive-text="$t('appAutomation.sceneBuilder.config.literalMode')"
                                        @update:model-value="setTypedParameterExpressionMode(element, field, $event)"
                                    />
                                </div>
                                <el-input
                                    v-if="isTypedParameterExpressionMode(element, field)"
                                    :model-value="getTypedParameterInput(element, field)"
                                    :type="isSensitive(element, field) ? 'password' : 'text'"
                                    :placeholder="$t('appAutomation.sceneBuilder.config.expressionPlaceholder')"
                                    @update:model-value="updateTypedParameterInput(element, field, $event)"
                                />
                                <NestedFlowEditor
                                    v-else-if="isStepArrayField(element, field, getProperty(element, field))"
                                    :model-value="element.config[field] || []"
                                    :label="getFieldLabel(field, element)"
                                    :components="components"
                                    :component-definitions="componentDefinitions"
                                    :field-label="fieldLabel"
                                    :field-options="fieldOptions"
                                    :depth="depth + 1"
                                    :max-depth="maxDepth"
                                    :focus-path="focusForField(field, index)"
                                    @update:model-value="element.config[field] = $event"
                                />
                                <el-select
                                    v-else-if="getOptions(field, element).length"
                                    :model-value="getStepConfigField(element, field)"
                                    @update:model-value="updateStepConfigField(element, field, $event)"
                                    size="small"
                                    filterable
                                    :placeholder="$t('appAutomation.sceneBuilder.config.pleaseSelect')"
                                >
                                    <el-option
                                        v-for="option in getOptions(field, element)"
                                        :key="String(option.value)"
                                        :label="option.label"
                                        :value="option.value"
                                    />
                                </el-select>
                                <el-switch
                                    v-else-if="getProperty(element, field)?.type === 'boolean'"
                                    :model-value="getTypedScalarControlValue(element, field)"
                                    @update:model-value="element.config[field] = $event"
                                />
                                <el-input-number
                                    v-else-if="getProperty(element, field)?.type === 'number' || getProperty(element, field)?.type === 'integer'"
                                    :model-value="getTypedScalarControlValue(element, field)"
                                    @update:model-value="element.config[field] = $event"
                                    :min="getNumberRule(element, field).min"
                                    :max="getNumberRule(element, field).max"
                                    :step="getNumberRule(element, field).step"
                                    controls-position="right"
                                />
                                <template v-else-if="isStructuredField(element, field)">
                                    <el-input
                                        type="textarea"
                                        :rows="5"
                                        :model-value="getStructuredValue(element, field)"
                                        :placeholder="$t('appAutomation.sceneBuilder.fieldPlaceholder.jsonArraySteps')"
                                        @update:model-value="updateStructuredField(element, field, $event)"
                                    />
                                    <div v-if="jsonErrors[getErrorKey(element, field)]" class="nested-json-error" role="alert">
                                        {{ jsonErrors[getErrorKey(element, field)] }}
                                    </div>
                                </template>
                                <el-input
                                    v-else
                                    :model-value="getStepConfigField(element, field)"
                                    @update:model-value="updateStepConfigField(element, field, $event)"
                                    :type="isSensitive(element, field) ? 'password' : 'text'"
                                    :show-password="isSensitive(element, field)"
                                />
                                <div v-if="jsonErrors[getErrorKey(element, field)] && isTypedParameterExpressionMode(element, field)" class="nested-json-error" role="alert">
                                    {{ jsonErrors[getErrorKey(element, field)] }}
                                </div>
                            </el-form-item>
                        </template>
                        <div v-else class="nested-flow-empty">
                            {{ $t('appAutomation.sceneBuilder.config.noSchema') }}
                        </div>
                        <NestedFlowEditor
                            v-if="hasInlineComponentSteps(element)"
                            class="nested-inline-component-steps"
                            :model-value="element.steps"
                            :label="$t('appAutomation.sceneBuilder.nested.inlineComponentSteps')"
                            :components="components"
                            :component-definitions="componentDefinitions"
                            :field-label="fieldLabel"
                            :field-options="fieldOptions"
                            :depth="depth + 1"
                            :max-depth="maxDepth"
                            :focus-path="focusForField('steps', index)"
                            @update:model-value="element.steps = $event"
                        />
                    </div>
                </article>
            </template>
        </draggable>
    </section>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import draggable from 'vuedraggable'
import { ElMessage } from 'element-plus'
import { getStepConfigField, linkedElementId, updateStepConfigField, withDragImageScopeFields } from '@/utils/app-automation-element-links.mjs'

const props = defineProps({
    modelValue: { type: Array, default: () => [] },
    label: { type: String, default: '' },
    components: { type: Array, default: () => [] },
    componentDefinitions: { type: Object, default: () => ({}) },
    fieldLabel: { type: Function, default: field => field },
    fieldOptions: { type: Function, default: () => [] },
    depth: { type: Number, default: 0 },
    maxDepth: { type: Number, default: 12 },
    focusPath: { type: Array, default: () => [] },
})

const emit = defineEmits(['update:modelValue'])
const { t } = useI18n()
const newStepType = ref('')
const expandedStepId = ref(null)
const jsonErrors = reactive({})
const typedExpressionModes = reactive({})
const steps = computed({
    get: () => props.modelValue,
    set: value => emit('update:modelValue', value),
})

const makeId = () => `nested_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`

watch(() => props.focusPath, focus => {
    const target = steps.value?.[focus?.[0]?.index]
    if (target?.id) expandedStepId.value = target.id
}, { immediate: true })

const focusForField = (field, index) => {
    const [current, child, ...rest] = props.focusPath || []
    return current?.index === index && child?.field === field ? [child, ...rest] : []
}
const clone = value => JSON.parse(JSON.stringify(value))

const getComponentDefaultConfig = (component, definition = getDefinition(component)) => {
    const properties = definition?.schema?.properties || component?.schema?.properties || {}
    const schemaDefaults = Object.fromEntries(Object.entries(properties)
        .filter(([, property]) => Object.prototype.hasOwnProperty.call(property || {}, 'default'))
        .map(([field, property]) => [field, property.default]))
    const configuredDefaults = {
        ...(definition?.default_config || {}),
        ...(definition?.defaultConfig || {}),
        ...(component?.default_config || {}),
        ...(component?.defaultConfig || {}),
    }
    return clone({ ...schemaDefaults, ...configuredDefaults })
}

const hasInlineComponentSteps = step => step?.kind === 'custom'
    && step.version === undefined
    && Array.isArray(step.steps)

const FLOW_CHILD_FIELDS = ['steps', 'then_steps', 'else_steps', 'try_steps', 'catch_steps', 'finally_steps']
const isCustomComponentStep = step => step?.kind === 'custom' || step?.version !== undefined
const normalizeFlowChildren = step => {
    step.id ||= makeId()
    if (!step.config || typeof step.config !== 'object' || Array.isArray(step.config)) step.config = {}
    if (isCustomComponentStep(step)) {
        if (hasInlineComponentSteps(step)) step.steps = step.steps.map(normalizeFlowChildren)
        return step
    }
    for (const field of FLOW_CHILD_FIELDS) {
        if (Array.isArray(step.config[field])) step.config[field] = step.config[field].map(normalizeFlowChildren)
        if (Array.isArray(step[field])) step[field] = step[field].map(normalizeFlowChildren)
    }
    return step
}

const regenerateStepTreeIds = step => {
    step.id = makeId()
    if (isCustomComponentStep(step)) {
        if (hasInlineComponentSteps(step)) step.steps.forEach(regenerateStepTreeIds)
    } else {
        for (const field of FLOW_CHILD_FIELDS) {
            if (Array.isArray(step.config?.[field])) step.config[field].forEach(regenerateStepTreeIds)
            if (Array.isArray(step[field])) step[field].forEach(regenerateStepTreeIds)
        }
    }
    return step
}

const getDefinition = step => (step?.version !== undefined
    ? props.componentDefinitions[`${step.type}@${step.version}`]
    : props.componentDefinitions[step?.type]) || null
const getProperty = (step, field) => getDefinition(step)?.schema?.properties?.[field] || {}
const getSchemaFields = step => withDragImageScopeFields(step, Object.keys(getDefinition(step)?.schema?.properties || {}))
const isVersionedTypedParameter = (step, field) => step?.kind === 'custom'
    && step.version !== undefined
    && ['number', 'integer', 'boolean', 'array', 'object'].includes(getProperty(step, field).type)
const isExactSceneVariableReference = value => typeof value === 'string'
    && /^(?:\{\{\s*(?:local|global)\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\}\}|\$\{\s*(?:local|global)\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*\s*\})$/.test(value)
const isTypedParameterExpressionMode = (step, field) => isVersionedTypedParameter(step, field)
    && (isExactSceneVariableReference(step?.config?.[field]) || typedExpressionModes[getErrorKey(step, field)] === true)
const getTypedParameterInput = (step, field) => {
    const config = step?.config || {}
    const value = config[field]
    if (typeof config[`${field}_input`] === 'string') return config[`${field}_input`]
    if (value === undefined || value === null) return ''
    if (typeof value === 'object') {
        try { return JSON.stringify(value) } catch { return '' }
    }
    return String(value)
}
const parseTypedParameterLiteral = (value, property = {}) => {
    const type = property.type
    if (value === undefined || value === null || value === '') return { valid: true, value }
    const typedResult = literal => {
        if (Array.isArray(property.enum) && property.enum.length) {
            const matches = property.enum.some(option => {
                if (option && typeof option === 'object' && literal && typeof literal === 'object') {
                    try { return JSON.stringify(option) === JSON.stringify(literal) } catch { return false }
                }
                return Object.is(option, literal)
            })
            if (!matches) return { valid: false }
        }
        return { valid: true, value: literal }
    }
    if (type === 'number' || type === 'integer') {
        let number = value
        if (typeof value === 'string') {
            try {
                number = JSON.parse(value)
                if (typeof number !== 'number') number = NaN
            } catch {
                number = NaN
            }
        }
        const valid = Number.isFinite(number)
            && (type !== 'integer' || Number.isInteger(number))
            && (!Number.isFinite(property.minimum) || number >= property.minimum)
            && (!Number.isFinite(property.maximum) || number <= property.maximum)
            && (!Number.isFinite(property.multipleOf) || property.multipleOf <= 0
                || Math.abs(number / property.multipleOf - Math.round(number / property.multipleOf)) < 1e-9)
        return valid ? typedResult(number) : { valid: false }
    }
    if (type === 'boolean') {
        if (value === true || value === false) return typedResult(value)
        if (value === 'true') return typedResult(true)
        if (value === 'false') return typedResult(false)
        return { valid: false }
    }
    if (type === 'array' || type === 'object') {
        let parsed = value
        if (typeof value === 'string') {
            try { parsed = JSON.parse(value) } catch { return { valid: false } }
        }
        const valid = type === 'array'
            ? Array.isArray(parsed)
            : parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
        return valid ? typedResult(parsed) : { valid: false }
    }
    return { valid: false }
}
const getTypedScalarControlValue = (step, field) => {
    const value = step?.config?.[field]
    if (!isVersionedTypedParameter(step, field)) return value
    const property = getProperty(step, field)
    const parsed = parseTypedParameterLiteral(value, property)
    if (property.type === 'boolean') return parsed.valid && parsed.value === true
    return parsed.valid && typeof parsed.value === 'number' ? parsed.value : null
}
const setTypedParameterExpressionMode = (step, field, enabled) => {
    const key = getErrorKey(step, field)
    if (enabled) {
        typedExpressionModes[key] = true
        return
    }
    if (isExactSceneVariableReference(step?.config?.[field])) {
        ElMessage.warning(t('appAutomation.sceneBuilder.messages.literalValueRequired'))
        return
    }
    const result = parseTypedParameterLiteral(step?.config?.[field], getProperty(step, field))
    if (!result.valid) {
        jsonErrors[key] = t('appAutomation.sceneBuilder.messages.invalidTypedParameter')
        ElMessage.warning(t('appAutomation.sceneBuilder.messages.invalidTypedParameter'))
        return
    }
    step.config[field] = result.value
    delete step.config[`${field}_input`]
    delete typedExpressionModes[key]
    delete jsonErrors[key]
}
const updateTypedParameterInput = (step, field, rawValue) => {
    const raw = String(rawValue ?? '')
    delete step.config[`${field}_input`]
    step.config[field] = raw
    const valid = isExactSceneVariableReference(raw) || parseTypedParameterLiteral(raw, getProperty(step, field)).valid
    if (valid) delete jsonErrors[getErrorKey(step, field)]
    else jsonErrors[getErrorKey(step, field)] = t('appAutomation.sceneBuilder.messages.invalidTypedParameter')
}
const isRequired = (step, field) => (getDefinition(step)?.schema?.required || []).includes(field)
const getFieldLabel = (field, step) => props.fieldLabel(field, step)
const getOptions = (field, step) => {
    const property = getProperty(step, field)
    if (Array.isArray(property.enum) && property.enum.length) {
        return property.enum.map(value => ({ label: String(value), value }))
    }
    return props.fieldOptions(field, step, property) || []
}
const isStepArrayField = (step, field, property) => props.depth < props.maxDepth
    && !isCustomComponentStep(step)
    && property?.type === 'array'
    && FLOW_CHILD_FIELDS.includes(field)
const isStructuredField = (step, field) => ['array', 'object'].includes(getProperty(step, field)?.type)
const isSensitive = (step, field) => {
    const property = getProperty(step, field)
    return property.sensitive === true || property.secret === true || property.writeOnly === true
}
const getNumberRule = (step, field) => {
    const property = getProperty(step, field)
    return {
        min: Number.isFinite(property.minimum) ? property.minimum : undefined,
        max: Number.isFinite(property.maximum) ? property.maximum : undefined,
        step: Number.isFinite(property.multipleOf) ? property.multipleOf : 1
    }
}
const getErrorKey = (step, field) => `${step.id}:${field}`

const getStructuredValue = (step, field) => {
    const key = `${field}_input`
    if (typeof step.config[key] === 'string') return step.config[key]
    const value = step.config[field]
    if (value === undefined || value === null) return ''
    try {
        return JSON.stringify(value, null, 2)
    } catch {
        return String(value)
    }
}

const updateStructuredField = (step, field, rawValue) => {
    const input = String(rawValue ?? '')
    const inputKey = `${field}_input`
    const errorKey = getErrorKey(step, field)
    step.config[inputKey] = input
    if (!input.trim()) {
        step.config[field] = getProperty(step, field).type === 'array' ? [] : {}
        delete jsonErrors[errorKey]
        return
    }
    try {
        const parsed = JSON.parse(input)
        const expected = getProperty(step, field).type
        if ((expected === 'array' && !Array.isArray(parsed)) || (expected === 'object' && (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object'))) {
            throw new TypeError('shape')
        }
        step.config[field] = parsed
        delete jsonErrors[errorKey]
    } catch {
        delete step.config[field]
        jsonErrors[errorKey] = t('appAutomation.sceneBuilder.messages.invalidJson')
    }
}

const toggleStep = step => {
    expandedStepId.value = expandedStepId.value === step.id ? null : step.id
}

const addStep = () => {
    if (props.depth >= props.maxDepth) {
        ElMessage.warning(t('appAutomation.sceneBuilder.nested.maxDepthReached'))
        return
    }
    const source = props.components.find(item => (item.optionKey || item.type) === newStepType.value)
    if (!source) return
    const def = getDefinition(source) || source
    const step = {
        id: makeId(),
        type: source.type,
        name: source.name || source.type,
        ...(source.kind === 'custom' ? { kind: 'custom' } : {}),
        ...(source.version !== undefined ? { version: String(source.version) } : {}),
        config: getComponentDefaultConfig(source, def),
    }
    if (source.kind === 'custom' && source.version === undefined && Array.isArray(source.steps)) {
        step.steps = clone(source.steps)
        step.steps.forEach(regenerateStepTreeIds)
    } else {
        normalizeFlowChildren(step)
    }
    steps.value = [...steps.value, step]
    expandedStepId.value = step.id
    newStepType.value = ''
}

const removeStep = index => {
    const removed = steps.value[index]
    steps.value = steps.value.filter((_, current) => current !== index)
    if (removed?.id === expandedStepId.value) expandedStepId.value = null
}

defineExpose({
    addStep,
    getSchemaFields,
    getTypedParameterInput,
    isTypedParameterExpressionMode,
    setTypedParameterExpressionMode,
    updateStructuredField,
    updateTypedParameterInput,
})
</script>

<style scoped>
.nested-flow-editor {
    border: 1px solid #dcdfe6;
    border-radius: 4px;
    padding: 8px;
    background: #fff;
}

.nested-flow-editor.is-nested {
    margin: 4px 0;
    background: #f8fafc;
}

.nested-flow-toolbar,
.nested-flow-step-heading {
    display: flex;
    align-items: center;
    gap: 8px;
}

.nested-flow-toolbar {
    flex-wrap: wrap;
    margin-bottom: 8px;
}

.typed-parameter-mode {
    margin-bottom: 6px;
}

.nested-flow-label {
    flex: 1 1 100%;
    font-size: 12px;
    font-weight: 600;
}

.nested-component-select {
    flex: 1;
    min-width: 140px;
}

.nested-flow-empty {
    padding: 8px;
    color: #909399;
    font-size: 12px;
}

.nested-flow-list {
    display: grid;
    gap: 6px;
}

.nested-flow-step {
    border: 1px solid #ebeef5;
    border-radius: 4px;
    background: #fff;
}

.nested-flow-step-heading {
    justify-content: space-between;
    padding: 4px 8px;
}

.nested-step-toggle {
    display: flex;
    align-items: center;
    flex: 1;
    min-width: 0;
    gap: 8px;
    border: 0;
    padding: 4px 0;
    background: transparent;
    color: inherit;
    text-align: left;
    cursor: pointer;
}

.nested-step-number {
    color: #606266;
    font-size: 12px;
}

.nested-step-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 500;
}

.nested-step-type,
.nested-step-caret {
    flex: none;
    color: #909399;
    font-size: 12px;
}

.nested-step-caret {
    margin-left: auto;
}

.nested-flow-step-config {
    border-top: 1px solid #ebeef5;
    padding: 10px;
}

.nested-flow-step-config :deep(.el-form-item) {
    margin-bottom: 10px;
}

.nested-json-error {
    color: #f56c6c;
    font-size: 12px;
    line-height: 1.4;
}
</style>
