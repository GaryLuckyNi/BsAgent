// 旧消息的浏览器 URL 防御；服务端共享凭证策略仍是正式数据出口边界。
const credentialAssignment = /(?:^|[?&#\s"'\\{,])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|password|passwd|secret|token|signature|authorization|auth|key|cookie)[\\"']*\s*[:=]/i
const credentialValue = /\bBearer\s+[^\s,;]+|(?<![a-z0-9])sk-[a-z0-9][a-z0-9_-]{19,}|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\b(?:set-cookie|cookie)\s*:|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/i

function sensitiveKey(key: string): boolean {
  const tokens = key.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean)
  if (tokens.length === 1 && ['authorization', 'credential', 'credentials', 'dsn', 'password', 'secret', 'token', 'cookie'].includes(tokens[0])) return true
  if (tokens.some(token => ['authorization', 'credential', 'credentials', 'dsn'].includes(token))) return true
  if (['password', 'secret', 'token', 'cookie'].includes(tokens.at(-1) || '')) return true
  const joined = tokens.join('_')
  return ['access_token', 'api_key', 'bearer_token', 'client_secret', 'connection_string', 'database_url', 'private_key', 'refresh_token'].some(phrase => ('_' + joined + '_').includes('_' + phrase + '_'))
}

type Budget = { nodes: number; chars: number }
function unsafeText(text: string, budget: Budget, depth = 0, requireJson = false): boolean {
  budget.nodes++; budget.chars += text.length
  if (depth > 16 || budget.nodes > 4096 || budget.chars > 131072) return true
  if (credentialAssignment.test(text) || credentialValue.test(text) || /\b[a-z][a-z0-9+.-]*:\/\/[^\s/:@]+:[^\s/@]+@/i.test(text)) return true
  try {
    if (requireJson && /^[\s]*[\[{]/.test(text)) JSON.parse(text)
    // 所有原始键都检查，包括重复键里被 JSON.parse 覆盖的旧值。
    for (const match of text.matchAll(/("(?:[^"\\]|\\.)*")\s*:/g)) {
      if (sensitiveKey(JSON.parse(match[1]) as string)) return true
    }
    for (const match of text.matchAll(/"(?:[^"\\]|\\.)*"/g)) {
      const decoded = JSON.parse(match[0]) as string
      if (decoded !== text && /[\[{%\\:=]/.test(decoded) && unsafeText(decoded, budget, depth + 1)) return true
    }
    if (text.includes('%')) {
      const decoded = decodeURIComponent(text)
      if (decoded !== text && unsafeText(decoded, budget, depth + 1, requireJson)) return true
    }
  } catch { return true }
  return false
}

function publicIpv4(host: string): boolean {
  const parts = host.split('.').map(Number)
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false
  const [a, b, c] = parts
  return !(a === 0 || a === 10 || a === 127 || a >= 224 || (a === 100 && b >= 64 && b <= 127)
    || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && (b === 168 || (b === 0 && [0, 2].includes(c))))
    || (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) || (a === 203 && b === 0 && c === 113))
}

function publicHost(hostname: string): boolean {
  const host = hostname.replace(/\.$/, '').toLowerCase()
  if (host === 'localhost' || /\.(localhost|local|internal|localdomain)$/.test(host)) return false
  if (host.startsWith('[')) {
    const ip = host.slice(1, -1)
    return /^[23][0-9a-f]{3}:/.test(ip) && !/^(2002:|2001:(0:|:|2:|10:|20:|db8:))/.test(ip)
  }
  if (/^[0-9.]+$/.test(host)) return publicIpv4(host)
  return host.includes('.') && !host.includes('%')
}

export function publicWebUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value || value.length > 16384 || /[\u0000-\u0020\u007f\\]/.test(value)) return
  try {
    const url = new URL(value)
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.port === '0' || !publicHost(url.hostname)) return
    const budget = { nodes: 0, chars: 0 }
    for (const [key, text] of url.searchParams) if (sensitiveKey(key) || unsafeText(text, budget, 0, true)) return
    let probe = value
    for (let round = 0; round < 4; round++) {
      if (/[\u0000-\u001f\u007f]/.test(probe) || unsafeText(probe, budget)) return
      const decoded = decodeURIComponent(probe.replace(/\+/g, ' '))
      if (decoded === probe) return url.href
      probe = decoded
    }
  } catch { return }
}
