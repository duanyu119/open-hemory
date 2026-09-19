# Open-Hemory 项目交接文档（HANDOVER）

> 本文档面向独立审阅者（如 ChatGPT Pro），用于在不接触本机运行数据的前提下，完整理解项目的目标、架构、构建思路、文件路径与质量边界。
>
> 本仓库是**可公开复制、不含任何个人数据**的源码快照。真实录音、转写正文、配对资料、云凭据、Apple 签名标识与本机验收历史均**不在**仓库内；它们只存在于维护者的本机运行目录。

---

## 1. 项目是什么

Open-Hemory（内部代号 ExtBrain / HemoryLocal）是一套**自用的 Apple Watch 全天录音 → Mac 本地语音理解系统**，目标是减少对订阅制云转写服务的依赖：

- **Watch**：连续采集麦克风，按约 5 分钟一个文件分片，停止后等待 Wi-Fi 上传到家庭局域网内的 Mac。
- **Mac**：接收并校验原音，本地做静音筛选、带时间戳的语音转写，把连续录音聚合成「对话」，再按核心意思拆成「主题」，生成可回听的摘要卡片。
- **看板**：浅色、内容优先的本地网页，展示对话卡、时间线、原始录音与处理状态，支持可撤销的人工修订。

核心价值主张是：**原音留在本机、可追溯、可回听、可纠正**；云端只承担「文本归组与摘要」这一小步，且默认可关闭。

---

## 2. 设计取舍（构建思路）

### 2.1 为什么「本地语音 + 云端文本摘要」

- 语音转写与时间对齐在 Mac 本地完成，使用固定版本的 MLX Whisper + Silero VAD，**原音不出本机**。
- 云端只接收「必要转写文本 + 匿名句段 ID」，用于把长对话按主题归组、生成标题与概述。文本本身仍可能含私人信息，因此这是**已选择的数据边界**，而非「完全匿名」的承诺。
- 本地处理可离线重算、可追溯来源；云端摘要保留原始回包与费用账本，未确认的结果不盲目重发。

### 2.2 为什么保留 5 分钟分片，但跨片构建对话

- 5 分钟文件是**采集与传输的容器边界**（避免单个文件过大、便于断点续传与完整性校验），不是语义边界。
- Mac 侧按 session + sequence + 时间间隙，把相邻分片重组成**连续对话候选**；只有证实的长静音、明确缺片、时钟跳变才构成边界。缺失 ASR 文字**不等于**静音。

### 2.3 为什么「保守过滤、完整保留原音」

- 只跳过「连续至少 8 秒、同时低人声概率 + 低能量」的明确静音区间；混合噪声、低声、短回答、不确定片段一律保留。
- VAD 只判断人声概率，**不是**环境声分类器，也不做声纹识别；ASR 为空不等于静音。
- 原音永不删除；精简播放只是「跳过区间」，界面分别显示原跨度、转写覆盖、跳过原因。

### 2.4 为什么版本化派生数据 + 可撤销人工修订

- 原音与上传元数据是**不可变**的一等公民；转写、对话、主题、摘要都是可重算的派生层。
- 人工改字、改标题、合并/拆分、隐藏/恢复都以追加 revision 记录，新模型重跑**不覆盖**人工确认版，过期 base_revision 返回 409 冲突而非静默覆盖。

### 2.5 为什么固定模型版本 + manifest

- 本地模型用固定 revision + SHA-256 锁定，运行时强制离线（`HF_HUB_OFFLINE=1`），避免每次启动拉到新权重、导致结果不可复现。
- 依赖锁、模型哈希、许可依据统一记录在 manifest 中。

---

## 3. 端到端架构

