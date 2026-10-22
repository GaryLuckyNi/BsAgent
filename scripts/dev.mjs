import net from 'node:net'
import { execFile, spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import process from 'node:process'
import { promisify } from 'node:util'

import {
  isLangfuseRegistryNetworkFailure,
  parseEnvironmentFile
} from './langfuse-dev.mjs'
import { loadLocalEnvironment } from './local-env.mjs'

loadLocalEnvironment()

const execFileAsync = promisify(execFile)
const python = process.env.PYTHON || '/opt/anaconda3/bin/python3'
const host = process.env.BS_AGENT_DEV_HOST || '0.0.0.0'
const localPostgresComposeFile = 'compose.local-postgres.yml'
const langfuseComposeFile = 'langfuse/compose.yaml'
const langfuseEnvFile = 'langfuse/.env'
const localPostgresUrl =
  'postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent'
const backendDatabaseUrl =
  process.env.BS_AGENT_DATABASE_URL ||
  'postgresql+psycopg://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent'
const graphCheckpointPostgresUrl =
  process.env.BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL || localPostgresUrl
const memoryStorePostgresUrl =
  process.env.BS_AGENT_MEMORY_STORE_POSTGRES_URL || localPostgresUrl
const genericMcpUrl = process.env.BS_AGENT_MCP_SERVER_URL?.trim() || ''
const ports = {
  backend: Number(process.env.BS_AGENT_BACKEND_PORT || 8000),
  frontend: Number(process.env.BS_AGENT_FRONTEND_PORT || 5175),
  mcp: Number(process.env.BS_AGENT_MCP_PORT || 8011),
  'admin-backend': Number(process.env.BS_AGENT_ADMIN_BACKEND_PORT || 8002),
  'admin-frontend': Number(process.env.BS_AGENT_ADMIN_FRONTEND_PORT || 5173)
}

const services = {
  backend: {
    label: 'backend',
    command: python,
    args: [
      '-m',
      'uvicorn',
      'app.main:app',
      '--host',
      host,
      '--port',
      String(ports.backend),
      '--reload'
    ],
    cwd: 'backend',
    env: {
      BS_AGENT_DATABASE_URL: backendDatabaseUrl,
      BS_AGENT_RECOVER_ORPHANED_RUNS_ON_STARTUP: 'true',
      BS_AGENT_PREWARM_TOOL_CATALOG_ON_STARTUP: 'true',
      BS_AGENT_GRAPH_CHECKPOINTER_BACKEND: 'postgres',
      BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL: graphCheckpointPostgresUrl,
      BS_AGENT_MEMORY_STORE_POSTGRES_URL: memoryStorePostgresUrl,
      BS_AGENT_ADMIN_BASE_URL: `http://127.0.0.1:${ports['admin-backend']}`,
      BS_AGENT_SKILL_REGISTRY_BASE_URL: `http://127.0.0.1:${ports['admin-backend']}`,
      BS_AGENT_SKILL_DEFINITION_CACHE_TTL_SECONDS: '5',
      BS_AGENT_MCP_SERVER_NAME: process.env.BS_AGENT_MCP_SERVER_NAME || 'agent-mcp',
      BS_AGENT_MCP_SERVER_URL: genericMcpUrl,
      BS_AGENT_MCP_SERVER_REVISION:
        process.env.BS_AGENT_MCP_SERVER_REVISION || 'local-dev',
      BS_AGENT_MCP_CONFIG_REVISION:
        process.env.BS_AGENT_MCP_CONFIG_REVISION || 'default'
    },
    port: ports.backend,
    startupTasks: [
      {
        label: 'langfuse-init',
        command: python,
        args: ['langfuse/init.py'],
        cwd: '.'
      },
      {
        label: 'langfuse',
        command: 'docker',
        args: [
          'compose',
          '--env-file',
          langfuseEnvFile,
          '-f',
          langfuseComposeFile,
          'up',
          '-d',
          '--wait'
        ],
        cwd: '.'
      },
      {
        label: 'local-postgres',
        command: 'docker',
        args: [
          'compose',
          '-f',
          localPostgresComposeFile,
          'up',
          '-d',
          '--wait',
          'postgres'
        ],
        cwd: '.'
      },
      {
        label: 'backend-migration',
        command: python,
        args: ['-m', 'alembic', 'upgrade', 'head'],
        cwd: '.',
        env: {
          BS_AGENT_DATABASE_URL: backendDatabaseUrl
        }
      }
    ]
  },
  mcp: {
    label: 'mcp',
    command: python,
    args: ['-m', 'app.main'],
    cwd: 'mcp server',
    env: {
      MCP_SERVER_HOST: host,
      MCP_SERVER_PORT: String(ports.mcp),
      MCP_SERVER_PATH: '/mcp',
      MCP_MARKET_DATA_ADMIN_BASE_URL: '',
      MCP_MARKET_DATA_PROVIDER: process.env.MCP_MARKET_DATA_PROVIDER || 'tushare'
    },
    port: ports.mcp
  },
  frontend: {
    label: 'frontend',
    command: 'npm',
    args: [
      '--prefix',
      'frontend',
      'run',
      'dev',
      '--',
      '--host',
      host,
      '--port',
      String(ports.frontend),
      '--strictPort'
    ],
    cwd: '.',
    env: {},
    port: ports.frontend
  },
  'admin-backend': {
    label: 'admin-backend',
    command: python,
    args: [
      '-m',
      'uvicorn',
      'app.main:app',
      '--host',
      host,
      '--port',
      String(ports['admin-backend']),
      '--reload'
    ],
    cwd: 'admin/backend',
    env: {
      ADMIN_DATABASE_URL: 'sqlite:///./admin_config.db',
      ADMIN_MCP_SERVER_NAME: 'local'
    },
    port: ports['admin-backend'],
    startupTasks: [
      {
        label: 'admin-migration',
        command: python,
        args: ['-m', 'alembic', 'upgrade', 'head'],
        cwd: 'admin/backend',
        env: {
          ADMIN_DATABASE_URL: 'sqlite:///./admin_config.db',
          ADMIN_MCP_SERVER_NAME: 'local'
        }
      }
    ]
  },
  'admin-frontend': {
    label: 'admin-frontend',
    command: 'npm',
    args: [
      '--prefix',
      'admin/frontend',
      'run',
      'dev',
      '--',
      '--host',
      host,
      '--port',
      String(ports['admin-frontend']),
      '--strictPort'
    ],
    cwd: '.',
    env: {},
    port: ports['admin-frontend']
  }
}

const defaultServiceNames = ['admin-backend', 'backend', 'frontend', 'admin-frontend']
const defaultStopServiceNames = ['mcp', ...defaultServiceNames]
const rawArgs = process.argv.slice(2)
const checkOnly = rawArgs.includes('--check')
const stopOnly = rawArgs.includes('--stop')
const dryRun = rawArgs.includes('--dry-run')
const args = rawArgs.filter(
  (arg) => !['--check', '--stop', '--dry-run'].includes(arg)
)

if (args.includes('--help') || args.includes('-h')) {
  printHelp()
  process.exit(0)
}

const selectedServiceNames =
  args.length > 0
    ? args
    : stopOnly
      ? defaultStopServiceNames
      : defaultServiceNames
const unknown = selectedServiceNames.filter((name) => !services[name])

if (unknown.length > 0) {
  console.error(`未知服务: ${unknown.join(', ')}`)
  printHelp()
  process.exit(1)
}

const selectedServices = selectedServiceNames.map((name) => services[name])

if (stopOnly) {
  let exitCode = await stopServices(selectedServices, { dryRun })
  if (
    exitCode === 0 &&
    selectedServices.some((service) => service.label === 'backend')
  ) {
    const postgresExitCode = await stopLocalPostgres({ dryRun })
    const langfuseExitCode = await stopLocalLangfuse({ dryRun })
    exitCode = Math.max(postgresExitCode, langfuseExitCode)
  }
  process.exit(exitCode)
}

validateRequiredEnvironment(selectedServices)

const portConflicts = await findPortConflicts(selectedServices)

if (portConflicts.length > 0) {
  console.error('端口预检失败，以下服务端口已被占用或不可监听：')
  for (const conflict of portConflicts) {
    console.error(`  - ${conflict.label}: ${host}:${conflict.port} (${conflict.reason})`)
  }
  console.error('\n处理方式：')
  console.error('  1. 停掉占用这些端口的旧服务后重新执行 npm run dev')
  console.error('  2. 或用环境变量改端口，例如：')
  console.error('     BS_AGENT_ADMIN_BACKEND_PORT=8012 npm run dev')
  process.exit(1)
}

if (checkOnly) {
  console.log('端口预检通过。')
  for (const service of selectedServices) {
    console.log(`  - ${service.label}: ${host}:${service.port}`)
  }
  process.exit(0)
}

const startupFailures = await runStartupTasks(selectedServices)

if (
  !configureLangfuseBackendEnvironment(selectedServices, {
    available: !startupFailures.has('langfuse')
  })
) {
  process.exit(1)
}

const children = selectedServices.map((service) => startService(service))

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    stopAll(signal)
  })
}

