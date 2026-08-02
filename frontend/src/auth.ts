import { UserManager, WebStorageStateStore, type User } from 'oidc-client-ts'

export type RuntimeConfig = {
  oidcAuthority?: string
  oidcClientId?: string
  oidcScope?: string
  oidcRedirectUri?: string
  oidcPostLogoutRedirectUri?: string
}

export type OidcConfig =
  | { enabled: false }
  | {
      enabled: true
      authority: string
      clientId: string
      scope: string
      redirectUri: string
      postLogoutRedirectUri: string
    }

export type AuthSnapshot = {
  status: 'disabled' | 'loading' | 'authenticated' | 'anonymous' | 'error'
  accessToken: string
  tenantId: string
  userId: string
  displayName: string
  error: string
}

type LocationOrigin = Pick<Location, 'origin'>

export function resolveOidcConfig(
  runtime: RuntimeConfig,
  location: LocationOrigin
): OidcConfig {
  const authority = normalize(runtime.oidcAuthority).replace(/\/$/, '')
  const clientId = normalize(runtime.oidcClientId)
  if (!authority && !clientId) {
    return { enabled: false }
  }
  if (!authority || !clientId) {
    throw new Error('OIDC authority 和 client ID 必须同时配置。')
  }
  return {
    enabled: true,
    authority,
    clientId,
    scope: normalize(runtime.oidcScope) || 'openid profile',
    redirectUri:
      normalize(runtime.oidcRedirectUri) || `${location.origin}/auth/callback`,
    postLogoutRedirectUri:
      normalize(runtime.oidcPostLogoutRedirectUri) || `${location.origin}/`
  }
}

export function isOidcCallback(search: string): boolean {
  const params = new URLSearchParams(search)
  return params.has('state') && (params.has('code') || params.has('error'))
}

export class OidcAuthController {
  readonly enabled: boolean
  private readonly manager: UserManager | null
  private readonly notify: (snapshot: AuthSnapshot) => void

  constructor(runtime: RuntimeConfig, notify: (snapshot: AuthSnapshot) => void) {
    this.notify = notify
    const config = resolveOidcConfig(runtime, window.location)
    this.enabled = config.enabled
    if (!config.enabled) {
      this.manager = null
      return
    }

    const sessionStore = new WebStorageStateStore({ store: window.sessionStorage })
    this.manager = new UserManager({
      authority: config.authority,
      client_id: config.clientId,
      redirect_uri: config.redirectUri,
      post_logout_redirect_uri: config.postLogoutRedirectUri,
      silent_redirect_uri: config.redirectUri,
      response_type: 'code',
      scope: config.scope,
      automaticSilentRenew: true,
      monitorSession: true,
      revokeTokensOnSignout: true,
      stateStore: sessionStore,
      userStore: sessionStore
    })
    this.manager.events.addUserLoaded((user) => this.publishUser(user))
    this.manager.events.addUserUnloaded(() => this.publishAnonymous())
    this.manager.events.addAccessTokenExpired(() => this.publishAnonymous())
    this.manager.events.addSilentRenewError((error) => {
      this.publishError(`登录续期失败：${error.message}`)
    })
  }

  async initialize(): Promise<void> {
    if (!this.manager) {
      this.notify(emptySnapshot('disabled'))
      return
    }
    this.notify(emptySnapshot('loading'))
    try {
      if (isOidcCallback(window.location.search)) {
        const user = await this.manager.signinCallback(window.location.href)
        if (window.self !== window.top) {
          return
        }
        window.history.replaceState({}, document.title, '/')
        if (user) {
          this.publishUser(user)
          return
        }
      }
      const user = await this.manager.getUser()
      if (user && !user.expired) {
        this.publishUser(user)
        return
      }
      if (user) {
        try {
          const renewed = await this.manager.signinSilent()
          if (renewed) {
            this.publishUser(renewed)
          } else {
            this.publishAnonymous()
          }
          return
        } catch {
          await this.manager.removeUser()
        }
      }
      this.publishAnonymous()
    } catch (error) {
      this.publishError(errorMessage(error))
    }
  }

  async login(): Promise<void> {
    if (!this.manager) {
      return
    }
    await this.manager.signinRedirect({ state: { returnUrl: window.location.pathname } })
  }

  async logout(): Promise<void> {
    if (!this.manager) {
      return
    }
    await this.manager.signoutRedirect()
  }

  private publishUser(user: User): void {
    const profile = user.profile as Record<string, unknown>
    this.notify({
      status: 'authenticated',
      accessToken: user.access_token,
      tenantId: claim(profile, 'tenant_id'),
      userId: claim(profile, 'sub'),
      displayName:
        claim(profile, 'name') ||
        claim(profile, 'preferred_username') ||
        claim(profile, 'email') ||
        claim(profile, 'sub'),
      error: ''
    })
  }

  private publishAnonymous(): void {
    this.notify(emptySnapshot('anonymous'))
  }

  private publishError(message: string): void {
    this.notify({ ...emptySnapshot('error'), error: message })
  }
}

function emptySnapshot(status: AuthSnapshot['status']): AuthSnapshot {
  return {
    status,
    accessToken: '',
    tenantId: '',
    userId: '',
    displayName: '',
    error: ''
  }
}

function claim(profile: Record<string, unknown>, name: string): string {
  const value = profile[name]
  return typeof value === 'string' ? value.trim() : ''
}

function normalize(value: string | undefined): string {
  return value?.trim() ?? ''
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'OIDC 登录失败。'
}
