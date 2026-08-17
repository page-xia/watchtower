import { useSyncExternalStore } from "react"

/**
 * 白天 / 夜晚主题。
 * 深夜主题为默认（:root 变量即深色），白天主题通过 <html class="light"> 切换，
 * 选择持久化到 localStorage；index.html 内联脚本负责首屏防闪烁。
 */

export type Theme = "dark" | "light"

const STORAGE_KEY = "terminal-theme"

function readStored(): Theme {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "light" ? "light" : "dark"
  } catch {
    return "dark"
  }
}

let current: Theme = readStored()
const listeners = new Set<() => void>()

function apply(t: Theme) {
  document.documentElement.classList.toggle("light", t === "light")
}

export function setTheme(t: Theme) {
  current = t
  try {
    window.localStorage.setItem(STORAGE_KEY, t)
  } catch {
    /* localStorage 不可用时仅本次会话生效 */
  }
  apply(t)
  for (const l of listeners) l()
}

export function toggleTheme() {
  setTheme(current === "dark" ? "light" : "dark")
}

function subscribe(l: () => void) {
  listeners.add(l)
  return () => {
    listeners.delete(l)
  }
}

/** 订阅当前主题；切换时触发组件重渲染（图表 option 依赖它重建） */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, () => current)
}

// 模块加载时同步一次，保证未经过 index.html 内联脚本时也不闪错主题
apply(current)

/**
 * ECharts / SVG 配色：canvas 与 SVG 属性无法解析 CSS var()，
 * 统一从这里按主题取实际颜色值；组件里 useTheme() + useMemo 依赖 palette 即可随主题重建。
 */
const CHANNELS = {
  dark: {
    grid: "220 15% 14%",
    axis: "220 10% 45%",
    axisStrong: "220 15% 75%",
    tooltipBg: "222 28% 9%",
    tooltipBorder: "220 15% 20%",
    tooltipText: "220 15% 85%",
    markLine: "220 10% 35%",
    axisPointer: "220 15% 30%",
    up: "354 88% 58%",
    down: "152 76% 42%",
    gold: "40 95% 55%",
    cyan: "187 85% 53%",
    flat: "220 10% 55%",
    textStrong: "220 15% 90%",
    textMuted: "220 10% 52%",
    gaugeTrack: "220 15% 18%",
    zeroLine: "220 15% 25%",
    symbolBorder: "#0b0e14",
    dimBuy: "#8f1023",
    dimSell: "#0d5c36",
    mag: "310 85% 62%",
    blue: "215 90% 62%",
  },
  light: {
    grid: "213 25% 86%",
    axis: "215 18% 42%",
    axisStrong: "215 30% 25%",
    tooltipBg: "0 0% 100%",
    tooltipBorder: "213 28% 85%",
    tooltipText: "215 45% 18%",
    markLine: "215 15% 58%",
    axisPointer: "213 20% 72%",
    up: "354 78% 46%",
    down: "152 68% 30%",
    gold: "35 90% 42%",
    cyan: "190 85% 35%",
    flat: "215 15% 45%",
    textStrong: "215 45% 18%",
    textMuted: "215 18% 45%",
    gaugeTrack: "213 28% 86%",
    zeroLine: "213 24% 82%",
    symbolBorder: "#ffffff",
    dimBuy: "#f5c0c6",
    dimSell: "#a9dcc3",
    mag: "310 75% 45%",
    blue: "215 85% 45%",
  },
} as const

export function chartPalette(theme: Theme) {
  const c = CHANNELS[theme]
  return {
    grid: `hsl(${c.grid})`,
    axis: `hsl(${c.axis})`,
    axisStrong: `hsl(${c.axisStrong})`,
    tooltipBg: `hsl(${c.tooltipBg})`,
    tooltipBorder: `hsl(${c.tooltipBorder})`,
    tooltipText: `hsl(${c.tooltipText})`,
    markLine: `hsl(${c.markLine})`,
    axisPointer: `hsl(${c.axisPointer})`,
    up: `hsl(${c.up})`,
    down: `hsl(${c.down})`,
    gold: `hsl(${c.gold})`,
    cyan: `hsl(${c.cyan})`,
    flat: `hsl(${c.flat})`,
    textStrong: `hsl(${c.textStrong})`,
    textMuted: `hsl(${c.textMuted})`,
    gaugeTrack: `hsl(${c.gaugeTrack})`,
    zeroLine: `hsl(${c.zeroLine})`,
    symbolBorder: c.symbolBorder as string,
    dimBuy: c.dimBuy as string,
    dimSell: c.dimSell as string,
    mag: `hsl(${c.mag})`,
    blue: `hsl(${c.blue})`,
    upA: (alpha: number) => `hsl(${c.up} / ${alpha})`,
    downA: (alpha: number) => `hsl(${c.down} / ${alpha})`,
    goldA: (alpha: number) => `hsl(${c.gold} / ${alpha})`,
    flatA: (alpha: number) => `hsl(${c.flat} / ${alpha})`,
    magA: (alpha: number) => `hsl(${c.mag} / ${alpha})`,
    blueA: (alpha: number) => `hsl(${c.blue} / ${alpha})`,
  }
}

export type ChartPalette = ReturnType<typeof chartPalette>
