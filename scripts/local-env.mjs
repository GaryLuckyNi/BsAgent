import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import process from 'node:process'

export function loadLocalEnvironment(file = resolve('.env.local')) {
  if (!existsSync(file)) return false
  process.loadEnvFile(file)
  return true
}
