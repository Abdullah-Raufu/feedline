export const BOOT = window.__NEWS_BOOTSTRAP__ || {}

let activeRequests = 0
const activityListeners = new Set()

export function subscribeActivity(listener) {
  activityListeners.add(listener)
  listener(activeRequests > 0)
  return () => activityListeners.delete(listener)
}

function emitActivity() {
  const busy = activeRequests > 0
  activityListeners.forEach((listener) => listener(busy))
}

export function tr(key, fallback) {
  const value = BOOT.strings?.[key]
  return value || fallback || key
}

export async function api(path, { method = 'GET', body, signal } = {}) {
  activeRequests += 1
  emitActivity()
  try {
    const headers = { 'X-CSRF-Token': BOOT.csrf || '' }
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    const response = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
    let data = {}
    try {
      data = await response.json()
    } catch (_) {
      /* an error page may not be JSON */
    }
    if (!response.ok) {
      const error = new Error(data.error || data.detail || `Request failed (${response.status})`)
      error.status = response.status
      error.data = data
      throw error
    }
    return data
  } finally {
    activeRequests = Math.max(0, activeRequests - 1)
    emitActivity()
  }
}

export function readHref(link) {
  return '/read?u=' + encodeURIComponent(link)
}

export function imageSrc(url) {
  return url ? '/img?u=' + encodeURIComponent(url) : ''
}

export function offerPasswordSave({ form, username, password, remember = true, next = '/' }) {
  // A real form POST is what browsers look for before offering to save a
  // password. The SPA handles login through fetch, so we repeat the same
  // credentials into a hidden iframe purely for the password manager.
  try {
    if (!form) return
    const userField = form.querySelector('[data-credential-username]')
    const passField = form.querySelector('[data-credential-password]')
    if (!userField || !passField) return
    userField.value = username
    passField.value = password
    passField.name = 'password'
    passField.setAttribute('autocomplete', 'current-password')
    form.action = '/api/auth/login'
    form.method = 'post'
    form.target = 'auth-save-frame'
    const rememberField = form.querySelector('[data-credential-remember]')
    if (rememberField) rememberField.value = remember ? '1' : ''
    form.submit()
  } catch (_) {
    /* saving a password is optional; never block sign-in over it */
  }
  window.setTimeout(() => { window.location.href = next || '/' }, 250)
}

export function reloadApp() {
  window.location.href = '/'
}

export function toast(message) {
  const el = document.getElementById('toast')
  if (!el) return
  el.textContent = message
  el.classList.add('show')
  clearTimeout(window.__toastTimer)
  window.__toastTimer = setTimeout(() => el.classList.remove('show'), 2600)
}

export function useQueryNavigate(navigate) {
  return (params) => {
    const search = new URLSearchParams(params).toString()
    navigate(search ? `/?${search}` : '/')
  }
}
