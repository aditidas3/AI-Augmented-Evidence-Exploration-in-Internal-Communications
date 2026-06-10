function escapeRegex(value) {
  return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function tokenizeForMatch(value) {
  return String(value || '').toLowerCase().match(/[a-z0-9]+/g) || [];
}

function charBigrams(value) {
  const normalized = String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  if (normalized.length < 2) return normalized ? [normalized] : [];
  const grams = [];
  for (let index = 0; index < normalized.length - 1; index += 1) {
    grams.push(normalized.slice(index, index + 2));
  }
  return grams;
}

function jaccard(a, b) {
  const left = new Set(charBigrams(a));
  const right = new Set(charBigrams(b));
  if (!left.size || !right.size) return 0;
  let intersection = 0;
  for (const item of left) {
    if (right.has(item)) intersection += 1;
  }
  return intersection / new Set([...left, ...right]).size;
}

function sortedTokenString(value) {
  return tokenizeForMatch(value).sort().join(' ');
}

const DOCUMENT_TYPE_SUFFIXES = new Set([
  'email',
  'e-mail',
  'mail',
  'memo',
  'memorandum',
  'letter',
  'document',
  'report',
  'spreadsheet',
  'chart',
  'graph',
  'map',
  'table',
  'presentation',
  'slide',
  'slides',
  'file',
  'attachment'
]);

function uniqueValues(values) {
  const seen = new Set();
  return values
    .map((value) => String(value || '').trim())
    .filter(Boolean)
    .filter((value) => {
      const key = value.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

export function highlightNeedleCandidates(value) {
  const original = String(value || '').trim();
  if (!original) return [];

  const candidates = [original];
  const tokens = original.match(/\S+/g) || [];
  if (tokens.length > 1) {
    const last = tokens[tokens.length - 1].toLowerCase().replace(/[^a-z-]/g, '');
    if (DOCUMENT_TYPE_SUFFIXES.has(last)) {
      candidates.push(tokens.slice(0, -1).join(' '));
    }
  }

  const withoutParenthetical = original.replace(/\s*\([^)]*\)\s*$/g, '').trim();
  if (withoutParenthetical && withoutParenthetical !== original) candidates.push(withoutParenthetical);

  const beforeSeparator = original.split(/\s+[·|]\s+/)[0]?.trim();
  if (beforeSeparator && beforeSeparator !== original) candidates.push(beforeSeparator);

  return uniqueValues(candidates);
}

function isInsideEmailLikeToken(source, start, end) {
  const left = source.slice(0, start).match(/\S*$/)?.[0] || '';
  const right = source.slice(end).match(/^\S*/)?.[0] || '';
  return `${left}${source.slice(start, end)}${right}`.includes('@');
}

function exactRanges(source, needle) {
  const exactPattern = new RegExp(escapeRegex(needle), 'ig');
  return [...String(source || '').matchAll(exactPattern)].map((match) => ({
    start: match.index,
    end: match.index + match[0].length,
    score: 1,
    method: 'exact',
    needle
  }));
}

export function findApproximateRanges(source, target, threshold = 0.9) {
  const tokens = tokenizeForMatch(target);
  if (!source || !tokens.length) return [];

  const sourceWords = [...String(source).matchAll(/[a-z0-9]+/gi)].map((match) => ({
    text: match[0],
    start: match.index,
    end: match.index + match[0].length
  }));
  const ranges = [];
  const minWindow = Math.max(1, tokens.length);
  const maxWindow = Math.min(tokens.length + 2, tokens.length * 2);

  for (let start = 0; start < sourceWords.length; start += 1) {
    for (let size = minWindow; size <= maxWindow && start + size <= sourceWords.length; size += 1) {
      const window = sourceWords.slice(start, start + size);
      const candidateStart = window[0].start;
      const candidateEnd = window[window.length - 1].end;
      if (isInsideEmailLikeToken(source, candidateStart, candidateEnd)) continue;
      const candidate = source.slice(candidateStart, candidateEnd);
      const direct = jaccard(candidate, target);
      const unordered = jaccard(sortedTokenString(candidate), sortedTokenString(target));
      const score = Math.max(direct, unordered);
      if (score >= threshold) {
        ranges.push({ start: candidateStart, end: candidateEnd, score, method: 'approximate' });
      }
    }
  }

  return ranges
    .sort((a, b) => b.score - a.score || (a.end - a.start) - (b.end - b.start))
    .reduce((kept, range) => {
      const overlaps = kept.some((item) => range.start < item.end && item.start < range.end);
      return overlaps ? kept : [...kept, range];
    }, [])
    .sort((a, b) => a.start - b.start);
}

export function findExactOrApproximateRanges(source, target) {
  const text = String(source || '');
  const needles = Array.isArray(target)
    ? uniqueValues(target.flatMap((item) => highlightNeedleCandidates(item)))
    : highlightNeedleCandidates(target);
  if (!text || !needles.length) return [];

  // Exact matching is never the only strategy. Every call also runs approximate
  // matching so reordered names, punctuation differences, type suffixes, and
  // near-labels can still resolve to a bounded source span.
  const exact = needles.flatMap((needle) => exactRanges(text, needle));
  const approximate = needles.flatMap((needle) =>
    findApproximateRanges(text, needle, 0.82).map((range) => ({ ...range, needle }))
  );
  const candidates = [...exact, ...approximate]
    .sort((a, b) => {
      const scoreDelta = b.score - a.score;
      if (scoreDelta) return scoreDelta;
      if (a.method !== b.method) return a.method === 'exact' ? -1 : 1;
      return (b.end - b.start) - (a.end - a.start);
    });

  if (!candidates.length) return [];

  return candidates
    .reduce((kept, range) => {
      const overlaps = kept.some((item) => range.start < item.end && item.start < range.end);
      return overlaps ? kept : [...kept, range];
    }, [])
    .sort((a, b) => a.start - b.start);
}

export function splitTextByRanges(source, ranges) {
  const text = String(source || '');
  if (!ranges.length) return [{ text, hit: false }];

  const parts = [];
  let cursor = 0;
  for (const range of ranges) {
    if (range.start > cursor) parts.push({ text: text.slice(cursor, range.start), hit: false });
    parts.push({ text: text.slice(range.start, range.end), hit: true });
    cursor = range.end;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), hit: false });
  return parts;
}

export function focusTextAroundRanges(source, ranges, radius = 260) {
  const text = String(source || '');
  if (!text || !ranges.length) return { text, ranges, clippedStart: false, clippedEnd: false };

  const first = ranges[0];
  let start = Math.max(0, first.start - radius);
  let end = Math.min(text.length, first.end + radius);

  const previousBreak = text.lastIndexOf('|', first.start);
  if (previousBreak >= 0 && first.start - previousBreak < radius) {
    start = Math.max(start, previousBreak + 1);
  }

  const nextBreak = text.indexOf('|', first.end);
  if (nextBreak >= 0 && nextBreak - first.end < radius) {
    end = Math.min(end, nextBreak);
  }

  while (start > 0 && /\S/.test(text[start - 1]) && first.start - start < radius + 80) start -= 1;
  while (end < text.length && /\S/.test(text[end]) && end - first.end < radius + 80) end += 1;

  const focusedRanges = ranges
    .filter((range) => range.start < end && start < range.end)
    .map((range) => ({
      ...range,
      start: Math.max(0, range.start - start),
      end: Math.min(end, range.end) - start
    }));

  return {
    text: text.slice(start, end).trim(),
    ranges: focusedRanges,
    clippedStart: start > 0,
    clippedEnd: end < text.length
  };
}

