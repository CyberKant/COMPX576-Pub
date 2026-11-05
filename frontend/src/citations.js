// Edge-citation parsing for the synthesized answer.
//
// The synthesis prompt asks the LLM to cite graph edges as `(SOURCE) -[RELATION]-> (TARGET)`, but at
// higher temperatures it drifts: entities in backticks or bold, parentheses on one side only,
// mixed case, chained paths. A single rigid regex missed ~1/3 of real citations, so this module
// finds the `-[REL]->` operator and parses the nearest operand on each side instead.
//
// Pure functions (no JSX / DOM) so they can be replayed against saved answers in Node.

export const EDGE_TOKEN_PREFIX = 'edge:'

const WRAPPERS = ['**', '*', '`'] // longest first
const MAX_NAME = 60
const MAX_WORDS = 3 // bare multi-word names are only accepted when they match a known entity

// -[REL]-> with tolerant dashes / arrows / escaped brackets; REL may be any case or contain spaces
const OPERATOR_SRC = '[-–—]{1,2}[ \\t]*\\\\?\\[[ \\t]*([A-Za-z][A-Za-z_ ]{0,30}?)[ \\t]*\\\\?\\][ \\t]*(?:[-–—]{1,2}>|[→⟶⇒])'
const NAME_OK = /^[\p{L}\p{N}](?:[\p{L}\p{N}_./+:-]{0,58}[\p{L}\p{N}_])?$/u
const BARE_LEFT = /[\p{L}\p{N}](?:[\p{L}\p{N}_.-]*[\p{L}\p{N}_])?$/u
const WORD_LEFT = /([\p{L}\p{N}][\p{L}\p{N}_.-]*)[ \t]+$/u
const BARE_RIGHT = /^[\p{L}\p{N}](?:[\p{L}\p{N}_.-]*[\p{L}\p{N}_])?/u
const WORD_RIGHT = /^[ \t]+([\p{L}\p{N}](?:[\p{L}\p{N}_.-]*[\p{L}\p{N}_])?)/u