function startService(service) {
  const child = spawn(service.command, service.args, {
    cwd: service.cwd,
    env: {
      ...process.env,
      ...service.env
    },
    stdio: ['ignore', 'pipe', 'pipe']
  })

  pipeOutput(service.label, child.stdout)
  pipeOutput(service.label, child.stderr)

  child.on('exit', (code, signal) => {
    if (signal) {
      return
    }
    if (code !== 0) {
      console.error(`[${service.label}] 已退出，code=${code}`)
      stopAll('SIGTERM')
      process.exitCode = code ?? 1
    }
  })

  console.log(`[${service.label}] 启动: ${service.command} ${service.args.join(' ')}`)
  return child
}

function pipeOutput(label, stream) {
  stream.setEncoding('utf8')
  let buffer = ''
  stream.on('data', (chunk) => {
    buffer += chunk
    const lines = buffer.split(/\r?\n/)
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (line.trim()) {
        console.log(`[${label}] ${line}`)
      }
    }
  })
}

function stopAll(signal) {
  for (const child of children) {
    if (!child.killed) {
      child.kill(signal)
    }
  }
}

async function stopServices(selectedServices, { dryRun }) {
  let stoppedCount = 0
  for (const service of selectedServices) {
    const pids = await listenPids(service.port)
    if (pids.length === 0) {
      console.log(`[${service.label}] 未发现监听 ${service.port} 的进程`)
      continue
    }
    for (const pid of pids) {
      if (dryRun) {
        console.log(`[${service.label}] 将关闭 PID ${pid} (${service.port})`)
        continue
      }
      try {
        process.kill(pid, 'SIGTERM')
        stoppedCount += 1
        console.log(`[${service.label}] 已发送 SIGTERM 到 PID ${pid} (${service.port})`)
      } catch (error) {
        console.error(`[${service.label}] 关闭 PID ${pid} 失败: ${error.message}`)
        return 1
      }
    }
  }
  if (dryRun) {
    console.log('dry-run 完成，未关闭任何进程。')
    return 0
  }
  console.log(`关闭命令已发送，共 ${stoppedCount} 个进程。`)
  return 0
}

