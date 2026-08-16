import assert from "node:assert/strict"
import { test } from "node:test"

import {
  messageBody,
  messageKeywords,
  messageMetaLabels,
} from "../src/components/detail/messagePresentation.ts"

test("message presentation removes machine tags from card metadata", () => {
  const msg = {
    event_type: "直接提及/theme:optical_800g_1_6t/sector:ai_power_liquid_cooling,consumer_electronics_precision",
    role: "theme:optical_800g_1_6t/sector:ai_power_liquid_cooling",
    keywords: ["胜宏科技", "theme:optical_800g_1_6t", "sector:consumer_electronics_precision"],
    display_text: "真实帖子里提到800G链路和液冷电源。",
  }

  assert.deepEqual(messageMetaLabels(msg), [])
  assert.deepEqual(messageKeywords(msg), ["胜宏科技"])
})

test("message body falls back when display text is only machine tags", () => {
  const msg = {
    display_text: "直接提及/theme:optical_800g_1_6t/sector:ai_power_liquid_cooling",
    media_summary: "",
    event_summary: "直接提及/theme:optical_800g_1_6t",
    topic_content: "原帖正文：AI算力链路继续放量。",
    topic_title: "算力链路跟踪",
  }

  assert.equal(messageBody(msg), "原帖正文：AI算力链路继续放量。")
})

test("message presentation keeps useful event labels", () => {
  const msg = {
    event_type: "订单",
    role: "受益标的",
    keywords: ["PCB", "胜宏科技"],
    display_text: "PCB订单改善，胜宏科技受益。",
  }

  assert.deepEqual(messageMetaLabels(msg), ["订单", "受益标的"])
  assert.deepEqual(messageKeywords(msg), ["PCB", "胜宏科技"])
  assert.equal(messageBody(msg), "PCB订单改善，胜宏科技受益。")
})
