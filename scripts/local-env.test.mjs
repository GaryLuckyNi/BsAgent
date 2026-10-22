import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { loadLocalEnvironment } from './local-env.mjs'

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)))

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

test('the local environment template includes the required backend key', () => {
  const template = readFileSync(join(projectRoot, '.env.example'), 'utf8')

  assert.match(template, /^BS_AGENT_DEEPSEEK_API_KEY=$/m)
})

test('a missing backend key points users to the local environment file', () => {
  const directory = mkdtempSync(join(tmpdir(), 'bs-agent-env-'))
  const env = { ...process.env }
  delete env.BS_AGENT_DEEPSEEK_API_KEY

  try {
    assert.throws(
      () =>
        execFileSync(
          process.execPath,
          [join(projectRoot, 'scripts/dev.mjs'), 'backend'],
          { cwd: directory, env, encoding: 'utf8', stdio: 'pipe' }
        ),
      (error) => {
        assert.match(error.stderr, /\.env\.local/)
        assert.match(error.stderr, /BS_AGENT_DEEPSEEK_API_KEY=/)
        return true
      }
    )
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
})