async function stopLocalPostgres({ dryRun }) {
  if (dryRun) {
    console.log('[local-postgres] 将停止 compose.local-postgres.yml 中的 postgres 服务')
    return 0
  }
  try {
    const { stdout, stderr } = await execFileAsync('docker', [
      'compose',
      '-f',
      localPostgresComposeFile,
      'stop',
      'postgres'
    ])
    if (stdout.trim()) {
      console.log(`[local-postgres] ${stdout.trim()}`)
    }
    if (stderr.trim()) {
      console.log(`[local-postgres] ${stderr.trim()}`)
    }
    return 0
  } catch (error) {
    console.error(`[local-postgres] 停止失败: ${error.message}`)
    return 1
  }
}

async function stopLocalLangfuse({ dryRun }) {
  if (dryRun) {
    console.log(`[langfuse] 将停止 ${langfuseComposeFile} 中的本地服务`)
    return 0
  }
  if (!existsSync(langfuseEnvFile)) {
    console.log(`[langfuse] 未找到 ${langfuseEnvFile}，无需停止`)
    return 0
  }
  try {
    const { stdout, stderr } = await execFileAsync('docker', [
      'compose',
      '--env-file',
      langfuseEnvFile,
      '-f',
      langfuseComposeFile,
      'stop'
    ])
    if (stdout.trim()) {
      console.log(`[langfuse] ${stdout.trim()}`)
    }
    if (stderr.trim()) {
      console.log(`[langfuse] ${stderr.trim()}`)
    }
    return 0
  } catch (error) {
    console.error(`[langfuse] 停止失败: ${error.message}`)
    return 1
  }
}

