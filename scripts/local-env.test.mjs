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

test('returns false when the local environment file is absent', () => {
  withCleanEnvironment(() => {
    assert.equal(loadLocalEnvironment('/path/that/does/not/exist'), false)
    assert.equal(process.env.BS_AGENT_LANGFUSE_PUBLIC_KEY, undefined)
  })
})

test('loads values from a local environment file', () => {
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

test('does not override a value provided by the shell', () => {
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
