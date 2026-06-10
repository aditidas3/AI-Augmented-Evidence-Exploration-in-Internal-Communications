export function pct(value) {
  if (value == null || Number.isNaN(Number(value))) return '?';
  return `${Math.round(Number(value) * 100)}%`;
}

export function count(value) {
  return Array.isArray(value) ? value.length : Object.keys(value || {}).length;
}

