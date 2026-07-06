import { http } from './client'
import type { Page, SkillProfile } from './types'

export async function listSkills(q = '') {
  const response = await http.get<Page<SkillProfile>>('/api/skills', {
    params: q ? { q } : {}
  })
  return response.data
}

export async function createSkill(payload: SkillProfile) {
  const response = await http.post<SkillProfile>('/api/skills', payload)
  return response.data
}

export async function updateSkill(name: string, payload: Partial<SkillProfile>) {
  const response = await http.put<SkillProfile>(`/api/skills/${name}`, payload)
  return response.data
}

export async function deleteSkill(name: string) {
  await http.delete(`/api/skills/${name}`)
}
