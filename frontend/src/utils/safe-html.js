import { marked } from 'marked'

const ALLOWED_TAGS = new Set([
  'A', 'BLOCKQUOTE', 'BR', 'CODE', 'EM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
  'LI', 'OL', 'P', 'PRE', 'STRONG', 'UL'
])
const ALLOWED_ATTRIBUTES = new Set(['href', 'title', 'target', 'rel', 'class'])

function isSafeUrl(value) {
  if (!value) return false
  if (value.startsWith('#') || value.startsWith('/')) return true
  try {
    const url = new URL(value, window.location.origin)
    return url.protocol === 'http:' || url.protocol === 'https:' || url.protocol === 'mailto:'
  } catch {
    return false
  }
}

export function sanitizeHtml(html) {
  const template = document.createElement('template')
  template.innerHTML = String(html || '')

  for (const element of Array.from(template.content.querySelectorAll('*'))) {
    if (!ALLOWED_TAGS.has(element.tagName)) {
      element.replaceWith(document.createTextNode(element.textContent || ''))
      continue
    }

    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase()
      if (!ALLOWED_ATTRIBUTES.has(name) || name.startsWith('on')) {
        element.removeAttribute(attribute.name)
        continue
      }
      if ((name === 'href' || name === 'src') && !isSafeUrl(attribute.value)) {
        element.removeAttribute(attribute.name)
      }
    }

    if (element.tagName === 'A' && element.getAttribute('target') === '_blank') {
      element.setAttribute('rel', 'noopener noreferrer')
    }
  }

  return template.innerHTML
}

export function renderSafeMarkdown(markdown) {
  return sanitizeHtml(marked.parse(String(markdown || ''), {
    headerIds: false,
    mangle: false,
  }))
}

export function escapeHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}
