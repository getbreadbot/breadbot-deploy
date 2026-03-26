// lib/api.js — thin fetch wrapper, all calls go through here

export async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  })
  if (res.status === 401) {
    window.location.href = '/login'
    return null
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Request failed')
  }
  return res.json()
}

export const get  = (path) => api(path)
export const post = (path, body) => api(path, { method: 'POST', body })
export const del  = (path, body) => api(path, { method: 'DELETE', body })
