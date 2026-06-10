export function parsePermissiveJson(text) {
  return JSON.parse(
    text
      .replace(/\bNaN\b/g, 'null')
      .replace(/\bInfinity\b/g, 'null')
      .replace(/\b-Infinity\b/g, 'null')
  );
}
