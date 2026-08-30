// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation, useRoutes } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'

import { routes } from './index'

describe('InvestWise routes', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: createMemoryStorage()
    })
    Object.defineProperty(globalThis, 'ResizeObserver', {
      configurable: true,
      value: TestResizeObserver
    })
  })

  it('redirects the root path to AI research', async () => {
    render(<RouteHarness initialPath="/" />)

    expect(
      await screen.findByRole('heading', { name: /A 股三季度风格展望/ })
    ).toBeTruthy()
    await waitFor(() => expect(screen.getByTestId('current-path').textContent).toBe('/ai-research'))
  })

  it('renders a route error for an unknown path', async () => {
    render(<RouteHarness initialPath="/missing" />)

    expect(await screen.findByRole('heading', { name: '页面未找到' })).toBeTruthy()
  })
})

function RouteHarness({ initialPath }: { initialPath: string }) {
  return <MemoryRouter initialEntries={[initialPath]}><RouteTree /></MemoryRouter>
}

class TestResizeObserver implements ResizeObserver {
  disconnect() {}
  observe() {}
  unobserve() {}
}

function RouteTree() {
  const element = useRoutes(routes)
  const location = useLocation()
  return <><output data-testid="current-path">{location.pathname}</output>{element}</>
}

function createMemoryStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key) },
    setItem: (key, value) => { values.set(key, value) }
  }
}
