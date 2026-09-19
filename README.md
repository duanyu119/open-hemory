# open-hemory

Apple Watch 录音、Mac 局域网接收与本地转写的实验性源码项目。手表和 iPhone 中的应用名称为 **ExtBrain**，代码与数据目录仍沿用 `HemoryLocal`。

**本仓库只提供源码，不包含预编译 App、TestFlight 邀请、模型权重、录音、转写原文、私人配对或云服务密钥。** 安装者需要自己的 Mac、Apple 设备和签名配置；首次安装模型需要联网下载，默认推理在 Mac 本地运行，云服务关闭。

> 本项目采用 [MIT 许可证](LICENSE)，Copyright (c) 2026 DUANLANG。第三方代码、依赖和模型分别受其自身条款约束。
>
> 想了解完整架构、构建思路与文件路径，请阅读 [HANDOVER.md](HANDOVER.md)。

## 现在能做什么

- 打开 Watch App 后，在麦克风权限允许时开始录音；录音以约 5 分钟分片保存在手表。
- 长按钟面约 2 秒停止。**录音期间暂停上传，停止后在可用 Wi-Fi 网络下上传到 Mac**。不使用蜂窝、昂贵或受限网络；系统可能借配对 iPhone 代理连接，后台调度不保证实时送达。
- Mac 通过 HTTPS、证书指纹和随机令牌接收分片，校验内容、落盘、去重，再返回回执。断网时手表保留待上传内容。
- Mac 使用 Silero VAD 与 MLX Whisper 在本地处理录音，保留转写与音频来源映射；浏览器看板提供对话、主题、时间线和原音回听。
- 云端文字整理是可选功能。旧音频云转写入口也保留在代码中，但本 README 的安装步骤均不会启动它们。

```text
Watch 录音 → 停止 → Wi-Fi / iPhone 网络代理 → Mac HTTPS 接收
                                             ↓
                                      本地 VAD + ASR
                                             ↓
                                    本机浏览器看板与导出
```

## 环境要求

| 部分 | 要求 |
| --- | --- |
| Mac 接收器与看板 | macOS，Python 3.10+；接收器需要 `ffprobe`，测试和音频处理需要 `ffmpeg` |
| 本地语音模型 | Apple Silicon Mac（arm64）、Python 3.13、`requirements-semantic.txt` 的锁定依赖；预留数 GB 下载和额外录音空间 |
| Watch/iPhone 构建 | 完整 Xcode，包含兼容设备系统的 watchOS/iOS SDK；工程最低目标 watchOS 10、iOS 17，建议 Xcode 16 或更新版本 |
| 真机运行 | Apple Watch、配对 iPhone、自行配置的 Apple 开发签名；免费 Personal Team 的设备/有效期限制以 Apple 当前政策为准 |
| 网络 | Mac 和 Watch/iPhone 位于相互可达的可信局域网，Mac 保持开机且接收服务运行；需要能够解析 Mac 的 `.local` 名称 |

最低 deployment target 不代表已经覆盖所有机型和系统版本。较新设备系统可能要求更新的 Xcode。Intel Mac 可运行接收器和看板，本地 MLX 安装脚本只支持 Apple Silicon。项目不包含独立的 iPhone 录音功能。

## 1. 获取源码与命令行依赖

下载本仓库 ZIP 并解压，或在仓库页面复制实际地址后执行 `git clone`。在终端进入源码目录。以下示例均从仓库根执行。

已使用 Homebrew 的 Mac 可以执行：

```bash
brew install python@3.13 ffmpeg openssl@3
export PATH="$(brew --prefix openssl@3)/bin:$PATH"
python3.13 --version
ffprobe -version
openssl version
```

生成证书需要支持 `openssl req -addext` 的 OpenSSL；如果系统自带版本不支持，请先把 OpenSSL 3 加入当前终端 PATH。上述命令会安装公开软件；也可以用自己的包管理方式准备同等工具。

## 2. 初始化 Mac 接收器

```bash
export HEMORY_DATA_DIR="$HOME/Library/Application Support/HemoryLocal"
python3.13 scripts/setup_mac.py --data-dir "$HEMORY_DATA_DIR"
```

脚本在指定数据目录生成随机令牌、自签 TLS 证书及 `private/mac-pairing.json`，权限限制为当前用户读取。不会自动安装服务或启用云处理，也不会配置公网入口。已存在的证书和令牌会保留；更换主机名、证书或端口时需检查配置并重新配对。

在同一个终端启动接收器：

