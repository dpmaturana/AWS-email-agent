import { fetchAuthSession } from 'aws-amplify/auth'
import type { DecideBody, WaiverDetail, WaiverListResponse } from '../types/waiver'
import { MOCK_DETAILS, MOCK_WAIVERS } from './mock'

// Strip any trailing slash so `${API_BASE}/waivers` doesn't become `//waivers`.
const API_BASE = (import.meta.env.VITE_API_URL ?? '').replace(/\/$/, '')
const USE_MOCK = false

async function authHeader(): Promise<string> {
  // Pull a fresh Cognito ID token from the Amplify session on each request —
  // Amplify auto-refreshes it, so there's no stale-token problem.
  try {
    const session = await fetchAuthSession()
    const token = session.tokens?.idToken?.toString()
    return token ? `Bearer ${token}` : ''
  } catch {
    return ''
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: await authHeader(),
      ...init?.headers,
    },
  })
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json() as Promise<T>
}

function delay(ms = 400) {
  return new Promise(r => setTimeout(r, ms))
}

export type WaiverFilters = {
  status?: string
  department?: string
  page?: number
  limit?: number
}

export async function getWaivers(filters: WaiverFilters = {}): Promise<WaiverListResponse> {
  if (USE_MOCK) {
    await delay()
    let items = [...MOCK_WAIVERS]
    if (filters.status) items = items.filter(w => w.status === filters.status)
    if (filters.department) items = items.filter(w => w.department === filters.department)
    const page = filters.page ?? 1
    const limit = filters.limit ?? 20
    const start = (page - 1) * limit
    return { items: items.slice(start, start + limit), total: items.length, page, limit }
  }

  const params = new URLSearchParams()
  if (filters.status) params.set('status', filters.status)
  if (filters.department) params.set('department', filters.department)
  if (filters.page) params.set('page', String(filters.page))
  if (filters.limit) params.set('limit', String(filters.limit))
  return apiFetch<WaiverListResponse>(`/waivers?${params}`)
}

export async function getWaiverDetail(waiverId: string): Promise<WaiverDetail> {
  if (USE_MOCK) {
    await delay()
    const detail = MOCK_DETAILS[waiverId]
    if (!detail) throw new Error('Waiver not found')
    return detail
  }
  return apiFetch<WaiverDetail>(`/waivers/${waiverId}`)
}

export async function decideWaiver(waiverId: string, body: DecideBody): Promise<{ success: boolean }> {
  if (USE_MOCK) {
    await delay(600)
    const detail = MOCK_DETAILS[waiverId]
    if (detail) detail.status = body.decision === 'approve' ? 'approved' : 'rejected'
    return { success: true }
  }
  return apiFetch<{ success: boolean }>(`/waivers/${waiverId}/decide`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
