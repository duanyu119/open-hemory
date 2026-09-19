# Mac 运行与排错

完整首次安装见 [根 README](../README.md)。本目录提供独立 Python 接收器、语义 worker 与只监听本机的看板。公开副本以手动运行和本地模型为默认，不带个人部署记录。

## 三个独立进程

- `hemory_local.py ... server`：TLS 上传接收器，默认数据根 `~/Library/Application Support/HemoryLocal`。局域网监听需显式设置 `--host 0.0.0.0`。
- `semantic_worker.py --no-cloud`：本地语音处理。模型环境使用 `.venv-semantic/bin/python`，配置为数据根的 `private/semantic.json`。`--once` 运行一轮；缺少队列库或模型时先按根 README 初始化。
- `dashboard.py`：`127.0.0.1:8766` 本机看板。需要已初始化的队列库；阅读不会触发模型，编辑操作会修改派生库。

`--data-dir` 对 `hemory_local.py` 必须位于子命令之前。另两个脚本直接接收该参数。不要同时启动旧云 worker 与语义 worker；它们使用同一个 `worker.lock`。

## 本机自启动

`scripts/setup_mac.py --install --host 0.0.0.0` 会安装/替换当前用户的 `org.open-hemory.receiver`，仅启动接收器。解释器路径取执行该脚本的 Python；安装后请保持源码与解释器路径有效。服务日志位于数据根的 `logs/`，不要公开日志。

停止并取消接收器下次自动启动（保留用户文件，不删除配置与录音）：

```bash
launchctl bootout "gui/$(id -u)/org.open-hemory.receiver"
launchctl disable "gui/$(id -u)/org.open-hemory.receiver"
```

如需恢复已安装服务：

```bash
launchctl enable "gui/$(id -u)/org.open-hemory.receiver"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.open-hemory.receiver.plist"
```

已在运行时不要重复 bootstrap。看板的可选模板在 `examples/org.open-hemory.dashboard.plist`：把全部 `__ABSOLUTE_*__` 替换为自己的绝对路径，确认数据根已有 `logs` 目录，保存为 `$HOME/Library/LaunchAgents/org.open-hemory.dashboard.plist`。路径中不要使用 `~` 或 `$HOME`，launchd 不经 shell 展开。检查后用 `launchctl bootstrap "gui/$(id -u)" ...` 加载；关闭方式同上，将标签换成 `org.open-hemory.dashboard`。

公开副本不提供个人迁移脚本或验收凭证，也不自动安装语义 worker。先用短录音验证本地处理，再自行选择运行方式。不要为了启动服务伪造模型质量验收。

## 常见问题

- **证书生成失败**：使用支持 `req -addext` 的 OpenSSL 3，见根 README 的 PATH 设置。现有证书不会被静默重建。
- **手表待上传**：先停止录音，确认 Wi-Fi 可用、Mac 没有睡眠、服务在运行、`.local` 主机名可解析且防火墙允许。访客 Wi-Fi 或客户端隔离可能阻止连接。
- **TLS/配对失败**：证书主机名、有效期、SAN 与指纹需正确；不要把 `.local` 换成未写进证书 SAN 的 IP。重新生成证书后需重新配对。
- **看板没文字**：确认音频已落盘、语义 worker 使用正确数据根和配置；本地模型未完成安装时不会产生转写。云关闭时不会有云生成摘要。
- **重复处理或锁被占用**：查看自己的进程/LaunchAgent，确保每种 worker 只运行一次。不要绕过共享锁。
- **模型安装失败**：核对 Python 3.13、arm64、可用空间、网络和固定依赖版本。checksum mismatch 时停止调查，不应绕过校验。离线校验使用 `scripts/setup_local_models.py --offline --data-root ...`。

## 上传协议

`POST /v1/chunks` 接受原始音频 body，`Authorization: Bearer <token>`，`X-Chunk-Metadata` 为 base64 编码的 UTF-8 JSON。元数据字段为 `chunk_id`、`session_id`、带时区的 `started_at`、实际 `duration_seconds`、非负 `sequence`、小写 SHA-256 和与 UUID 一致的 `filename`。

新上传返回 201，相同内容重传返回 200，回执包含 `chunk_id`、`sha256`、`stored:true`。相同标识内容冲突返回 409，鉴权失败返回 401。原音与元数据落盘且数据库提交后才返回成功；手表核对回执后标记已同步，仍保留原音。`GET /health` 不返回私人内容。

## 可选云处理

源代码保留 `hemory_local.py worker --config ...` 旧音频云转写入口和语义云文字摘要接口。本次公开默认安装不配置或启动这些入口。自行启用意味着向对应供应商发送音频或文字，并可能计费；必须自行核验数据保留政策、模型价格、额度和预算。不要把云 API key 放进 Watch App、源码或公开配置。未知结果请求不会自动无限重试；手动 retry 可能再次计费。