```bash
python3.13 mac/hemory_local.py --data-dir "$HEMORY_DATA_DIR" server \
  --host 0.0.0.0 --port 8765 \
  --cert "$HEMORY_DATA_DIR/private/receiver-cert.pem" \
  --key "$HEMORY_DATA_DIR/private/receiver-key.pem" \
  --token-file "$HEMORY_DATA_DIR/private/receiver-token.txt"
```

`0.0.0.0` 是监听所有本机接口，不是填给手表的地址。手表使用配对 JSON 中生成的 `https://<你的 Mac 主机名>.local:8765`。macOS 防火墙应允许该 Python 接收可信局域网连接；访客网络/AP 隔离可能阻止访问。不要做公网端口转发。Mac 睡眠或进程退出后，上传需等待服务恢复。

如需本机登录后自动启动接收器，可明确选择：

```bash
python3.13 scripts/setup_mac.py --data-dir "$HEMORY_DATA_DIR" --install --host 0.0.0.0
```

这会创建/替换当前用户的 `org.open-hemory.receiver` LaunchAgent，仅启动接收器，不启动语义 worker 或旧云转写 worker。关闭自启动的方法见 [Mac 说明](mac/README.md)。

## 3. 构建并安装 Watch 与 iPhone App

所有签名值均为空或示例值，需要自行填写。先将下面的 Bundle ID 改成自己唯一的反向域名标识；不要使用维护者的 Apple 账号或 Team。

```bash
python3.13 watch/generate_project.py --bundle-id org.example.myhemory
open watch/HemoryLocal.xcodeproj
```

1. 在 Xcode 登录自己的 Apple 账号，在 **Signing & Capabilities** 为 `HemoryLocal` 和 `HemoryLocalDistribution` 两个 target 选择自己的 Team，使用自动签名。
2. `HemoryLocalDistribution` 是 iPhone 配套 App；其 Bundle ID 为生成器指定值。Watch ID 为同一值加 `.watchkitapp`。配套关系通过 `HEMORY_COMPANION_BUNDLE_IDENTIFIER` 传到 Watch 的 Info.plist，三者应一致。
3. 选择 `HemoryLocalDistribution` scheme 和自己的已配对 iPhone，构建运行；确认 Watch App 已安装。必要时在 iPhone 的 Watch App 中安装，或选择 `HemoryLocal` scheme 运行到手表。按 Xcode/设备提示开启 Developer Mode 和信任开发者。
4. 不要把本地修改后的 Team ID、签名文件或个人 Bundle ID 当作通用发布配置提交。再次运行生成器会重建工程并覆盖工程内的手动配置。

生成器也支持 `--team`、`HEMORY_DEVELOPMENT_TEAM` 和 `HEMORY_BUNDLE_ID` 环境变量；见 [示例环境](examples/config.env.example)。仓库生成工程保持 Team 为空。

## 4. 私下配对并试录

在自己的 Mac 上打开 `$HEMORY_DATA_DIR/private/mac-pairing.json`，通过你信任的本地方式传给自己的 iPhone，在 ExtBrain 的配对页面粘贴。它包含访问录音接收器的令牌，不要发到群聊、Issue、截图或公开仓库。等待手表显示配置已保存。

`watch/pairing.example.json` 只展示格式，示例令牌和指纹无效，不能直接用于配对。当前上传策略始终禁用蜂窝网络，示例与新配置均使用 `allow_mobile_data: false`。

打开手表 App，授予麦克风权限，录一段你有权录制的短测试内容；长按约 2 秒停止后观察上传状态。当前行为会在离开 App 后再次打开时重新尝试开始录音；停止后只抬腕不会自动重启。请先熟悉状态提示，再作长时间录音。未同步前不要卸载 Watch App。上传成功后也不会自动清理原音，应自行规划存储。

## 5. 安装本地模型并启动转写

模型下载会联网访问公开的软件/模型源，不上传录音、不读取云凭据。依赖和模型有固定版本/校验值，网络或上游可用性可能影响安装；脚本遇到校验不符会停止。第三方模型条款见下载后的 LICENSE 文件和 manifest。

在仓库根的新终端执行：

```bash
export HEMORY_DATA_DIR="$HOME/Library/Application Support/HemoryLocal"
python3.13 scripts/setup_local_models.py --python "$(command -v python3.13)" \
  --data-root "$HEMORY_DATA_DIR"
python3.13 scripts/configure_local.py --data-dir "$HEMORY_DATA_DIR"
.venv-semantic/bin/python mac/semantic_worker.py \
  --data-dir "$HEMORY_DATA_DIR" --once --no-cloud
```