const FENCED = /(```[\s\S]*?```)/
const INLINE_CODE = /(`[^`\n]*`)/

export const normalizeName = (s) => String(s).trim().replace(/\s+/g, '_').toUpperCase()
const normalizeRelation = (s) => String(s).trim().replace(/\s+/g, '_').toUpperCase()

// An undelimited (no parentheses / backticks / bold) operand must look like an identifier, otherwise
// prose such as "the operator -[X]-> with" would be swallowed: needs _, a digit, ALL CAPS, camelCase,
// or to be a known graph entity.
function looksLikeIdentifier(raw, known) {
  return /[_\d]/.test(raw) || /[a-z][A-Z]/.test(raw) || (raw === raw.toUpperCase() && /[A-Z]/.test(raw)) || known.has(normalizeName(raw))
}

// `X`, **X**, *X* -> X (applied up to twice, e.g. **`X`**)
function stripWrap(s) {
  let t = s.trim()
  for (let n = 0; n < 2; n++) {
    const w = WRAPPERS.find((x) => t.length > 2 * x.length && t.startsWith(x) && t.endsWith(x))
    if (!w) break
    t = t.slice(w.length, -w.length).trim()
  }
  return t
}

// Normalised entity name, or null if it doesn't look like one
function cleanName(raw) {
  const name = normalizeName(stripWrap(raw))
  return NAME_OK.test(name) ? name : null
}

// Whitespace skipping that may cross one line break but never a blank line
function skipBack(text, i) {
  let nl = 0
  while (i > 0) {
    const c = text[i - 1]
    if (c === ' ' || c === '\t') i--
    else if (c === '\n' && nl === 0) { nl = 1; i-- }
    else break
  }
  return i
}

function skipFwd(text, i) {
  let nl = 0
  while (i < text.length) {
    const c = text[i]
    if (c === ' ' || c === '\t') i++
    else if (c === '\n' && nl === 0) { nl = 1; i++ }
    else break
  }
  return i
}

// ---- left operand: the term ending just before the operator --------------------------------

function parenLeft(text, closeIdx, w) {
  const open = text.lastIndexOf('(', closeIdx)
  if (open < 0 || closeIdx - open > MAX_NAME + 12) return null
  const inner = text.slice(open + 1, closeIdx)
  if (/[()\n]/.test(inner)) return null
  let start = open
  if (w) {
    // an outer wrapper is only consumed as a symmetric pair; otherwise it belongs to surrounding text
    if (text.slice(open - w.length, open) !== w) return null
    start = open - w.length
  }
  const name = cleanName(inner)
  return name ? { start, name } : null
}

function parseLeft(text, opStart, known) {
  const i = skipBack(text, opStart)
  if (text[i - 1] === ')') return parenLeft(text, i - 1, '') // (NAME)

  const w = WRAPPERS.find((x) => i >= x.length && text.slice(i - x.length, i) === x)
  if (w) {
    if (text[i - w.length - 1] === ')') return parenLeft(text, i - w.length - 1, w) // **(NAME)**
    const closeIdx = i - w.length
    const open = text.lastIndexOf(w, closeIdx - 1) // `NAME`, **NAME**, *NAME*
    if (open < 0 || closeIdx - open - w.length > MAX_NAME) return null
    const inner = text.slice(open + w.length, closeIdx)
    if (/[()\n]/.test(inner) || inner.includes(w)) return null
    const name = cleanName(inner)
    return name ? { start: open, name } : null
  }

  // Bare token: only the single word next to the operator, unless a longer run is a known entity
  const win = text.slice(Math.max(0, i - 120), i)
  const m = BARE_LEFT.exec(win)
  if (!m) return null
  const start = i - win.length + m.index
  let best = { start, name: m[0] }
  let words = [m[0]]
  let cur = start
  for (let n = 0; n < MAX_WORDS; n++) {
    const before = text.slice(Math.max(0, cur - 120), cur)
    const m2 = WORD_LEFT.exec(before)
    if (!m2) break
    cur = cur - before.length + m2.index
    words = [m2[1], ...words]
    const cand = words.join(' ')
    if (known.has(normalizeName(cand))) best = { start: cur, name: cand }
  }
  if (!looksLikeIdentifier(best.name, known)) return null
  const name = cleanName(best.name)
  return name ? { start: best.start, name } : null
}

// ---- right operand: the term starting just after the operator ------------------------------

function parenRight(text, openIdx, w) {
  const close = text.indexOf(')', openIdx)
  if (close < 0 || close - openIdx > MAX_NAME + 12) return null
  const inner = text.slice(openIdx + 1, close)
  if (/[()\n]/.test(inner)) return null
  let end = close + 1
  if (w) {
    if (text.slice(end, end + w.length) !== w) return null
    end += w.length
  }
  const name = cleanName(inner)
  return name ? { end, name } : null
}

function parseRight(text, opEnd, known) {
  const j = skipFwd(text, opEnd)
  if (text[j] === '(') return parenRight(text, j, '') // (NAME)

  const w = WRAPPERS.find((x) => text.startsWith(x, j))
  if (w) {
    if (text[j + w.length] === '(') return parenRight(text, j + w.length, w) // **(NAME)**
    const close = text.indexOf(w, j + w.length) // `NAME`, **NAME**, *NAME*
    if (close < 0 || close - j - w.length > MAX_NAME) return null
    const inner = text.slice(j + w.length, close)
    if (/[()\n]/.test(inner)) return null
    const name = cleanName(inner)
    return name ? { end: close + w.length, name } : null
  }

  const m = BARE_RIGHT.exec(text.slice(j, j + 120))
  if (!m) return null
  let end = j + m[0].length
  let best = { end, name: m[0] }
  let words = [m[0]]
  for (let n = 0; n < MAX_WORDS; n++) {
    const m2 = WORD_RIGHT.exec(text.slice(end, end + 120))
    if (!m2) break
    end += m2[0].length
    words = [...words, m2[1]]
    const cand = words.join(' ')
    if (known.has(normalizeName(cand))) best = { end, name: cand }
  }
  if (!looksLikeIdentifier(best.name, known)) return null
  const name = cleanName(best.name)
  return name ? { end: best.end, name } : null
}

// ---- assembly ------------------------------------------------------------------------------

// `edge:<url-encoded JSON>` as an inline-code token. No raw `|` so a citation inside a GFM table
// cell can't split the row.
const makeToken = (src, rel, tgt) => `\`${EDGE_TOKEN_PREFIX}${encodeURIComponent(JSON.stringify([src, rel, tgt]))}\``

