import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getWaiverDetail, decideWaiver } from '../api/client'
import { StatusBadge } from '../components/StatusBadge'
import { ArrowLeft, Download, CheckCircle, XCircle } from 'lucide-react'

function formatDate(iso: string) {
  return new Date(iso).toLocaleString('en-GB', {
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-6">
      <h3 className="font-semibold text-gray-900 mb-4">{title}</h3>
      {children}
    </div>
  )
}

export function WaiverDetailPage() {
  const { waiverId } = useParams<{ waiverId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [comment, setComment] = useState('')
  const [actionResult, setActionResult] = useState<'approved' | 'rejected' | null>(null)

  const { data: waiver, isLoading, isError } = useQuery({
    queryKey: ['waiver', waiverId],
    queryFn: () => getWaiverDetail(waiverId!),
    enabled: !!waiverId,
  })

  const mutation = useMutation({
    mutationFn: (decision: 'approve' | 'reject') =>
      decideWaiver(waiverId!, { decision, comment }),
    onSuccess: (_, decision) => {
      setActionResult(decision === 'approve' ? 'approved' : 'rejected')
      queryClient.invalidateQueries({ queryKey: ['waivers'] })
      queryClient.invalidateQueries({ queryKey: ['waiver', waiverId] })
    },
  })

  if (isLoading) {
    return <div className="p-8 text-gray-400">Loading waiver…</div>
  }

  if (isError || !waiver) {
    return <div className="p-8 text-red-500">Waiver not found.</div>
  }

  const canDecide = waiver.status === 'pending_approval' && !actionResult

  return (
    <div className="p-8 max-w-4xl">
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <button
          onClick={() => navigate(-1)}
          className="p-2 rounded-lg hover:bg-gray-100 text-gray-500 hover:text-gray-700 transition-colors"
        >
          <ArrowLeft size={20} />
        </button>
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-2xl font-bold text-gray-900">{waiver.waiver_id}</h2>
            <StatusBadge status={actionResult ?? waiver.status} />
          </div>
          <p className="text-gray-500 text-sm mt-0.5">{waiver.waiver_type} · {waiver.department}</p>
        </div>
      </div>

      <div className="space-y-5">
        {/* Summary */}
        <Section title="Request Summary">
          <dl className="grid grid-cols-2 gap-x-8 gap-y-3 text-sm">
            {[
              ['Submitted by', waiver.email_from],
              ['Department',   waiver.department],
              ['Waiver type',  waiver.waiver_type],
              ['Status',       <StatusBadge key="s" status={actionResult ?? waiver.status} />],
              ['Created',      formatDate(waiver.created_at)],
              ['Last updated', formatDate(waiver.updated_at)],
            ].map(([label, value]) => (
              <div key={String(label)}>
                <dt className="text-gray-500 font-medium">{label}</dt>
                <dd className="text-gray-900 mt-0.5">{value}</dd>
              </div>
            ))}
          </dl>
        </Section>

        {/* Collected info */}
        <Section title="Collected Information">
          {Object.keys(waiver.collected_info).length === 0 ? (
            <p className="text-gray-400 text-sm">No information collected yet.</p>
          ) : (
            <dl className="space-y-2 text-sm">
              {Object.entries(waiver.collected_info).map(([k, v]) => (
                <div key={k} className="flex gap-4">
                  <dt className="text-gray-500 w-48 shrink-0 font-medium capitalize">{k.replace(/_/g, ' ')}</dt>
                  <dd className="text-gray-900">{String(v)}</dd>
                </div>
              ))}
            </dl>
          )}

          {waiver.missing_fields.length > 0 && (
            <div className="mt-4 pt-4 border-t border-gray-100">
              <p className="text-sm font-medium text-yellow-700 mb-2">Missing fields:</p>
              <ul className="list-disc list-inside text-sm text-yellow-600 space-y-1">
                {waiver.missing_fields.map(f => <li key={f}>{f.replace(/_/g, ' ')}</li>)}
              </ul>
            </div>
          )}
        </Section>

        {/* Attachments */}
        {waiver.attachments.length > 0 && (
          <Section title="Attachments">
            <ul className="space-y-2">
              {waiver.attachments.map(a => (
                <li key={a.filename}>
                  <a
                    href={a.s3_presigned_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-2 text-sm text-blue-600 hover:text-blue-700"
                  >
                    <Download size={15} />
                    {a.filename}
                  </a>
                </li>
              ))}
            </ul>
          </Section>
        )}

        {/* History */}
        <Section title="Event History">
          <ol className="relative border-l border-gray-200 space-y-4 ml-2">
            {waiver.history.map((h, i) => (
              <li key={i} className="ml-4">
                <div className="absolute -left-1.5 w-3 h-3 rounded-full bg-blue-500 border-2 border-white" />
                <p className="text-xs text-gray-400">{formatDate(h.timestamp)}</p>
                <p className="text-sm font-medium text-gray-700 capitalize">{h.event.replace(/_/g, ' ')}</p>
                <p className="text-sm text-gray-500">{h.content}</p>
              </li>
            ))}
          </ol>
        </Section>

        {/* Approve / Reject */}
        {actionResult ? (
          <div className={`rounded-xl border p-6 flex items-center gap-3 ${
            actionResult === 'approved'
              ? 'bg-green-50 border-green-200 text-green-700'
              : 'bg-red-50 border-red-200 text-red-700'
          }`}>
            {actionResult === 'approved'
              ? <CheckCircle size={20} />
              : <XCircle size={20} />
            }
            <p className="font-medium">
              Waiver {actionResult === 'approved' ? 'approved' : 'rejected'} successfully.
            </p>
          </div>
        ) : waiver.status === 'pending_approval' ? (
          <Section title="Decision">
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5">
                  Comment <span className="text-gray-400 font-normal">(optional)</span>
                </label>
                <textarea
                  rows={3}
                  value={comment}
                  onChange={e => setComment(e.target.value)}
                  placeholder="Add a comment for the applicant…"
                  className="w-full px-4 py-2.5 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                />
              </div>

              {mutation.isError && (
                <p className="text-red-600 text-sm">Something went wrong. Please try again.</p>
              )}

              <div className="flex gap-3">
                <button
                  onClick={() => mutation.mutate('approve')}
                  disabled={mutation.isPending || !canDecide}
                  className="flex items-center gap-2 px-5 py-2.5 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  <CheckCircle size={16} />
                  {mutation.isPending ? 'Processing…' : 'Approve'}
                </button>
                <button
                  onClick={() => mutation.mutate('reject')}
                  disabled={mutation.isPending || !canDecide}
                  className="flex items-center gap-2 px-5 py-2.5 bg-red-600 text-white text-sm font-medium rounded-lg hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  <XCircle size={16} />
                  {mutation.isPending ? 'Processing…' : 'Reject'}
                </button>
              </div>
            </div>
          </Section>
        ) : (
          <p className="text-sm text-gray-400 italic">
            This waiver is not in a state that requires a decision ({waiver.status.replace(/_/g, ' ')}).
          </p>
        )}
      </div>
    </div>
  )
}