```text
Apple Watch (watchOS)
  └─ AVAudioEngine 连续采样 → 约 5 分钟 AAC 分片
       └─ 停止后、Wi-Fi 可用时 → 后台 URLSession → HTTPS 上传
             (v1 七字段：chunk_id, session_id, sequence, started_at,
              duration_seconds, sha256, filename；TLS pin + Bearer 回执)

Mac receiver (hemory_local.py, HTTPS 8765)
  └─ 校验 UUID/SHA/时长 → 原音落盘 + SQLite 队列 → 回执 stored:true

语义层 (semantic_worker.py, 独立虚拟环境, 共享 worker.lock)
  ├─ audio.py      : SHA 校验 → FFmpeg 16k PCM → Silero VAD → 区间分类
  ├─ timeline.py   : 派生输入 ↔ 原音时间轴映射（左闭右开毫秒）
  ├─ transcribe.py : MLX Whisper 本地转写（word_timestamps，映射回原音）
  ├─ boundary.py   : 跨分片接缝的小范围本地复核（仅替换已证实覆盖的句段）
  ├─ conversation.py: 相邻分片 → 连续对话候选（含缺口/长停顿保护）
  ├─ summarize.py  : 有界文本窗口 → 主题归组 → 证据化摘要（证据 ID 校验）
  ├─ providers.py  : 云端文本摘要传输 + 预算/幂等/原始回包账本
  ├─ quality.py    : 疑似重复/不可靠转写标注（保留但暂不作为摘要证据）
  └─ store.py      : 版本化 SQLite，原音只读，派生结果可重算

Web 看板 (dashboard.py, 127.0.0.1:8766, 标准库)
  ├─ 只读 GET（不触发模型）→ 对话卡 / 时间线 / 原始录音 / 处理状态
  └─ 受保护写接口（会话 + CSRF + Origin/Host）→ 可撤销人工修订
```

---

## 4. 目录结构（文件路径）

```text
open-hemory/
├── README.md                 # 面向使用者的安装/运行说明（中文）
├── HANDOVER.md               # 本文档（面向审阅者）
├── SECURITY.md               # 安全与隐私边界说明
├── LICENSE                   # MIT
├── .gitignore
├── requirements-semantic.txt # 本地语义处理依赖锁
│
├── watch/                    # Apple Watch + iPhone 端
│   ├── generate_project.py   # 生成 Xcode 工程（Team/Bundle ID 参数化）
│   ├── pairing.example.json  # 配对配置示例（非真实凭据）
│   ├── HemoryLocal.xcodeproj/  # 由生成器产出，非手写
│   ├── HemoryLocal/
│   │   ├── HemoryLocalApp.swift  # SwiftUI 钟面、长按 2 秒停止、状态机 UI
│   │   ├── Recorder.swift        # AVAudioEngine 连续采样、5 分钟分片、中断恢复
│   │   ├── Storage.swift         # 分片元数据/清单、SHA-256、落盘、磁盘检查
│   │   ├── Uploader.swift        # 后台 URLSession、Wi-Fi 限制、TLS pin、重试、回执校验
│   │   ├── PairingBridge.swift   # iPhone↔Watch 的 WatchConnectivity 配对
│   │   ├── Info.plist            # 权限文案、后台能力、companion 声明
│   │   ├── PrivacyInfo.xcprivacy # 隐私清单
│   │   └── Assets.xcassets/      # 图标资源
│   ├── Phone/
│   │   ├── ExtBrainPhoneApp.swift
│   │   └── Assets.xcassets/
│   ├── HoldState.swift         # 长按状态机（单调时钟、generation、24pt 容差、取消）
│   ├── tests/HoldStateTests.swift  # 状态机测试
│   └── README.md
│
├── mac/                      # Mac 服务与语义处理
│   ├── hemory_local.py      # 接收器：HTTPS、SQLite 队列、原音校验、旧云 STT（可关）
│   ├── dashboard.py          # 浅色 Web 看板（标准库 HTTP 服务，仅回环）
│   ├── dashboard/
│   │   ├── index.html
│   │   ├── style.css
│   │   └── app.js            # 对话卡/时间线/原始录音/处理状态、单音频实例、修订交互
│   ├── semantic_worker.py    # 本地语义 worker（与旧 worker 共享 worker.lock 互斥）
│   ├── semantic/
│   │   ├── __init__.py
│   │   ├── store.py          # 版本化 SQLite、来源校验、修订/撤销、分页搜索
│   │   ├── audio.py          # FFmpeg 解码 + Silero VAD + 区间分类
│   │   ├── timeline.py       # 时间轴映射、ASR 窗口、稳定 utterance ID
│   │   ├── transcribe.py     # MLX Whisper 适配、模型身份、时间戳
│   │   ├── boundary.py       # 跨分片接缝复核
│   │   ├── conversation.py   # 连续对话候选
│   │   ├── summarize.py      # 主题归组 + 证据化摘要（schema 校验）
│   │   ├── providers.py      # 云端文本摘要传输 + 费用/幂等账本
│   │   └── quality.py        # 转写质量标注
│   └── README.md
│
├── scripts/
│   ├── setup_mac.py          # 初始化配置与安装 receiver（不自动装云 worker）
│   ├── setup_local_models.py # 独立环境安装 + 固定 revision 下载 + SHA/许可核验
│   └── configure_local.py    # 从模型 manifest 生成本地配置（云默认关闭）
│
├── contracts/
│   ├── processing-result-v1.schema.json  # 单次云 STT 来源记录 schema
│   └── semantic-result-v1.schema.json    # 语义结果契约
│
├── examples/
│   ├── config.env.example
│   ├── semantic.local.example.json
│   └── org.open-hemory.dashboard.plist
│
└── tests/                    # 见 §11
```

