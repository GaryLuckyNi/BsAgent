import { http } from './client'
import type { MarketDataProviderConfig, Page } from './types'

export async function listMarketDataProviders(q = ''): Promise<Page<MarketDataProviderConfig>> {
  const response = await http.get<Page<MarketDataProviderConfig>>('/api/market-data-providers', {
    params: q ? { q } : {}
  })
  return response.data
}

export async function createMarketDataProvider(
  payload: MarketDataProviderConfig
): Promise<MarketDataProviderConfig> {
  const response = await http.post<MarketDataProviderConfig>('/api/market-data-providers', payload)
  return response.data
}

export async function updateMarketDataProvider(
  id: string,
  payload: Partial<MarketDataProviderConfig>
): Promise<MarketDataProviderConfig> {
  const response = await http.put<MarketDataProviderConfig>(
    `/api/market-data-providers/${id}`,
    payload
  )
  return response.data
}

export async function activateMarketDataProvider(id: string): Promise<MarketDataProviderConfig> {
  const response = await http.post<MarketDataProviderConfig>(
    `/api/market-data-providers/${id}/activate`
  )
  return response.data
}

export async function deleteMarketDataProvider(id: string): Promise<void> {
  await http.delete(`/api/market-data-providers/${id}`)
}
