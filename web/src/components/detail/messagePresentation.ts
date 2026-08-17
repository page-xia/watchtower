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

export type MessageDetailPresentationInput = {
  topic?: {
    title?: string | null
    content?: string | null
    media_summary?: string | null
  } | null
  event?: {
    title?: string | null
    summary?: string | null
  } | null
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

function readableBlocks(values: unknown[]): string[] {
  const seen = new Set<string>()
  const blocks: string[] = []
  for (const value of values) {
    const normalized = text(value)
    if (isOnlyMachineText(normalized) || seen.has(normalized)) continue
    seen.add(normalized)
    blocks.push(normalized)
  }
  return blocks
}

export function messageBody(msg: MessagePresentationInput, detail?: MessageDetailPresentationInput | null): string {
  if (detail) {
    const fullBlocks = readableBlocks([
      detail.topic?.content,
      detail.topic?.media_summary,
      detail.event?.summary,
    ])
    if (fullBlocks.length > 0) return fullBlocks.join("\n\n")

    const titleFallback = readableBlocks([detail.topic?.title, detail.event?.title])
    if (titleFallback.length > 0) return titleFallback[0]
  }

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