> 说明：本仓库是公开副本。维护者本机另有一个开发目录，其 `PROMA_HANDOVER.md`、`docs/` 历史验收、`.validation/`、模型权重、原音、配对与密钥文件**均不进入仓库**。

---

## 5. 模块职责详解

### 5.1 Watch 端（SwiftUI + AVFoundation）

- **连续采集**：`Recorder.swift` 用 `AVAudioEngine` 持续采样，文件轮换不重新开麦；按约 5 分钟一个 AAC 分片。
- **长按停止**：`HemoryLocalApp.swift` 用 `DragGesture(minimumDistance: 0)` + `HoldState.swift` 状态机实现「按住钟面 2 秒显示进度并停止」，松手取消、停止后轻点恢复。进度用**同一单调时钟**驱动，取消/失活/后台/低亮度统一取消，代次（generation）防止旧回调误触发；移动超过 24pt 容差视为取消。
- **上传策略**：录音中暂停上传，停止后等待 Wi-Fi；`Uploader.swift` 用后台 URLSession，禁用蜂窝/昂贵/受限网络，TLS pin，校验 ID/SHA 回执，幂等重试。

### 5.2 Mac receiver

- `hemory_local.py` 用标准库实现 HTTPS 接收，校验规范 UUID、带时区时间戳、0–3600 秒时长、SHA-256 与文件名一致，原音先落盘并提交 SQLite 才回 `stored:true`。
- 保留旧云 STT 路径但**默认不自动启用**；维护门控 `private/pause-legacy-worker` 可在认领前停用。

### 5.3 本地语义层

- `audio.py`：先校验原音 SHA，再 FFmpeg 解码为单声道 16k PCM（保留 PTS），Silero VAD 输出人声概率区间，按能量/VAD 组合分类为 `speech / silence / uncertain / mixed`。
- `timeline.py`：所有范围为原音时间轴毫秒、左闭右开 `[start_ms, end_ms)`，派生输入按采样数映射回原音，不按文字长度猜测。
- `transcribe.py`：本地 MLX Whisper（`word_timestamps=True`），以声学区域构造有界 ASR 窗口，时间 + 文本双证据去重，句段映射回原音。
- `boundary.py`：对相邻分片各取 ≤6 秒边界做本地复核，只有「时间映射 + 文本覆盖」都支持的跨片句段才替换原句，否则只生成待核对候选。
- `conversation.py`：按 session + sequence 连续性组装，明确缺片/时钟跳变/证实长静音构成边界；缺失文字不当作静音。
- `summarize.py`：把转写拆成有界窗口（≤100 句/约 2400 字符），逐窗主题归组，合并同义主题，生成带证据 ID 的标题/概述/要点；严格校验证据 ID 来自输入、每个句段恰好归属一个主题。
- `providers.py`：仅发送文本与匿名句段 ID；请求前预算预留，保存原始回包，未确认请求不盲目重试；报价核验超 7 天自动关闭新请求。
- `quality.py`：标注连续重复/疑似解码异常句段（保留原文与原音，仅暂不作为摘要证据）。
- `store.py`：独立 `semantic/index.sqlite3`，原音只读；版本化 revision、CAS 修订、撤销、合并/拆分、分页搜索。

