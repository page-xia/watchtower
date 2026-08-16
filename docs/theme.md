# 主题配色规范（白天 / 夜晚）

日内盯盘终端支持昼夜双主题。默认是**夜晚（深夜主题）**，点击顶栏右上角 ☀️/🌙 按钮切换，选择持久化在 `localStorage["terminal-theme"]`。

## 切换机制

| 环节 | 位置 | 说明 |
| --- | --- | --- |
| 主题变量 | `web/src/index.css` | `:root` = 夜晚；`:root.light` = 白天 |
| 首屏防闪烁 | `web/index.html` 内联脚本 | 渲染前从 localStorage 读取并给 `<html>` 加 `.light` |
| 运行时状态 | `web/src/lib/theme.ts` | `useTheme()` / `toggleTheme()` / `setTheme()` |
| 图表配色 | `web/src/lib/theme.ts` | `chartPalette(theme)`，见下文 |

切换 = `document.documentElement.classList.toggle("light")`。Tailwind `darkMode: ["class"]` 未使用 `dark:` 前缀，主题完全由 CSS 变量驱动。

## 层次设计原则

- **夜晚**：深色平铺，靠边框（`--border`）分区，不加投影。
- **白天**：三层明度差 + 柔和投影：
  1. 页面底色 `--background` 最深的蓝
  2. 卡片/面板 `--card` 近白
  3. 浮层 `--popover` / tooltip 纯白
  4. 投影由 `:root.light .terminal-panel, :root.light .border.bg-card` 规则统一叠加，夜晚模式无投影

## CSS 变量对照表

| 变量 | 夜晚 `:root` | 白天 `:root.light` | 用途 |
| --- | --- | --- | --- |
| `--background` | `222 30% 5%` | `214 45% 89%` | 页面底色 |
| `--foreground` | `220 15% 90%` | `215 45% 18%` | 主文字 |
| `--card` | `222 26% 7.5%` | `210 60% 99%` | 面板/卡片底 |
| `--popover` | `222 28% 8%` | `0 0% 100%` | 浮层底 |
| `--primary` | `210 90% 55%` | `212 85% 46%` | 主按钮/强调 |
| `--secondary` | `220 18% 14%` | `213 40% 93%` | 次级底 |
| `--muted` | `220 16% 12%` | `213 35% 93%` | 徽章/标签底 |
| `--muted-foreground` | `220 10% 52%` | `215 18% 42%` | 次要文字 |
| `--accent` | `220 18% 16%` | `212 40% 91%` | 悬停/选中底 |
| `--border` | `220 15% 15%` | `213 28% 86%` | 边框 |
| `--input` | `220 15% 18%` | `213 26% 83%` | 输入框 |
| `--ring` | `210 90% 55%` | `212 85% 46%` | 焦点环 |
| `--destructive` | `0 72% 51%` | `0 72% 45%` | 错误/告警 |

### 行情语义色（A股惯例：红涨绿跌）

| 变量 | 夜晚 | 白天 | 用途 |
| --- | --- | --- | --- |
| `--up` / `--up-dim` | `354 88% 58%` / `354 60% 22%` | `354 78% 46%` / `354 70% 92%` | 涨 / 买入 · 红 |
| `--down` / `--down-dim` | `152 76% 42%` / `152 50% 16%` | `152 68% 30%` / `152 38% 90%` | 跌 / 卖出 · 绿 |
| `--gold` | `40 95% 55%` | `35 90% 42%` | 重点 / 龙头 |
| `--cyan` | `187 85% 53%` | `190 85% 35%` | 辅助信息 |
| `--flat` | `220 10% 55%` | `215 15% 45%` | 平盘/中性 |

白天语义色一律「压深一档」保证白底对比度；`*-dim` 用作文字徽章的底色。

## 图表 / SVG 配色（chartPalette）

ECharts canvas 与 SVG 属性**无法解析 CSS `var()`**，一律通过 `chartPalette(theme)` 取实际色值（`web/src/lib/theme.ts`）：

| palette 字段 | 夜晚 | 白天 | 用途 |
| --- | --- | --- | --- |
| `grid` | `220 15% 14%` | `213 25% 86%` | 网格线/轴线 |
| `axis` | `220 10% 45%` | `215 18% 42%` | 刻度文字 |
| `axisStrong` | `220 15% 75%` | `215 30% 25%` | 分类轴标签（板块名） |
| `tooltipBg` / `tooltipBorder` / `tooltipText` | `222 28% 9%` / `220 15% 20%` / `220 15% 85%` | `0 0% 100%` / `213 28% 85%` / `215 45% 18%` | 悬浮提示 |
| `markLine` | `220 10% 35%` | `215 15% 58%` | 零轴/昨收虚线 |
| `axisPointer` | `220 15% 30%` | `213 20% 72%` | 十字线 |
| `up` / `down` / `gold` / `cyan` / `flat` | 同语义色 | 同语义色 | 序列/标记 |
| `upA(a)` / `downA(a)` / `goldA(a)` / `flatA(a)` | — | — | 带透明度的同色（量柱、面积填充） |
| `textStrong` / `textMuted` | `220 15% 90%` / `220 10% 52%` | `215 45% 18%` / `215 18% 45%` | SVG 内文字（仪表盘等） |
| `gaugeTrack` | `220 15% 18%` | `213 28% 86%` | 仪表盘轨道 |
| `zeroLine` | `220 15% 25%` | `213 24% 82%` | SVG 零轴 |
| `symbolBorder` | `#0b0e14` | `#ffffff` | 标记点描边（与卡片底同色） |
| `dimBuy` / `dimSell` | `#8f1023` / `#0d5c36` | `#f5c0c6` / `#a9dcc3` | 菱形开盘标记填充 |

序列彩色（指数共振 `COLORS`、板块 `sectorColor` 哈希色、情绪仪表渐变色）为高饱和中明度色，双主题通用，不随主题变化。

## 新增组件规则

**DO**

- 一律用 Tailwind 语义类：`bg-card`、`text-muted-foreground`、`text-up`、`bg-up-dim` 等（均带 `<alpha-value>`，`text-up/80` 这类修饰符可用）。
- 图表/SVG：`const theme = useTheme(); const pal = useMemo(() => chartPalette(theme), [theme])`，option 的 `useMemo` 依赖里加上 `pal`，切换主题时 option 自动重建（`useECharts` 会 setOption 增量更新）。
- 新的主题色需求：先加到 `index.css` 双主题变量 + `theme.ts` 双 CHANNELS，再使用。

**DON'T**

- 不要在组件里写死 `hsl(...)` / hex 深色值（历史教训：白天模式下刻度、tooltip 会发虚或隐形）。
- 不要在 `index.css` 手写 `.bg-up` 这类纯 CSS 类（Tailwind 透明度修饰符会静默失效，见 `tailwind.config.js` 注释）。
- 不要给白天主题单独写 `dark:` 前缀类——主题只走 CSS 变量。

## 相关文件

- `web/src/index.css` — 双主题 CSS 变量 + 白天投影规则
- `web/src/lib/theme.ts` — 主题状态 + chartPalette
- `web/index.html` — 首屏防闪烁脚本
- `web/src/components/TopBar.tsx` — 切换按钮
