import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { getWaivers } from '../api/client'
import { StatusBadge } from '../components/StatusBadge'
import { Search } from 'lucide-react'
import type { WaiverStatus } from '../types/waiver'

const STATUSES: { value: string; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'pending_approval', label: 'Pending Approval' },
  { value: 'pending_info',     label: 'Pending Info' },
  { value: 'approved',         label: 'Approved' },
  { value: 'rejected',         label: 'Rejected' },
]

const DEPARTMENTS = ['', 'Computer Science', 'Electrical Engineering', 'Mathematics', 'Physics', 'Chemistry', 'Business']

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })
}

export function WaiverListPage() {
  const navigate = useNavigate()
  const [status, setStatus] = useState('')
  const [department, setDepartment] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const limit = 10

  const { data, isLoading } = useQuery({
    queryKey: ['waivers', status, department, page],
    queryFn: () => getWaivers({ status: status || undefined, department: department || undefined, page, limit }),
  })

  const items = (data?.items ?? []).filter(w =>
    !search ||
    w.email_from.toLowerCase().includes(search.toLowerCase()) ||
    w.waiver_id.toLowerCase().includes(search.toLowerCase()) ||
    w.waiver_type.toLowerCase().includes(search.toLowerCase())
  )
  const totalPages = Math.ceil((data?.total ?? 0) / limit)

  return (
    <div className="p-8">
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-gray-900">Waiver Requests</h2>
        <p className="text-gray-500 mt-1">Browse, filter, and act on incoming waiver requests</p>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-6">
        <div className="relative">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            placeholder="Search by email, ID, type…"
            value={search}
            onChange={e => { setSearch(e.target.value); setPage(1) }}
            className="pl-9 pr-4 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 w-64"
          />
        </div>

        <select
          value={status}
          onChange={e => { setStatus(e.target.value); setPage(1) }}
          className="px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
        >
          {STATUSES.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>

        <select
          value={department}
          onChange={e => { setDepartment(e.target.value); setPage(1) }}
          className="px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
        >
          {DEPARTMENTS.map(d => <option key={d} value={d}>{d || 'All departments'}</option>)}
        </select>

        {(status || department || search) && (
          <button
            onClick={() => { setStatus(''); setDepartment(''); setSearch(''); setPage(1) }}
            className="px-3 py-2 text-sm text-gray-500 hover:text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50"
          >
            Clear filters
          </button>
        )}
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200">
        {isLoading ? (
          <div className="p-12 text-center text-gray-400">Loading waivers…</div>
        ) : items.length === 0 ? (
          <div className="p-12 text-center text-gray-400">No waivers found.</div>
        ) : (
          <>
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
                    <th className="text-left px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Updated</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {items.map(w => (
                    <tr
                      key={w.waiver_id}
                      onClick={() => navigate(`/waivers/${w.waiver_id}`)}
                      className="hover:bg-gray-50 cursor-pointer transition-colors"
                    >
                      <td className="px-6 py-4 font-mono text-xs text-gray-500">{w.waiver_id}</td>
                      <td className="px-6 py-4 text-gray-700 max-w-[180px] truncate">{w.email_from}</td>
                      <td className="px-6 py-4 text-gray-700">{w.department}</td>
                      <td className="px-6 py-4 text-gray-700">{w.waiver_type}</td>
                      <td className="px-6 py-4"><StatusBadge status={w.status as WaiverStatus} /></td>
                      <td className="px-6 py-4 text-gray-500 whitespace-nowrap">{formatDate(w.created_at)}</td>
                      <td className="px-6 py-4 text-gray-500 whitespace-nowrap">{formatDate(w.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="px-6 py-4 border-t border-gray-100 flex items-center justify-between text-sm text-gray-500">
                <span>Page {page} of {totalPages} · {data?.total} waivers</span>
                <div className="flex gap-2">
                  <button
                    onClick={() => setPage(p => Math.max(1, p - 1))}
                    disabled={page === 1}
                    className="px-3 py-1 border border-gray-300 rounded-lg disabled:opacity-40 hover:bg-gray-50"
                  >
                    Previous
                  </button>
                  <button
                    onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                    disabled={page === totalPages}
                    className="px-3 py-1 border border-gray-300 rounded-lg disabled:opacity-40 hover:bg-gray-50"
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
