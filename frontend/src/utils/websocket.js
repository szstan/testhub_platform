import api from '@/utils/api'

/**
 * Exchange the in-memory browser access token for a one-shot WebSocket ticket.
 * The access token is sent only in the authenticated HTTP request; the ticket
 * is short-lived and bound server-side to the exact execution WebSocket path.
 */
export async function issueWebSocketTicket(channel, executionId) {
  const response = await api.post('/auth/websocket-ticket/', {
    channel,
    execution_id: String(executionId),
  })
  return response.data
}

export function buildWebSocketUrl(path, ticket) {
  const url = new URL(path, window.location.origin)
  url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  url.searchParams.set('ticket', ticket)
  return url.toString()
}
