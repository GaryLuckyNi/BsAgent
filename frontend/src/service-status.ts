export type ServiceHealth = 'checking' | 'online' | 'offline'


export function invocationBlockedReason(health: ServiceHealth): string | null {
  if (health === 'online') {
    return null
  }
  if (health === 'checking') {
    return 'Agent 服务正在检查状态，请稍后再试。'
  }
  return 'Agent 服务未启动或不可达，请先启动后端服务。'
}