### 5.4 Web 看板

- 标准库 HTTP 服务，仅绑定 `127.0.0.1`；音频访问要求规范 UUID + 数据库存在 + 路径无符号链接 + 位于原音根内；支持 HEAD 与普通/后缀 Range，流式发送。
- GET 不触发模型；显式修订走会话 Cookie + CSRF + Origin/Host 校验；列表分页、详情按需读取；自动刷新单飞、错误保留旧数据。

---

## 6. 数据模型与存储

**生产数据根**（不在仓库内，仅本机）：

```text
~/Library/Application Support/HemoryLocal/
├── chunks/<UUID>/<UUID>.m4a + metadata.json   # 原音（不可变）
├── queue.sqlite3                              # 接收去重、状态、费用预留
├── raw/<UUID>.json                            # 旧云 STT 原始回包
├── runs/<run_id>/                             # 不可变处理记录
├── transcripts/                               # 可重建 Markdown 视图
├── private/                                   # 密钥/配置（0600，不进仓库）
└── semantic/
    ├── index.sqlite3                          # 派生层主库
    ├── runs/<key>/                            # 分析/摘要/边界缓存
    ├── models/                                # 本地模型权重 + manifest
    ├── quality/                               # 转写质量标注与教师评测报告
    ├── exports/                               # 新版对话 Markdown
    └── runtime/                               # 语义 worker 的独立运行环境
```

**`semantic/index.sqlite3` 关键表**：

| 表 | 用途 |
|---|---|
| `analyses` | 每 chunk 的本地分析状态 + 输入 key（来源/模型/代码哈希） |
| `conversations` / `revisions` | 对话投影 + 追加式版本正文 |
| `corrections` | 人工修订操作与撤销引用 |
| `jobs` | 摘要任务（幂等 key、状态） |
| `provider_attempts` | 云端请求账本（预留/实际费用、usage、状态） |
| `processing_controls` | 每 chunk 的「仅本地重算 / 抑制云」标记 |

去重键由 stage + 来源 hash + 模型/参数/提示词版本计算；崩溃恢复不发布半张卡，人工确认版不被自动重算覆盖。

---

## 7. 云端摘要与费用策略

- 默认供应商：SiliconFlow；免费模型 `Qwen/Qwen3-8B` 用于文本归组与摘要，报价核验超过 7 天即停用新请求。
- 仅发送文本与匿名句段 ID，不上传音频、文件路径、哈希、配对信息或日志。
- 请求前预留预算：月度估算上限 5 USD、单次试跑 1 USD、最多 1000 请求/月；付费模型需另行核验供应商额度后才可能启用。
- 完整回包缓存后校验；格式不合格最多做一次明确修复；未确认请求（超时/5xx/中断）标 `needs_review`，不盲目重发。
- 「教师模型评测」是维护者的离线 QA 手段（用更强模型对照本地转写与摘要），**不在仓库或默认流程中启用**，其脚本与原始报告也不进入仓库。

---

## 8. 服务与部署

以 `launchd` 常驻（标签在公开副本中已通用化为 `org.open-hemory.*`，避免个人命名）：

- `receiver`：HTTPS 8765，接收并校验原音。
- `dashboard`：127.0.0.1:8766 看板。
- `semantic`：本地语义 worker，与旧云 worker 共享 `worker.lock` 互斥，确保同一时间只有一个 worker 处理队列，避免重复计费。

切换/迁移原则：旧 worker 自然完成当前请求后停用，再启用语义 worker；receiver 始终运行、不重跑会改配对的 `setup_mac.py`。

---

## 9. 测试与质量门槛

```bash
python3 -m unittest discover -s tests        # 标准库 + 隔离测试
node --check mac/dashboard/app.js            # 前端语法
```

