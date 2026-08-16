export type MessagePresentationInput = {
  display_text?: string | null
  media_summary?: string | null
  event_summary?: string | null
  topic_content?: string | null
  topic_title?: string | null
  event_type?: string | null
  role?: string | null
  keywords?: string[] | null
}

const MACHINE_PREFIX = /^(?:theme|sector):/i
const DIRECT_MENTION = /^直接提及(?:个股|板块)?$/

function text(value: unknown): string {
  return String(value ?? "").trim()
}

function isMachineFragment(value: string): boolean {
  const normalized = text(value)
  return !normalized || DIRECT_MENTION.test(normalized) || MACHINE_PREFIX.test(normalized)
}

function readableFragments(value: unknown): string[] {
  return text(value)
    .split("/")
    .map((part) => part.trim())
    .filter((part) => part && !isMachineFragment(part))
}

function isOnlyMachineText(value: unknown): boolean {
  const normalized = text(value)
  return !normalized || readableFragments(normalized).length === 0
}

export function messageBody(msg: MessagePresentationInput): string {
  const candidates = [
    msg.display_text,
    msg.media_summary,
    msg.event_summary,
    msg.topic_content,
    msg.topic_title,
  ]

  return candidates.map(text).find((candidate) => !isOnlyMachineText(candidate)) ?? ""
}

export function messageMetaLabels(msg: MessagePresentationInput): string[] {
  const labels = [msg.event_type, msg.role].flatMap(readableFragments)
  return Array.from(new Set(labels))
}

export function messageKeywords(msg: MessagePresentationInput): string[] {
  return Array.from(new Set((msg.keywords ?? []).map(text).filter((item) => item && !isMachineFragment(item))))
}
