import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider, createBrowserRouter } from 'react-router-dom'

import { routes } from '@/router'
import './styles.css'

const root = document.getElementById('app')
if (!root) throw new Error('App root not found.')

createRoot(root).render(<StrictMode><RouterProvider router={createBrowserRouter(routes)} /></StrictMode>)
