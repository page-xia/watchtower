/** WS deliveries can arrive after an HTTP mutation; only newer server state wins. */
export function shouldRefreshPersonalizationRevision(
  localRevision: number,
  streamRevision: number | undefined,
): streamRevision is number {
  return streamRevision != null && streamRevision > localRevision
}
