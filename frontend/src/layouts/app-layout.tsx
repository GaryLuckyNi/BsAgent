import { BarChart3, Bell, ChevronDown, CircleUserRound } from 'lucide-react'
import { Outlet } from 'react-router-dom'

import { MainNavigation } from '@/components/navigation/main-navigation'
import { TooltipProvider } from '@/components/ui/tooltip'
import { useAuthentication } from '@/hooks/use-authentication'

export type AppOutletContext = {
  authentication: ReturnType<typeof useAuthentication>
}

export function AppLayout() {
  const authentication = useAuthentication()

  return (
    <TooltipProvider>
      <div className="app-shell">
        <header className="topbar">
          <div className="brand">
            <span className="brand-mark"><BarChart3 size={20} /></span>
            <b>InvestWise</b>
            <i>智能投资研究平台</i>
          </div>
          <MainNavigation />
          <div className="top-actions">
            <button type="button" aria-label="通知"><Bell size={18} /></button>
            <button
              className="profile"
              type="button"
              onClick={() => void authentication.toggleAuthentication()}
              title={authentication.enabled ? '点击登录或退出' : undefined}
            >
              <CircleUserRound size={19} />
              <span>{authentication.status === 'anonymous' ? '登录' : authentication.displayName}</span>
              <ChevronDown size={13} />
            </button>
          </div>
        </header>
        <Outlet context={{ authentication } satisfies AppOutletContext} />
      </div>
    </TooltipProvider>
  )
}