测试矩阵（见 `tests/`）：

| 文件 | 覆盖 |
|---|---|
| `test_mac_pipeline.py` | 隔离 HTTPS、合成音频、stub 云、幂等/恢复/费用/Markdown |
| `test_audio_mapping.py` | 媒体 PTS、时间轴映射、静音跳过、AAC 偏移 |
| `test_boundary.py` | 跨片边界替换/候选、部分覆盖、重叠冲突 |
| `test_conversation.py` | 缺口保护、跨分片、无文字非静音 |
| `test_semantic_store.py` | 版本、人工修订、合并/拆分、撤销、分页 |
| `test_summary_contract.py` | 摘要 schema、证据 ID、费用门控、流式完整性、单次格式修复 |
| `test_semantic_quality.py` | 重复/不可靠转写标注、保留原文 |
| `test_worker_boundary.py` | worker 集成、边界缓存、人工保护、迟到片 |
| `test_dashboard.py` | Range/HEAD、路径/符号链接、Host/Origin/CSRF、版本冲突 |

**尚未由自动化测试覆盖、必须人工/真机验收的质量项**：

1. 真实中文字错率、句首定位误差（≥20 句抽查，目标 90% ≤1 秒）。
2. 低声、短回答、混合噪声的人声覆盖保留率（目标 ≥98%）。
3. 摘要忠实性（标题/决定/数字/人物均须原文支持）。
4. S11 真机触摸、抬腕、熄屏、VoiceOver、连续触摸与续航。
5. 非零视口下的视觉与 200% 缩放。

> 结论：自动化测试证明的是「结构正确、来源可追溯、幂等、边界防护」；**不等同于转写准确率或摘要忠实度已达标**。这两类必须分开陈述。

---

## 10. 已知限制与风险

- Watch 录音中暂停上传、停止后等待 Wi-Fi，因此卡片在 Mac 收到音频后生成，**不承诺录音时实时出字**。
- v1 上传协议没有「会话结束 / 最后序号」事件，跨分片对话边界是**暂定**的，补片后重算受影响邻域。
- Silero 检测人声概率，**不是**环境声分类器；无法自动区分「电视人声 / 远处谈话 / 本人对话」。
- 模型句段时间戳不是绝对真值；对齐失败会降级到整片定位，不伪造。
- 本地模型（MLX Whisper large-v3-turbo）约 1.6 GB 权重，转写占用内存（实测峰值约 2.1 GB）；低配机器需注意。
- 免费摘要模型归组质量低于强模型，可能偏碎；可后续用更强模型或人工修订改善。

---

## 11. 构建与运行（维护者视角）

### 11.1 安装本地模型环境

```bash
# 建立独立虚拟环境，下载固定 revision 模型并校验 SHA/许可
python3 scripts/setup_local_models.py
```

### 11.2 生成本地配置（云默认关闭）

```bash
python3 scripts/configure_local.py
```

### 11.3 启动本地语义 worker（隔离验收目录）

```bash
.venv-semantic/bin/python mac/semantic_worker.py --data-dir /path/to/isolated --once --no-cloud
```

### 11.4 启动看板

```bash
python3 mac/dashboard.py              # http://127.0.0.1:8766
```

### 11.5 Watch 工程生成

```bash
# 使用空 Team + 示例 Bundle ID，避免注入本机真实签名值
HEMORY_DEVELOPMENT_TEAM='' HEMORY_BUNDLE_ID=org.example.openhemory \
  python3 watch/generate_project.py
```

> 仓库不含预编译 App 与签名信息；使用者需自行在 Xcode 中设置自己的 Team 与 Bundle ID。

---

## 12. 一句话总结

这是一套「**原音不出本机、可追溯、可回听、可纠正**」的自用全天录音理解系统：Watch 采集 → Mac 本地筛选与带时间戳转写 → 跨片连续对话 → 云端文本归组摘要 → 浅色看板与可撤销人工修订；本地与云端职责清晰分离，派生数据版本化，质量边界在文档中显式声明而非隐含承诺。
