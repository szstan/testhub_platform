import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { parse } from '@vue/compiler-sfc'

test('API dashboard quick actions are keyboard-operable buttons', async () => {
  const source = await readFile(new URL('../src/views/api-testing/Dashboard.vue', import.meta.url), 'utf8')
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])

  const template = descriptor.template.content
  const actions = template.match(/<button\b[^>]*class="action-item"[^>]*>/g) || []
  assert.equal(actions.length, 8)
  assert.doesNotMatch(template, /<div\b[^>]*class="action-item"/)
  assert.match(source, /\.action-item:focus-visible/)
})
