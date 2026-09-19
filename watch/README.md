# Watch / iPhone 源码

安装与配对详见 [根 README](../README.md)。Watch 负责录音和停止后的 Wi-Fi 上传；iPhone companion 负责输入配对 JSON，并通过 WatchConnectivity 传给手表。

- `HemoryLocal/`：SwiftUI Watch 界面、录音、存储、配对与上传。
- `Phone/`：iPhone 配对页面。
- `HoldState.swift` / `tests/HoldStateTests.swift`：长按交互纯状态逻辑及可在 Mac 运行的测试。
- `generate_project.py`：重建 Xcode 工程及共享 scheme。支持 `--team`、`--bundle-id`，默认 Team 为空，Bundle ID 为示例值。
- `pairing.example.json`：无效占位配置，实际配置需由自己的 Mac 生成。

```bash
python3 watch/generate_project.py --bundle-id org.example.myhemory
open watch/HemoryLocal.xcodeproj
```

`HemoryLocalDistribution` target/scheme 是 iPhone App，`HemoryLocal` 是 Watch App。两个 target 都需选择自己的签名 Team，使用自己的唯一 Bundle ID。Watch Info.plist 的 `WKCompanionAppBundleIdentifier` 从 `HEMORY_COMPANION_BUNDLE_IDENTIFIER` 构建设置读取；生成器让它与 iPhone ID 一致。Watch ID 追加 `.watchkitapp`。重复运行生成器会覆盖手动工程配置；可通过本地环境变量保留自己的选择，但不要发布个人配置。

行为：打开 App 后尝试开始录音；长按钟面约 2 秒停止，短按停止状态的钟面可开始。录音期间暂停上传；停止后使用非蜂窝、非昂贵、非受限网络，系统可能借 iPhone 代理 Wi-Fi 连接。网络恢复、重新打开 App 或手动重试可触发同步；后台上传时间由 watchOS 决定。未经同步不要卸载 App。原音不自动删除。

在模拟器完成构建只证明源码可编译，不能证明 Watch 麦克风、真机网络代理、后台恢复或全天续航可靠。测试应覆盖短录音、长按/取消、断网恢复、来电中断、长时录音和存储压力。这里不包含签名证书、预编译 App 或个人真机验收历史。
