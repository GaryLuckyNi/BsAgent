import assert from 'node:assert/strict'
import test from 'node:test'

import { isLangfuseRegistryNetworkFailure } from './langfuse-dev.mjs'

test('recognizes the Docker Hub EOF that blocks Langfuse image pulls', () => {
  const error = {
    stderr:
      'Error response from daemon: Get "https://registry-1.docker.io/v2/": EOF'
  }

  assert.equal(isLangfuseRegistryNetworkFailure(error), true)
})

test('recognizes registry DNS and TLS timeouts', () => {
  assert.equal(
    isLangfuseRegistryNetworkFailure({
      message:
        'Head "https://docker.langfuse.com/v2/": net/http: TLS handshake timeout'
    }),
    true
  )
  assert.equal(
    isLangfuseRegistryNetworkFailure({
      stderr: 'lookup cgr.dev: no such host'
    }),
    true
  )
})

test('does not hide compose configuration or authentication errors', () => {
  assert.equal(
    isLangfuseRegistryNetworkFailure({
      stderr: 'services.langfuse-web.environment must be a mapping'
    }),
    false
  )
  assert.equal(
    isLangfuseRegistryNetworkFailure({
      stderr: 'unauthorized: authentication required for registry-1.docker.io'
    }),
    false
  )
})
