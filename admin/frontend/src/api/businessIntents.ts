import { http } from './client'
import type { BusinessIntentProfile, Page } from './types'

export async function listBusinessIntents(q = '') {
  const response = await http.get<Page<BusinessIntentProfile>>('/api/business-intents', {
    params: q ? { q } : {}
  })
  return response.data
}

export async function createBusinessIntent(payload: BusinessIntentProfile) {
  const response = await http.post<BusinessIntentProfile>('/api/business-intents', payload)
  return response.data
}

export async function updateBusinessIntent(id: string, payload: Partial<BusinessIntentProfile>) {
  const response = await http.put<BusinessIntentProfile>(`/api/business-intents/${id}`, payload)
  return response.data
}

export async function deleteBusinessIntent(id: string) {
  await http.delete(`/api/business-intents/${id}`)
}
