const DEFAULT_API_BASE = '';

export function apiBase() {
  return globalThis.window?.EVIDENCE_EXPLORER_API_BASE || DEFAULT_API_BASE;
}

export function apiPath(path) {
  if (/^https?:\/\//i.test(path)) return path;
  return `${apiBase()}${path}`;
}

export async function requestJson(path, options = {}) {
  const response = await fetch(apiPath(path), {
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  });
  if (!response.ok) {
    throw new Error(`Request failed for ${path}: ${response.status}`);
  }
  return response.json();
}

export async function requestText(path, options = {}) {
  const response = await fetch(apiPath(path), {
    cache: 'no-store',
    ...options
  });
  if (!response.ok) {
    throw new Error(`Request failed for ${path}: ${response.status}`);
  }
  return response.text();
}
