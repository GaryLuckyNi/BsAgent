import { Navigate, type RouteObject } from 'react-router-dom'

import { AppLayout } from '@/layouts/app-layout'
import { AiResearchView } from '@/views/ai-research'
import { RouteErrorView } from '@/views/route-error'

export const routes: RouteObject[] = [{
  element: <AppLayout />,
  errorElement: <RouteErrorView />,
  children: [
    { index: true, element: <Navigate replace to="/ai-research" /> },
    { path: 'ai-research', element: <AiResearchView /> },
    { path: '*', element: <RouteErrorView /> }
  ]
}]
