# 更新说明（Changelog）

本项目为自用 Apple Watch 录音 + Mac 本地语音理解工具。本文记录面向审阅与迭代的改动，按时间倒序。

---

## 2026-09-19 — 审阅修复（PR-02 最小范围）

针对外部审阅意见（`open_hemory_review_2026-09-19.md`）的首轮修复。审阅结论是「不推倒重来」，本轮只执行 **PR-01（评测基线）+ PR-02（模型输出健壮性）** 的最小范围，未重写架构。

### 已修复（P1）

1. **F02 · 模型坏输出不再中断 worker**：`validate_summary()` 对 `topics=[1]`、`topics=[null]` 这类非对象元素原本会抛未捕获的 `AttributeError`，导致摘要任务异常中断。现在统一为受控的 `SummaryValidationError`（继承 `ValueError`，兼容旧契约），并在 worker 的单任务边界兜底捕获非预期 `Exception`（不捕获 `BaseException`），坏输出只标记 `needs_review`，下一任务继续处理。

2. **F07 · 主题核心证据必须属于本主题**：主题要点的 `evidence_ids` 现在校验必须落在本主题的 `utterance_ids` 内；顶层要点仍可用全局证据。跨主题关联今后需用显式 context 引用表达。

3. **F07 · 移除合并阶段的静默截断**：删除主题要点 `[:8]` 与顶层要点 `[:20]` 的静默截断，完整要点写入数据库；「少显示」留给 UI 层，数据库不再丢数据。

4. **F09 · 看板不再漏计质量状态**：`overview` 的「需要留意」现在包含 `status='needs_review'` 的对话及其 `summary_review_reasons`，不再只统计任务失败数；「生成成功」与「内容待核对」作为两个维度分别可见。

### 新增测试

- `tests/test_summary_guardrails_v2.py`（10 项）：非对象主题、null 主题、跨主题证据、未知证据、错误字段类型、空/超长数组、非对象摘要、错误继承契约。
- `tests/test_semantic_worker.py`：一个坏摘要输出后，worker 继续处理下一个对话（`needs_review` + `succeeded` 两个 job 状态并存）。
- `tests/test_dashboard.py`：`overview` 包含对话级质量警告。

### 未处理（列为后续 PR，本轮不实现）

| PR | 内容 |
|---|---|
| PR-03 | 重摘要、人工保护细化、失效语义 |
| PR-04 | 本地 / 云端模型适配层（`provider_adapters/`） |
| PR-05 | 真正语义分段（Episode / TopicSegment / TopicThread） |
| PR-06 | 证据化要点、决定、承诺、建议 |
| PR-07 | 每日上下文与 Agent 出口（outbox / JSON+MD / 撤回事件） |
| PR-08 | 来源完整性、时区、Watch 会话清单 |
| PR-09 | 朋友试用与多实例隔离 |

---

## 0.1.0（Build 5，源码基线）

- 长按钟面 2 秒停止 + 单调时钟状态机（`HoldState.swift`）；24pt 移动取消、失活/后台/低亮度取消、停止后轻点恢复。
- 40mm 小屏长计时截断修复。
- 本地 Silero VAD + MLX Whisper 转写、跨分片连续对话、主题归组、证据化摘要、可撤销人工修订、浅色看板。
- 原音不出本机，云端只接收必要文本；预算与幂等账本。

## 0.1.0（Build 4，已发布 TestFlight）

- 旋转光圈、打开自动录音、长按 2 秒停止、停止后 Wi-Fi 上传；iPhone 自动配对。
