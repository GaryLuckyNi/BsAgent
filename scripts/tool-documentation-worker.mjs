import { spawn } from 'node:child_process'
import process from 'node:process'
import { loadLocalEnvironment } from './local-env.mjs'

loadLocalEnvironment()
const child = spawn(process.env.PYTHON || '.venv/bin/python', [
  '-m', 'celery', '-A', 'app.worker.celery_app:celery_app', 'worker',
  '--loglevel=INFO', '--queues', 'tool-documentation', '--concurrency', '2',
  '--hostname', 'tool-documentation@%h'
], {
  cwd: process.cwd(),
  env: {
    ...process.env,
    PYTHONPATH: 'backend',
    BS_AGENT_DATABASE_URL: process.env.BS_AGENT_DATABASE_URL ||
      'postgresql+psycopg://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent',
    BS_AGENT_ADMIN_BASE_URL: process.env.BS_AGENT_ADMIN_BASE_URL || 'http://127.0.0.1:8002'
  },
  stdio: 'inherit'
})
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal))
}
child.on('error', (error) => {
  console.error(`文档补全 worker 启动失败：${error.message}`)
  process.exitCode = 1
})
child.on('exit', (code) => { process.exitCode = code ?? 1 })
