import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { OidcAuthController, type AuthSnapshot } from '../auth'
import type { RequestIdentity } from '../services/research-api'

const localToken = normalizeToken(import.meta.env.VITE_AGENT_ACCESS_TOKEN)
const localSnapshot: AuthSnapshot = {
  status: 'loading',
  accessToken: localToken ?? '',
  tenantId: 'tenant_1',
  userId: 'user_1',
  displayName: localToken ? '本地用户' : '投资者',
  error: ''
}

export function useAuthentication() {
  const controllerRef = useRef<OidcAuthController | null>(null)
  const [snapshot, setSnapshot] = useState(localSnapshot)
  const [enabled, setEnabled] = useState(false)

  useEffect(() => {
    if (controllerRef.current) return
    const controller = new OidcAuthController(
      window.__BS_AGENT_RUNTIME_CONFIG__ ?? {},
      (next) => {
        if (next.status === 'disabled') {
          setSnapshot({ ...localSnapshot, status: 'disabled' })
          return
        }
        setSnapshot(next)
      }
    )
    controllerRef.current = controller
    setEnabled(controller.enabled)
    void controller.initialize()
  }, [])

  const identity = useMemo<RequestIdentity>(() => ({
    tenantId: snapshot.tenantId || 'tenant_1',
    userId: snapshot.userId || 'user_1',
    accessToken: snapshot.accessToken || undefined
  }), [snapshot.accessToken, snapshot.tenantId, snapshot.userId])

  const toggleAuthentication = useCallback(async () => {
    if (!enabled) return
    if (snapshot.status === 'authenticated') await controllerRef.current?.logout()
    else await controllerRef.current?.login()
  }, [enabled, snapshot.status])

  return {
    identity,
    enabled,
    displayName: snapshot.displayName || '投资者',
    status: snapshot.status,
    error: snapshot.error,
    toggleAuthentication
  }
}

function normalizeToken(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}
