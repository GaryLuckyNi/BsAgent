import { Bell, Bot, BriefcaseBusiness, FileText, LineChart, Star } from 'lucide-react'
import { NavLink } from 'react-router-dom'

const navigationItems = [
  { label: '市场', icon: LineChart, disabled: true },
  { label: '自选', icon: Star, disabled: true },
  { label: '资讯', icon: FileText, disabled: true },
  { label: '持仓', icon: BriefcaseBusiness, disabled: true },
  { label: '策略与提醒', icon: Bell, disabled: true },
  { label: 'AI 研究', icon: Bot, to: '/ai-research' }
] as const

export function MainNavigation() {
  return (
    <nav className="main-nav" aria-label="主导航">
      {navigationItems.map(({ label, icon: Icon, ...item }) => {
        if ('disabled' in item) {
          return <button key={label} type="button" disabled title="功能规划中"><Icon size={16} />{label}</button>
        }
        return <NavLink key={label} to={item.to} className={({ isActive }) => isActive ? 'active' : undefined}><Icon size={16} />{label}</NavLink>
      })}
    </nav>
  )
}
