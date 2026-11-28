const list = value => Array.isArray(value) ? value : []

export function uiEngineOptions(readiness) {
  return list(readiness?.engines).map(engine => ({
    ...engine,
    label: engine.label || (engine.value === 'playwright' ? 'Playwright' : 'Selenium'),
    disabled: engine.available !== true,
  }))
}

export function uiBrowserOptions(readiness, selectedEngine) {
  const engine = list(readiness?.engines).find(item => item.value === selectedEngine)
  const labels = { chrome: 'Chrome / Chromium', chromium: 'Chromium', firefox: 'Firefox', edge: 'Edge', safari: 'WebKit / Safari', webkit: 'WebKit' }
  return list(engine?.browsers).map(browser => ({
    ...browser,
    label: browser.label || labels[browser.value] || browser.value,
    disabled: engine.available !== true || browser.available !== true,
  }))
}

export function uiReadinessError(readiness, selectedEngine, selectedBrowser) {
  if (!readiness) return 'Execution readiness has not been checked.'
  if (readiness.base_url_ready !== true) return readiness.base_url_message || 'The project base URL is not ready.'
  const engine = list(readiness.engines).find(item => item.value === selectedEngine)
  if (!engine || engine.available !== true) return engine?.message || 'This execution engine is unavailable.'
  const browser = list(engine.browsers).find(item => item.value === selectedBrowser)
  if (!browser || browser.available !== true) return browser?.message || 'This browser is unavailable on the execution service.'
  return ''
}
