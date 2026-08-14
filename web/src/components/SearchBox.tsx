import { useEffect, useRef, useState } from "react"
import { Search } from "lucide-react"
import { searchStocks } from "@/lib/api"
import type { StockSearchResult } from "@/types/api"
import { cn } from "@/lib/utils"

/** 代码/名称搜索，回车或点选打开个股详情 */
export function SearchBox({ onOpenDetail, watchlistCodes = [] }: { onOpenDetail: (code: string) => void; watchlistCodes?: string[] }) {
  const [q, setQ] = useState("")
  const [results, setResults] = useState<StockSearchResult[]>([])
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener("mousedown", onDocClick)
    return () => document.removeEventListener("mousedown", onDocClick)
  }, [])

  useEffect(() => {
    if (timerRef.current) window.clearTimeout(timerRef.current)
    const query = q.trim()
    if (!query) return
    timerRef.current = window.setTimeout(() => {
      searchStocks(query, watchlistCodes)
        .then((r) => {
          setResults(r)
          setOpen(true)
        })
        .catch(() => setResults([]))
    }, 250)
  }, [q, watchlistCodes])

  return (
    <div ref={boxRef} className="relative">
      <div className="flex h-7 w-52 items-center gap-1.5 rounded-md border border-input bg-secondary px-2">
        <Search className="h-3 w-3 text-muted-foreground" />
        <input
          value={q}
          onChange={(e) => {
            setQ(e.target.value)
            if (!e.target.value.trim()) {
              setResults([])
              setOpen(false)
            }
          }}
          onFocus={() => results.length > 0 && setOpen(true)}
          placeholder="代码 / 名称"
          className="w-full bg-transparent text-[11px] outline-none placeholder:text-muted-foreground/60"
        />
      </div>
      {open && results.length > 0 && (
        <div className="absolute right-0 top-8 z-40 w-64 overflow-hidden rounded-md border border-border bg-popover shadow-xl">
          {results.map((r) => (
            <button
              key={r.code}
              type="button"
              className={cn("flex w-full items-center justify-between px-2.5 py-1.5 text-left hover:bg-accent")}
              onClick={() => {
                setOpen(false)
                setQ("")
                onOpenDetail(r.code)
              }}
            >
              <span className="text-[12px] font-medium">{r.name}</span>
              <span className="num text-[10px] text-muted-foreground">{r.code}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
