import { Link } from 'react-router-dom'

export function RouteErrorView() {
  return (
    <main className="route-error">
      <p>404</p>
      <h1>页面未找到</h1>
      <span>该页面尚未开放，或访问地址已变更。</span>
      <Link to="/ai-research">返回 AI 研究</Link>
    </main>
  )
}