配置脚本从模型 manifest 读取本机路径，创建 `private/semantic.json`，设为本地处理、云服务关闭，不覆盖已有配置、不启动服务。数据目录中需要先有接收器初始化的 `queue.sqlite3`。要持续处理可省略 `--once`；保留 `--no-cloud` 可明确禁止本次进程调用云摘要。不要同时运行旧 `hemory_local.py worker` 与新语义 worker，它们共用进程锁。

仓库还提供 [本地配置格式示例](examples/semantic.local.example.json)，其中路径是占位符，通常应使用配置脚本生成真实路径。示例 `.env` 只供 shell 导出变量，Python 进程不会自动加载 `.env`。

## 6. 查看本机看板

在另一个终端执行：

```bash
python3.13 mac/dashboard.py
```

浏览器打开 `http://127.0.0.1:8766`。自定义数据目录时增加 `--data-dir "$HEMORY_DATA_DIR"`。看板只监听本机，不提供远程访问；它包含原音与转写，请不要用隧道公开。页面 GET 不触发模型，编辑和重算等操作会写入独立派生数据。

需要自己管理看板自启动时，可参考 [LaunchAgent 模板](examples/org.open-hemory.dashboard.plist)：先替换全部占位符为本机绝对路径，再按 [Mac 说明](mac/README.md) 安装。不要直接加载原模板。

## 数据保存在什么位置

默认 Mac 数据根为 `~/Library/Application Support/HemoryLocal`，可以通过 CLI 参数指定。修改数据根时接收器、模型安装器、配置脚本、worker 和看板必须全部指向同一目录。

| 路径 | 内容 |
| --- | --- |
| `private/` | 证书、私钥、接收令牌、真实配对 JSON、可选模型配置/凭据 |
| `chunks/`、`queue.sqlite3` | 原始音频、录音元数据、接收队列和去重状态 |
| `semantic/models/` | 下载模型、许可证和校验 manifest |
| `semantic/index.sqlite3`、`semantic/runs/` | 派生索引、转写和可选模型请求记录 |
| `semantic/exports/` | 派生 Markdown |
| `raw/`、`runs/`、`transcripts/`、`daily/` | 仅在使用旧云转写入口时产生的回包与输出 |
| `logs/` | 已安装后台服务的日志 |

Watch 音频及状态在 App 沙盒的 Application Support/HemoryLocal 中，配对凭据保存在系统 Keychain。iPhone 配对凭据也保存在 Keychain。Mac 数据未由应用做额外的静态加密；本地权限不能抵御同账号访问、管理员或已解锁设备被控制，敏感数据可结合 FileVault 与受保护备份管理。项目不自动清空原音、上传目录或备份。

## 验证与实验边界

```bash
python3.13 -m unittest discover -s tests -v
swiftc watch/HoldState.swift watch/tests/HoldStateTests.swift -o /tmp/open-hemory-hold-tests
/tmp/open-hemory-hold-tests
```

Python 测试使用临时目录、合成音频与明确的云回包 stub；需要 FFmpeg/OpenSSL，部分测试会开本机临时 HTTP/HTTPS 端口，不请求付费云接口。Swift 测试覆盖长按状态逻辑。通过这些测试不代表已完成真机续航、后台上传、真实转写或云端质量验收。

- 长时录音、来电/系统中断、存储耗尽、后台恢复和不同 Watch 机型需自行实测，未承诺全天续航或零丢失。
- 转写可能漏字、幻听或时间偏移；静音裁剪采取保守策略，不能保证可靠的环境声分类。语义分段、跨片衔接和摘要属于实验功能。
- 没有可靠的身份级说话人识别；不能把模型文字当作已核实事实。重要结论应核对原音与证据来源。
- 首次处理大批历史录音可能耗时、占内存、发热；原音和派生文件会持续占用磁盘。
- 当前日期分组和预算月份按 `Asia/Shanghai` 处理，其他时区用户需留意。
- 云摘要若自行启用会发送转写文本；旧云 STT 若自行启动会发送音频。必须先评估隐私、当前价格和服务商额度，估算预算不是供应商账单硬上限。仓库没有免费额度或永久免费承诺，也不会默认启动云端老师评测。

## 目录与安全

`watch/` 是 Swift 应用与工程生成器；`mac/` 是接收、语义处理和看板；`contracts/` 是输出 schema；`scripts/` 是本机安装/配置工具；`tests/` 是隔离测试；`examples/` 是无凭据的模板。

提交前遵循 [SECURITY.md](SECURITY.md)，不要发布数据目录、真实配对、日志、模型权重、签名产物或 `.env`。`.gitignore` 只是辅助，不代替检查待提交文件。
