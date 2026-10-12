export type RiskLevel = 'low' | 'medium' | 'high'

export interface BusinessIntentProfile {
  id: string
  intent_type: string
  domain: string
  subdomain: string
  trigger_keywords: string[]
  trigger_examples: string[]
  enabled: boolean
  description?: string | null
  created_at?: string
  updated_at?: string
}

export interface SkillProfile {
  name: string
  version: string
  summary: string
  supported_intents: string[]
  supported_domains: string[]
  supported_sub_intents: string[]
  risk_level: RiskLevel
  enabled: boolean
  created_at?: string
  updated_at?: string
}

export type MarketDataProviderName = 'tushare'

export interface TushareProviderSettings {
  mcp_server_url?: string
  [key: string]: unknown
}

export interface MarketDataProviderConfig {
  id: string
  provider: MarketDataProviderName
  display_name: string
  enabled: boolean
  active: boolean
  settings: TushareProviderSettings
  description?: string | null
  created_at?: string
  updated_at?: string
}

export interface Page<T> {
  items: T[]
  total: number
}
