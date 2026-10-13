import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { loadLocalEnvironment } from './local-env.mjs'

const managedKeys = [
  'BS_AGENT_LANGFUSE_ENABLED',
  'BS_AGENT_LANGFUSE_PUBLIC_KEY'
]

function withCleanEnvironment(callback) {
  const previous = Object.fromEntries(
    managedKeys.map((key) => [key, process.env[key]])
  )
  for (const key of managedKeys) delete process.env[key]
  try {
    callback()
  } finally {
    for (const key of managedKeys) {
      if (previous[key] === undefined) delete process.env[key]
      else process.env[key] = previous[key]
    }
  }
}

test('本地环境文件不存在时返回 false', () => {
  withCleanEnvironment(() => {
    assert.equal(loadLocalEnvironment('/path/that/does/not/exist'), false)
    assert.equal(process.env.BS_AGENT_LANGFUSE_PUBLIC_KEY, undefined)
  })
})

test('加载本地环境文件', () => {
  withCleanEnvironment(() => {
    const directory = mkdtempSync(join(tmpdir(), 'bs-agent-env-'))
    const envFile = join(directory, '.env.local')
    writeFileSync(
      envFile,
      'BS_AGENT_LANGFUSE_ENABLED=true\nBS_AGENT_LANGFUSE_PUBLIC_KEY=pk-local\n'
    )
    try {
      assert.equal(loadLocalEnvironment(envFile), true)
      assert.equal(process.env.BS_AGENT_LANGFUSE_ENABLED, 'true')
      assert.equal(process.env.BS_AGENT_LANGFUSE_PUBLIC_KEY, 'pk-local')
    } finally {
      rmSync(directory, { recursive: true, force: true })
    }
  })
})

test('本地环境文件不覆盖 shell 变量', () => {
  withCleanEnvironment(() => {
    const directory = mkdtempSync(join(tmpdir(), 'bs-agent-env-'))
    const envFile = join(directory, '.env.local')
    writeFileSync(envFile, 'BS_AGENT_LANGFUSE_ENABLED=from-file\n')
    process.env.BS_AGENT_LANGFUSE_ENABLED = 'from-shell'
    try {
      loadLocalEnvironment(envFile)
      assert.equal(process.env.BS_AGENT_LANGFUSE_ENABLED, 'from-shell')
    } finally {
      rmSync(directory, { recursive: true, force: true })
    }
  })
})
