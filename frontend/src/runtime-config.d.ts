import type { RuntimeConfig } from './auth'

declare global {
  interface Window {
    __BS_AGENT_RUNTIME_CONFIG__?: RuntimeConfig
  }
}

export {}
