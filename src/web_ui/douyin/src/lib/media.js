// Resolve a stored avatar path to a servable URL (used by MessageList +
// ConversationList). Local 'avatars/…' paths are served under /media/; full
// http(s) URLs pass through; anything else has no avatar (initial-letter fallback).
import { mediaUrl } from './base'

export { mediaUrl }

export function resolveAvatarUrl(url) {
  if (!url) return null
  if (url.startsWith('avatars/')) return mediaUrl(url)
  if (url.startsWith('http')) return url
  return null
}
