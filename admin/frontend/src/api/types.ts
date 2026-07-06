export type RiskLevel = 'low' | 'medium' | 'high'

export type IntentKind =
  | 'knowledge_qa'
  | 'memory_query'
  | 'tool_action'
  | 'task_workflow'
  | 'conversation'
  | 'clarification_needed'
  | 'unsupported'

export interface BusinessIntentProfile {
  id: string
  intent: IntentKind
  domain: string
  sub_intent: string
  candidate_skill?: string | null
  risk_level: RiskLevel
  trigger_keywords: string[]
  trigger_examples: string[]
  required_capabilities: string[]
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
  allowed_tools: string[]
  risk_level: RiskLevel
  enabled: boolean
  created_at?: string
  updated_at?: string
}

export interface Page<T> {
  items: T[]
  total: number
}
