export type WaiverStatus = 'pending_info' | 'pending_approval' | 'approved' | 'rejected'

export type WaiverSummary = {
  waiver_id: string
  email_from: string
  department: string
  waiver_type: string
  status: WaiverStatus
  created_at: string
  updated_at: string
}

export type WaiverDetail = WaiverSummary & {
  collected_info: Record<string, unknown>
  missing_fields: string[]
  history: Array<{
    timestamp: string
    event: string
    content: string
  }>
  attachments: Array<{
    filename: string
    s3_presigned_url: string
  }>
}

export type WaiverListResponse = {
  items: WaiverSummary[]
  total: number
  page: number
  limit: number
}

export type DecideBody = {
  decision: 'approve' | 'reject'
  comment: string
}
