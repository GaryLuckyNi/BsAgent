import { readFileSync } from 'node:fs'

const registryPattern =
  /(?:registry-1\.docker\.io|docker\.langfuse\.com|cgr\.dev|public\.ecr\.aws|mirror\.gcr\.io)/i
const networkFailurePattern =
  /(?:\bEOF\b|TLS handshake timeout|context deadline exceeded|Client\.Timeout exceeded|i\/o timeout|no such host|ENOTFOUND|network is unreachable|connection reset|connection refused|temporary failure)/i

export function isLangfuseRegistryNetworkFailure(error) {
  const details = [error?.message, error?.stdout, error?.stderr]
    .filter((value) => typeof value === 'string')
    .join('\n')
  return registryPattern.test(details) && networkFailurePattern.test(details)
}

export function parseEnvironmentFile(path) {
  const values = {}
  for (const rawLine of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const separator = line.indexOf('=')
    if (separator <= 0) continue
    values[line.slice(0, separator).trim()] = line.slice(separator + 1).trim()
  }
  return values
}