function configureLangfuseBackendEnvironment(
  selectedServices,
  { available = true } = {}
) {
  const needsBackend = selectedServices.some(
    (service) => service.label === 'backend'
  )
  if (!needsBackend) {
    return true
  }
  if (!available) {
    process.env.BS_AGENT_LANGFUSE_ENABLED = 'false'
    console.warn('[langfuse] Backend 将在本次开发会话中禁用 Langfuse 上报')
    return true
  }
  if (!existsSync(langfuseEnvFile)) {
    console.error(`[langfuse] 未找到 ${langfuseEnvFile}，无法为 Backend 配置本地观测`)
    return false
  }

  const values = parseEnvironmentFile(langfuseEnvFile)
  const publicKey =
    process.env.BS_AGENT_LANGFUSE_PUBLIC_KEY?.trim() ||
    values.LANGFUSE_INIT_PROJECT_PUBLIC_KEY
  const secretKey =
    process.env.BS_AGENT_LANGFUSE_SECRET_KEY?.trim() ||
    values.LANGFUSE_INIT_PROJECT_SECRET_KEY
  if (!publicKey || !secretKey) {
    console.error('[langfuse] 本地项目 key 不完整，请重新执行 python langfuse/init.py')
    return false
  }

  setEnvironmentDefault('BS_AGENT_LANGFUSE_ENABLED', 'true')
  setEnvironmentDefault('BS_AGENT_LANGFUSE_BASE_URL', 'http://localhost:3000')
  setEnvironmentDefault('BS_AGENT_LANGFUSE_PUBLIC_KEY', publicKey)
  setEnvironmentDefault('BS_AGENT_LANGFUSE_SECRET_KEY', secretKey)
  return true
}

function setEnvironmentDefault(name, value) {
  if (!process.env[name]?.trim()) {
    process.env[name] = value
  }
}

function validateRequiredEnvironment(selectedServices) {
  const needsBackend = selectedServices.some((service) => service.label === 'backend')
  const needsLocalMcp = selectedServices.some((service) => service.label === 'mcp')
  const missing = []
  if (needsBackend && !process.env.BS_AGENT_DEEPSEEK_API_KEY?.trim()) {
    missing.push('BS_AGENT_DEEPSEEK_API_KEY')
  }
  if (needsLocalMcp && !process.env.TUSHARE_TOKEN?.trim()) {
    missing.push('TUSHARE_TOKEN')
  }
  if (needsLocalMcp && !process.env.MCP_TUSHARE_ENABLED_API_NAMES?.trim()) {
    missing.push('MCP_TUSHARE_ENABLED_API_NAMES')
  }
  if (missing.length === 0) {
    return
  }
  console.error(`本地开发缺少必需环境变量：${missing.join('、')}`)
  console.error('\n请在仓库根目录的 .env.local 中填写：')
  if (missing.includes('BS_AGENT_DEEPSEEK_API_KEY')) {
    console.error('  BS_AGENT_DEEPSEEK_API_KEY=你的DeepSeekApiKey')
  }
  if (missing.includes('TUSHARE_TOKEN')) {
    console.error('  TUSHARE_TOKEN=你的TushareToken')
  }
  if (missing.includes('MCP_TUSHARE_ENABLED_API_NAMES')) {
    console.error('  MCP_TUSHARE_ENABLED_API_NAMES=stock_basic,daily,daily_basic')
  }
  console.error('\n然后重新执行当前启动命令。shell 中已 export 的变量仍会优先于文件。')
  console.error('\n如果只启动 Admin 或前端，可使用 npm run dev:admin 或 npm run dev:frontend。')
  process.exit(1)
}

async function runStartupTasks(selectedServices) {
  const tasks = selectedServices.flatMap((service) => service.startupTasks ?? [])
  const failedOptionalTasks = new Set()
  for (const task of tasks) {
    console.log(`[${task.label}] 执行: ${task.command} ${task.args.join(' ')}`)
    try {
      const { stdout, stderr } = await execFileAsync(task.command, task.args, {
        cwd: task.cwd,
        env: {
          ...process.env,
          ...task.env
        }
      })
      if (stdout.trim()) {
        console.log(`[${task.label}] ${stdout.trim()}`)
      }
      if (stderr.trim()) {
        console.log(`[${task.label}] ${stderr.trim()}`)
      }
    } catch (error) {
      if (error.stdout?.trim()) {
        console.error(`[${task.label}] ${error.stdout.trim()}`)
      }
      if (error.stderr?.trim()) {
        console.error(`[${task.label}] ${error.stderr.trim()}`)
      }
      console.error(`[${task.label}] 启动前置任务失败。`)
      if (
        task.label === 'langfuse' &&
        !environmentFlagEnabled('BS_AGENT_DEV_LANGFUSE_REQUIRED') &&
        isLangfuseRegistryNetworkFailure(error)
      ) {
        failedOptionalTasks.add(task.label)
        console.warn(
          '[langfuse] 镜像仓库网络不可用，将继续启动其他开发服务。'
        )
        console.warn(
          '[langfuse] 网络恢复后重新运行 npm run dev；如需严格启动，设置 BS_AGENT_DEV_LANGFUSE_REQUIRED=true。'
        )
        continue
      }
      process.exit(1)
    }
  }
  return failedOptionalTasks
}