export function parseEdgeToken(content) {
  if (typeof content !== 'string' || !content.startsWith(EDGE_TOKEN_PREFIX)) return null
  try {
    const arr = JSON.parse(decodeURIComponent(content.slice(EDGE_TOKEN_PREFIX.length)))
    return Array.isArray(arr) && arr.length === 3 && arr.every((x) => typeof x === 'string')
      ? { src: arr[0], rel: arr[1], tgt: arr[2] }
      : null
  } catch {
    return null
  }
}

// Replace every parseable citation in `text` with an edge token. Chained paths
// (A) -[R1]-> (B) -[R2]-> (C) become two consecutive chips.
export function citationsToTokens(text, known = new Set()) {
  const hits = []
  for (const m of text.matchAll(new RegExp(OPERATOR_SRC, 'g'))) {
    const left = parseLeft(text, m.index, known)
    if (!left) continue
    const right = parseRight(text, m.index + m[0].length, known)
    if (!right) continue
    hits.push({ start: left.start, end: right.end, src: left.name, rel: normalizeRelation(m[1]), tgt: right.name })
  }

  let out = ''
  let cursor = 0
  for (const h of hits) {
    if (h.end <= cursor) continue
    if (h.start >= cursor) out += text.slice(cursor, h.start) + makeToken(h.src, h.rel, h.tgt)
    else out += ' ' + makeToken(h.src, h.rel, h.tgt) // chained: left operand was the previous chip's target
    cursor = h.end
  }
  return out + text.slice(cursor)
}

// An inline-code span that wraps a whole citation, `A -[R]-> B`, is unwrapped so it can become a chip
function unwrapCodedCitations(text, known) {
  return text
    .split(INLINE_CODE)
    .map((seg, i) => {
      if (i % 2 === 0) return seg
      const inner = seg.slice(1, -1)
      const processed = citationsToTokens(inner, known)
      return processed !== inner ? processed : seg
    })
    .join('')
}

// remark-math only understands $…$ / $$…$$; Gemini sometimes emits \( … \) and \[ … \]
function convertMathDelimiters(text) {
  return text
    .split(INLINE_CODE)
    .map((seg, i) => (i % 2 === 1
      ? seg
      : seg
        .replace(/\\\[([\s\S]+?)\\\]/g, (_, m) => `\n$$\n${m.trim()}\n$$\n`)
        .replace(/\\\(([\s\S]+?)\\\)/g, (_, m) => `$${m.trim()}$`)))
    .join('')
}

// Full pre-processing pipeline. Fenced code blocks are left verbatim. Citations are converted
// BEFORE the math rewrite so escaped brackets (-\[REL\]->) are never mistaken for LaTeX.
export function prepareAnswer(text = '', knownEntities = []) {
  const known = new Set((knownEntities || []).map(normalizeName))
  return String(text)
    .split(FENCED)
    .map((part, i) => {
      if (i % 2 === 1) return part
      return convertMathDelimiters(citationsToTokens(unwrapCodedCitations(part, known), known))
    })
    .join('')
}
