import type { WaiverStatus } from '../types/waiver'

const CONFIG: Record<WaiverStatus, { label: string; className: string }> = {
  pending_info:     { label: 'Pending Info',     className: 'bg-yellow-100 text-yellow-800' },
  pending_approval: { label: 'Pending Approval', className: 'bg-blue-100 text-blue-800' },
  approved:         { label: 'Approved',          className: 'bg-green-100 text-green-800' },
  rejected:         { label: 'Rejected',          className: 'bg-red-100 text-red-800' },
}

export function StatusBadge({ status }: { status: WaiverStatus }) {
  const { label, className } = CONFIG[status] ?? { label: status, className: 'bg-gray-100 text-gray-800' }
  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${className}`}>
      {label}
    </span>
  )
}
