export class RunSubscriptionAuthorizationError extends Error {
  readonly code: string

  constructor(message: string, code: string) {
    super(message)
    this.name = 'RunSubscriptionAuthorizationError'
    this.code = code
  }
}

export function isStreamAccessCode(code: unknown): code is string {
  return typeof code === 'string' && [
    'stream_access_denied', 'stream_access_expired', 'stream_access_unavailable'
  ].includes(code)
}

export function isRunSubscriptionAuthorizationError(error: unknown): error is Error {
  // 真实 SDK 会重建 Error 并丢弃 code，但保留受信服务返回的 name。
  return error instanceof Error && error.name === 'RunSubscriptionAuthorizationError'
}

export async function authorizedSubscriptionFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, init)
  if (![401,403,404,503].includes(response.status)) return response
  let code = `HTTP_${response.status}`
  try {
    const detail = (await response.clone().json())?.detail
    const candidate: unknown = detail?.code
    if (isStreamAccessCode(candidate) || ['permission_directory_unavailable','permission_denied','invalid_token','authentication_required','run_not_found'].includes(candidate as string)) {
      code = candidate as string
    }
  } catch { /* 非 JSON 认证拒绝仍用固定消息，不回显代理正文。 */ }
  if (response.status === 503 && code !== 'permission_directory_unavailable' && !isStreamAccessCode(code)) return response
  throw new RunSubscriptionAuthorizationError(
    response.status === 404 ? '当前资源不可用，请确认身份和资源后重连。' : '订阅未获有效授权，请重新登录或确认权限后重连。', code
  )
}
