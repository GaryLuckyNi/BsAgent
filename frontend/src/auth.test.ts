import { describe, expect, it } from 'vitest'

import { isOidcCallback, resolveOidcConfig } from './auth'

describe('resolveOidcConfig', () => {
  const location = {
    origin: 'https://agent.example.com'
  }

  it('returns disabled when no OIDC values are configured', () => {
    expect(resolveOidcConfig({}, location)).toEqual({ enabled: false })
  })

  it('builds a PKCE client configuration from runtime values', () => {
    expect(
      resolveOidcConfig(
        {
          oidcAuthority: 'https://id.example.com/',
          oidcClientId: 'bs-agent-web',
          oidcScope: 'openid profile email',
          oidcRedirectUri: 'https://agent.example.com/auth/callback',
          oidcPostLogoutRedirectUri: 'https://agent.example.com/signed-out'
        },
        location
      )
    ).toEqual({
      enabled: true,
      authority: 'https://id.example.com',
      clientId: 'bs-agent-web',
      scope: 'openid profile email',
      redirectUri: 'https://agent.example.com/auth/callback',
      postLogoutRedirectUri: 'https://agent.example.com/signed-out'
    })
  })

  it('uses same-origin callback defaults', () => {
    expect(
      resolveOidcConfig(
        {
          oidcAuthority: 'https://id.example.com',
          oidcClientId: 'bs-agent-web'
        },
        location
      )
    ).toMatchObject({
      scope: 'openid profile',
      redirectUri: 'https://agent.example.com/auth/callback',
      postLogoutRedirectUri: 'https://agent.example.com/'
    })
  })

  it('rejects partial OIDC configuration', () => {
    expect(() =>
      resolveOidcConfig({ oidcAuthority: 'https://id.example.com' }, location)
    ).toThrow('OIDC authority 和 client ID 必须同时配置')
  })
})

describe('isOidcCallback', () => {
  it('recognizes authorization-code callbacks', () => {
    expect(isOidcCallback('?code=abc&state=state-1')).toBe(true)
  })

  it('recognizes authorization error callbacks', () => {
    expect(isOidcCallback('?error=access_denied&state=state-1')).toBe(true)
  })

  it('does not treat ordinary query strings as callbacks', () => {
    expect(isOidcCallback('?trace_id=trace-1')).toBe(false)
  })
})