function environmentFlagEnabled(name) {
  return ['1', 'true', 'yes', 'on'].includes(
    process.env[name]?.trim().toLowerCase()
  )
}

async function listenPids(port) {
  try {
    const { stdout } = await execFileAsync('lsof', [
      '-nP',
      `-iTCP:${port}`,
      '-sTCP:LISTEN',
      '-t'
    ])
    return stdout
      .split(/\r?\n/)
      .map((line) => Number(line.trim()))
      .filter((pid) => Number.isInteger(pid) && pid > 0)
  } catch (error) {
    if (error.code === 1) {
      return []
    }
    throw error
  }
}

async function findPortConflicts(selectedServices) {
  const checks = await Promise.all(
    selectedServices.map(async (service) => ({
      label: service.label,
      port: service.port,
      reason: await unavailableReason(service.port)
    }))
  )
  return checks.filter((check) => check.reason !== null)
}

function unavailableReason(port) {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', (error) => {
      if (error.code === 'EADDRINUSE') {
        resolve('端口已被占用')
        return
      }
      resolve(error.message)
    })
    server.once('listening', () => {
      server.close(() => resolve(null))
    })
    server.listen({ host, port })
  })
}

function printHelp() {
  console.log(`
BsAgent 本地开发启动器

用法:
  npm run dev
  npm run dev:backend
  npm run dev:frontend
  npm run dev:admin
  npm run dev:admin:backend
  npm run dev:admin:frontend
  node scripts/dev.mjs backend frontend
  node scripts/dev.mjs --check
  node scripts/dev.mjs --stop
  node scripts/dev.mjs --stop --dry-run

默认端口:
  backend         http://127.0.0.1:${ports.backend}
  mcp（仅学习）   http://127.0.0.1:${ports.mcp}/mcp
  frontend        http://127.0.0.1:${ports.frontend}
  admin-backend   http://127.0.0.1:${ports['admin-backend']}
  admin-frontend  http://127.0.0.1:${ports['admin-frontend']}
  langfuse        http://127.0.0.1:3000

环境变量:
  PYTHON          Python 解释器路径，默认 /opt/anaconda3/bin/python3
  BS_AGENT_DEV_HOST
  BS_AGENT_BACKEND_PORT
  BS_AGENT_MCP_PORT
  BS_AGENT_FRONTEND_PORT
  BS_AGENT_ADMIN_BACKEND_PORT
  BS_AGENT_ADMIN_FRONTEND_PORT
  BS_AGENT_DEEPSEEK_API_KEY
  TUSHARE_TOKEN                  仅 dev:mcp 学习示例需要
  MCP_TUSHARE_ENABLED_API_NAMES  仅 dev:mcp 学习示例需要
  BS_AGENT_MCP_SERVER_URL        可选的通用 Agent MCP 地址
  BS_AGENT_DATABASE_URL
  BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL
  BS_AGENT_MEMORY_STORE_POSTGRES_URL
  BS_AGENT_LANGFUSE_ENABLED
  BS_AGENT_LANGFUSE_BASE_URL
  BS_AGENT_LANGFUSE_PUBLIC_KEY
  BS_AGENT_LANGFUSE_SECRET_KEY
  BS_AGENT_DEV_LANGFUSE_REQUIRED    设为 true 时 Langfuse 启动失败会终止 dev

启动器会自动设置:
  BS_AGENT_DATABASE_URL=postgresql+psycopg://...@127.0.0.1:55432/bs_agent
  BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL=postgresql://...@127.0.0.1:55432/bs_agent
  BS_AGENT_MEMORY_STORE_POSTGRES_URL=postgresql://...@127.0.0.1:55432/bs_agent
  BS_AGENT_ADMIN_BASE_URL=http://127.0.0.1:<admin-backend-port>
  BS_AGENT_SKILL_REGISTRY_BASE_URL=http://127.0.0.1:<admin-backend-port>
  BS_AGENT_MCP_SERVER_NAME=agent-mcp
  BS_AGENT_LANGFUSE_ENABLED=true
  BS_AGENT_LANGFUSE_BASE_URL=http://localhost:3000
  BS_AGENT_LANGFUSE_PUBLIC_KEY/SECRET_KEY=来自 langfuse/.env（不输出密钥）

Tushare MCP 完整地址请在 Admin 的“市场数据源”页面配置。
`)
}
