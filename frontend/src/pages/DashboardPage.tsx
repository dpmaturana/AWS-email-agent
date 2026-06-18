import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { getWaivers } from '../api/client'
import { StatusBadge } from '../components/StatusBadge'
import { Clock, CheckCircle, XCircle, FileText } from 'lucide-react'
import type { WaiverSummary } from '../types/waiver'

function MetricCard({ label, value, icon: Icon, color }: {
  label: string
  value: number | string
  icon: React.ElementType
  color: string
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-6 flex items-center gap-4">
      <div className={`w-12 h-12 rounded-lg flex items-center justify-center ${color}`}>
        <Icon size={22} />
      </div>
      <div>
        <p className="text-2xl font-bold text-gray-900">{value}</p>
        <p className="text-sm text-gray-500">{label}</p>
      </div>
    </div>
  )
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })
}

export function DashboardPage() {
  const navigate = useNavigate()
  const { data, isLoading } = useQuery({
    queryKey: ['waivers', 'all'],
    queryFn: () => getWaivers({ limit: 100 }),
  })

  const items: WaiverSummary[] = data?.items ?? []
  const total = items.length
  const pending = items.filter(w => w.status === 'pending_approval' || w.status === 'pending_info').length
  const approved = items.filter(w => w.status === 'approved').length
  const rejected = items.filter(w => w.status === 'rejected').length

  const avgResolutionMs = (() => {
    const resolved = items.filter(w => w.status === 'approved' || w.status === 'rejected')
    if (!resolved.length) return null
    const avg = resolved.reduce((sum, w) =>
      sum + (new Date(w.updated_at).getTime() - new Date(w.created_at).getTime()), 0
    ) / resolved.length
    return Math.round(avg / (1000 * 60 * 60 * 24))
  })()

  const recent = items.slice(0, 5)

  return (
    <div className="p-8">
      <div className="mb-8">
        <h2 className="text-2xl font-bold text-gray-900">Dashboard</h2>
        <p className="text-gray-500 mt-1">Overview of all waiver requests</p>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <MetricCard label="Total Waivers"     value={isLoading ? '—' : total}    icon={FileText}    color="bg-purple-100 text-purple-600" />
        <MetricCard label="Pending"           value={isLoading ? '—' : pending}  icon={Clock}       color="bg-yellow-100 text-yellow-600" />
        <MetricCard label="Approved"          value={isLoading ? '—' : approved} icon={CheckCircle} color="bg-green-100 text-green-600" />
        <MetricCard label="Rejected"          value={isLoading ? '—' : rejected} icon={XCircle}     color="bg-red-100 text-red-600" />
      </div>

      {avgResolutionMs !== null && (
        <p className="text-sm text-gray-500 mb-6">
          Average resolution time: <span className="font-medium text-gray-700">{avgResolutionMs} day{avgResolutionMs !== 1 ? 's' : ''}</span>
        </p>
      )}

      {/* Recent waivers table */}
      <div className="bg-white rounded-xl border border-gray-200">
        <div className="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
          <h3 className="font-semibold text-gray-900">Recent Waivers</h3>
          <button
            onClick={() => navigate('/waivers')}
            className="text-sm text-blue-600 hover:text-blue-700 font-medium"
          >
            View all →
          </button>
        </div>

        {isLoading ? (
          <div className="p-8 text-center text-gray-400">Loading…</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="text-left px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">ID</th>
                  <th className="text-left px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">From</th>
                  <th className="text-left px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Department</th>
                  <th className="text-left px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Type</th>
                  <th className="text-left px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Status</th>
                  <th className="text-left px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {recent.map(w => (
                  <tr
                    key={w.waiver_id}
                    onClick={() => navigate(`/waivers/${w.waiver_id}`)}
                    className="hover:bg-gray-50 cursor-pointer transition-colors"
                  >
                    <td className="px-6 py-4 font-mono text-xs text-gray-500">{w.waiver_id}</td>
                    <td className="px-6 py-4 text-gray-700 max-w-[180px] truncate">{w.email_from}</td>
                    <td className="px-6 py-4 text-gray-700">{w.department}</td>
                    <td className="px-6 py-4 text-gray-700">{w.waiver_type}</td>
                    <td className="px-6 py-4"><StatusBadge status={w.status} /></td>
                    <td className="px-6 py-4 text-gray-500">{formatDate(w.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
