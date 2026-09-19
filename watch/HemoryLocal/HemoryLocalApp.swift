import SwiftUI
import WatchKit

final class AppModel {
    static let shared = AppModel()
    let recorder: Recorder?
    let uploader: Uploader?
    let error: String?
    let pairing = PairingBridge()

    private init() {
        do {
            let store = try ChunkStore()
            let uploader = Uploader(store: store)
            self.uploader = uploader
            recorder = Recorder(store: store, uploader: uploader)
            error = nil
            pairing.onPairingSaved = { [weak uploader] in uploader?.retryAfterConfigurationChange() }
        } catch {
            recorder = nil; uploader = nil
            self.error = "无法打开录音存储：\(error.localizedDescription)"
        }
    }
}

final class WatchDelegate: NSObject, WKApplicationDelegate {
    // Reconnect a background session without turning an OS wake into a new user retry budget.
    func applicationDidFinishLaunching() { _ = AppModel.shared }

    func handle(_ backgroundTasks: Set<WKRefreshBackgroundTask>) {
        for task in backgroundTasks {
            if let pairingTask = task as? WKWatchConnectivityRefreshBackgroundTask {
                AppModel.shared.pairing.handleBackgroundTask(pairingTask)
            } else if let transfer = task as? WKURLSessionRefreshBackgroundTask, let uploader = AppModel.shared.uploader {
                uploader.handleBackgroundTask(transfer)
            } else if let refresh = task as? WKApplicationRefreshBackgroundTask, let uploader = AppModel.shared.uploader {
                uploader.handleRetryTask(refresh)
            } else { task.setTaskCompletedWithSnapshot(false) }
        }
    }
}

@main
struct HemoryLocalApp: App {
    @WKApplicationDelegateAdaptor(WatchDelegate.self) private var delegate
    var body: some Scene {
        WindowGroup {
            if let recorder = AppModel.shared.recorder, let uploader = AppModel.shared.uploader {
                Dashboard(recorder: recorder, uploader: uploader, pairing: AppModel.shared.pairing)
            } else { Text(AppModel.shared.error ?? "存储不可用").foregroundStyle(.red) }
        }
    }
}

private enum WatchPalette {
    static let orange = Color(red: 243 / 255, green: 183 / 255, blue: 125 / 255)
    static let apricot = Color(red: 1, green: 211 / 255, blue: 170 / 255)
    static let cream = Color(red: 1, green: 247 / 255, blue: 239 / 255)
}

private struct Dashboard: View {
    @ObservedObject var recorder: Recorder
    @ObservedObject var uploader: Uploader
    @ObservedObject var pairing: PairingBridge
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.isLuminanceReduced) private var isLuminanceReduced
    @State private var showRecordingError = false
    @State private var startOnNextActivation = true
    @State private var hold = HoldState()
    @GestureState private var touchActive = false
    @State private var faceVisible = false
    @State private var stopWorkItem: DispatchWorkItem?

    private var monotonicNow: TimeInterval { Double(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000 }
    private var gestureAllowed: Bool { displayActive && faceVisible && !recorder.busy }
    private var canStop: Bool { recorder.recording || recorder.interrupted }

    private var status: String {
        if recorder.interrupted { return "已中断 · 结束" }
        if recorder.busy { return "正在处理…" }
        if recorder.recording { return hold.isHolding ? "按住 2 秒停止" : "" }
        if recorder.failureMessage != nil { return "录音异常 · 重试" }
        return recorder.recordedSeconds > 0 ? "已停止 · 开始" : "轻点开始录音"
    }

    private var elapsed: String {
        let seconds = max(0, Int(recorder.recordedSeconds))
        return seconds < 3600
            ? String(format: "%02d:%02d", seconds / 60, seconds % 60)
            : String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
    }

    private var displayActive: Bool { scenePhase == .active && !isLuminanceReduced }

    private var needsAttention: Bool {
        uploader.snapshot.blocked > 0 || uploader.snapshot.orphaned > 0 ||
            uploader.snapshot.lastError != nil || recorder.interrupted || recorder.failureMessage != nil
    }

    private func clearStopWork() {
        stopWorkItem?.cancel()
        stopWorkItem = nil
    }

    private func cancelHold() {
        clearStopWork()
        hold.cancel()
    }

    private func scheduleStop() {
        clearStopWork()
        guard let deadline = hold.deadline else { return }
        let token = hold.generation
        let item = DispatchWorkItem {
            // Cancellation alone does not prevent an already queued work item from running.
            guard token == self.hold.generation, self.hold.isHolding else { return }
            let effect = self.hold.fire(generation: token, at: self.monotonicNow,
                                       allowed: self.gestureAllowed, canStop: self.canStop)
            if effect == .stop { self.recorder.stop() }
            // A timer firing early must not shorten the two-second hold.
            if self.hold.isHolding { self.scheduleStop() }
            else { self.clearStopWork() }
        }
        stopWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + max(0, deadline - monotonicNow), execute: item)
    }

    private func changeHold(_ value: DragGesture.Value, bounds: CGRect) {
        let wasIdle = hold.phase == .idle
        hold.begin(at: monotonicNow, origin: value.startLocation,
                   intent: canStop ? .stop : .start, allowed: gestureAllowed, bounds: bounds)
        hold.move(to: value.location, allowed: gestureAllowed, bounds: bounds)
        if wasIdle && hold.isHolding { scheduleStop() }
        if !hold.isHolding { clearStopWork() }
    }

    private func endHold(_ value: DragGesture.Value, bounds: CGRect) {
        clearStopWork()
        let effect = hold.end(at: monotonicNow, location: value.location, bounds: bounds,
                              allowed: gestureAllowed, canStop: canStop, canStart: !canStop)
        if effect == .stop { recorder.stop() }
        if effect == .start { recorder.start() }
    }

    var body: some View {
        NavigationStack {
            GeometryReader { geometry in
                let width = geometry.size.width
                let height = geometry.size.height
                let faceHeight = max(0, height - 44)
                let faceBounds = CGRect(x: 0, y: 0, width: width, height: faceHeight)
                ZStack(alignment: .top) {
                    Color.black
                    TimelineView(.everyMinute) { context in
                        RecordingClockFace(date: context.date, status: status,
                            recording: recorder.recording, interrupted: recorder.interrupted,
                            reducedLuminance: isLuminanceReduced, animationActive: displayActive,
                            holding: hold.isHolding, holdState: hold)
                    }
                    .frame(width: width, height: faceHeight)
                    .contentShape(Rectangle())
                    .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .local)
                        .updating($touchActive) { _, active, _ in active = true }
                        .onChanged { value in changeHold(value, bounds: faceBounds) }
                        .onEnded { value in endHold(value, bounds: faceBounds) })
                    .onChange(of: touchActive) { _, active in
                        if !active {
                            clearStopWork()
                            hold.release()
                        }
                    }
                    .onAppear { faceVisible = true }
                    .onDisappear {
                        faceVisible = false
                        cancelHold()
                    }
                    .accessibilityLabel(recorder.recording ? "正在录音" : "录音钟面")
                    .accessibilityValue(elapsed)
                    .accessibilityHint("打开 App 自动录音；长按两秒停止；停止后轻点可继续")
                    .accessibilityAction(named: "停止录音") {
                        cancelHold()
                        if !recorder.busy && (recorder.recording || recorder.interrupted) { recorder.stop() }
                    }
                    .accessibilityAction(named: "开始录音") {
                        cancelHold()
                        if !recorder.busy && !recorder.recording && !recorder.interrupted { recorder.start() }
                    }

                    HStack(spacing: 0) {
                        NavigationLink {
                            SyncDetailsView(recorder: recorder, uploader: uploader)
                        } label: {
                            ZStack(alignment: .topTrailing) {
                                Image(systemName: "list.bullet")
                                    .font(.system(size: 21, weight: .regular))
                                if needsAttention {
                                    Circle().fill(WatchPalette.orange)
                                        .frame(width: 5, height: 5).offset(x: 5, y: -2)
                                }
                            }
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                        }
                        .accessibilityLabel("录音与同步明细")
                        Spacer(minLength: 0)
                        Text(elapsed)
                            .font(.system(size: recorder.recordedSeconds < 3600
                                ? (width < 180 ? 20 : 22) : (width < 180 ? 13 : 18),
                                weight: .semibold, design: .rounded))
                            .monospacedDigit()
                            .lineLimit(1)
                            .minimumScaleFactor(0.8)
                            .foregroundStyle(WatchPalette.cream)
                            .accessibilityLabel("已录音 \(Int(recorder.recordedSeconds) / 3600) 小时 \(Int(recorder.recordedSeconds) / 60 % 60) 分 \(Int(recorder.recordedSeconds) % 60) 秒")
                        Spacer(minLength: 0)
                        NavigationLink {
                            PairingView(uploader: uploader, pairing: pairing)
                        } label: {
                            Image(systemName: "gearshape")
                                .font(.system(size: 22, weight: .regular))
                                .frame(width: 44, height: 44)
                                .contentShape(Rectangle())
                        }
                        .accessibilityLabel("配对与设置")
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(WatchPalette.cream)
                    // Reserve room for the elapsed value on 40mm screens while retaining 44pt controls.
                    .padding(.horizontal, width < 180 ? 6 : 13)
                    .opacity(isLuminanceReduced ? 0.5 : 1)
                    .offset(y: faceHeight - 1)
                }
            }
            .ignoresSafeArea()
            .containerBackground(.black, for: .navigation)
            .toolbar(.hidden, for: .navigationBar)
        }
        .tint(WatchPalette.orange)
        .onChange(of: scenePhase, initial: true) { _, phase in
            recorder.setDisplayActive(displayActive)
            if phase != .active { cancelHold() }
            // Wrist lowering only makes the scene inactive. Re-arm only after leaving the App.
            if phase == .background { startOnNextActivation = true }
            if phase == .active {
                if startOnNextActivation {
                    startOnNextActivation = false
                    recorder.start()
                }
                uploader.refreshStatus()
                pairing.refresh()
            }
        }
        .onChange(of: isLuminanceReduced) { _, reduced in
            recorder.setDisplayActive(displayActive)
            if reduced { cancelHold() }
        }
        .onChange(of: recorder.busy) { _, busy in
            if busy { cancelHold() }
        }
        .onChange(of: recorder.failureMessage) { _, value in
            if value != nil { showRecordingError = true }
        }
        .alert("录音未能继续", isPresented: $showRecordingError) {
            Button("知道了", role: .cancel) {}
        } message: {
            Text(recorder.failureMessage ?? "请查看录音与同步明细。")
        }
    }
}

// Only the halo animates while visible. The elapsed counter still follows captured audio frames.
private struct RecordingClockFace: View {
    let date: Date
    let status: String
    let recording: Bool
    let interrupted: Bool
    let reducedLuminance: Bool
    let animationActive: Bool
    let holding: Bool
    let holdState: HoldState
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private static let weekdayFormatter: DateFormatter = {
        let value = DateFormatter()
        value.locale = Locale(identifier: "en_US_POSIX")
        value.dateFormat = "EEE"
        return value
    }()
    private static let timeFormatter: DateFormatter = {
        let value = DateFormatter()
        value.locale = Locale(identifier: "en_US_POSIX")
        value.dateFormat = "HH:mm"
        return value
    }()
    private static let dateFormatter: DateFormatter = {
        let value = DateFormatter()
        value.locale = Locale(identifier: "en_US_POSIX")
        value.dateFormat = "dd MMM"
        return value
    }()

    var body: some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            let height = geometry.size.height
            ZStack {
                TimelineView(.animation(minimumInterval: 1.0 / 20,
                    paused: !recording || !animationActive || reducedLuminance || reduceMotion || holding)) { context in
                    DotMatrixRing(recording: recording, interrupted: interrupted,
                        reducedLuminance: reducedLuminance,
                        rotation: recording && animationActive && !reducedLuminance && !reduceMotion && !holding
                            ? context.date.timeIntervalSinceReferenceDate / 8 * 2 * .pi : 0)
                }
                // Keep the ring mounted; render actual elapsed monotonic time, not a trim animation.
                TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !holding || !animationActive)) { _ in
                    HoldProgressRing(progress: holdState.progress(
                        at: Double(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000))
                }
                .frame(width: width * 0.91, height: height * 0.88)
                .position(x: width / 2, y: height * 0.52)
                .opacity(holding && animationActive ? 1 : 0)
                .transaction { $0.animation = nil }
                .allowsHitTesting(false)
                Text(Self.weekdayFormatter.string(from: date).uppercased())
                    .font(.system(size: width * 0.064, weight: .semibold, design: .rounded))
                    .tracking(3.3)
                    .position(x: width / 2 + 1.6, y: height * 0.335)
                Text(Self.timeFormatter.string(from: date))
                    .font(.system(size: width * 0.238, weight: .light, design: .rounded))
                    .monospacedDigit()
                    .tracking(-1.5)
                    .position(x: width / 2, y: height * 0.535)
                Text(Self.dateFormatter.string(from: date).uppercased())
                    .font(.system(size: width * 0.078, weight: .semibold, design: .rounded))
                    .position(x: width / 2, y: height * 0.70)
                if !status.isEmpty {
                    Text(status)
                        .font(.system(size: width * 0.043, weight: .medium))
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)
                        .foregroundStyle(WatchPalette.apricot.opacity(reducedLuminance ? 0.55 : 1))
                        .position(x: width / 2, y: height * 0.795)
                }
            }
            .foregroundStyle(WatchPalette.cream.opacity(reducedLuminance ? 0.65 : 1))
        }
        .accessibilityElement(children: .ignore)
    }
}

private struct DotMatrixRing: View {
    let recording: Bool
    let interrupted: Bool
    let reducedLuminance: Bool
    let rotation: Double

    var body: some View {
        Canvas(opaque: false, rendersAsynchronously: false) { context, size in
            let step = size.width / 37
            let dot = step * 0.72
            let center = CGPoint(x: size.width / 2, y: size.height * 0.52)
            let radiusX = size.width * 0.475
            let radiusY = size.height * 0.48
            let brightness = reducedLuminance ? 0.19 : recording ? 1.0 : interrupted ? 0.65 : 0.48
            for row in 0...Int(size.height / step) {
                for column in 0...37 {
                    let x = (Double(column) + 0.5) * step
                    let y = (Double(row) + 0.5) * step
                    let dx = (x - center.x) / radiusX
                    let dy = (y - center.y) / radiusY
                    let radius = sqrt(dx * dx + dy * dy)
                    guard radius >= 0.67, radius <= 1 else { continue }
                    let edge = min(1.0, (1 - radius) / 0.09, (radius - 0.67) / 0.065)
                    // A continuous angular light band travels clockwise; pixels never alternate on/off.
                    let light = recording && !reducedLuminance
                        ? 0.42 + 0.58 * pow((cos(atan2(dy, dx) - rotation) + 1) / 2, 2) : 1
                    let alpha = (0.2 + edge * 0.8) * brightness * light
                    let mix = min(1.0, max(0.0, (x / size.width) * 0.7 + (1 - y / size.height) * 0.3))
                    let color = Color(red: (243 + 12 * mix) / 255,
                        green: (183 + 28 * mix) / 255, blue: (125 + 45 * mix) / 255)
                    let square = CGRect(x: x - dot / 2, y: y - dot / 2, width: dot, height: dot)
                    context.fill(Path(square), with: .color(color.opacity(alpha)))
                }
            }
        }
        .accessibilityHidden(true)
    }
}

private struct HoldRingShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.addArc(center: .zero, radius: 1, startAngle: .degrees(-90),
                    endAngle: .degrees(270), clockwise: false)
        return path.applying(CGAffineTransform(a: rect.width / 2, b: 0, c: 0,
                                              d: rect.height / 2, tx: rect.midX, ty: rect.midY))
    }
}

private struct HoldProgressRing: View {
    let progress: Double

    var body: some View {
        ZStack {
            Ellipse().stroke(WatchPalette.orange.opacity(0.22), lineWidth: 3)
            HoldRingShape()
                .trim(from: 0, to: max(0.0, min(1.0, progress)))
                .stroke(WatchPalette.orange, style: StrokeStyle(lineWidth: 3, lineCap: .round))
        }
        .accessibilityHidden(true)
    }
}

private struct SyncDetailsView: View {
    @ObservedObject var recorder: Recorder
    @ObservedObject var uploader: Uploader

    var body: some View {
        List {
            Section("录音") {
                Label(recorder.recording ? "正在录音" : recorder.interrupted ? "录音中断" : "麦克风已关闭",
                    systemImage: recorder.recording ? "mic.fill" : "mic.slash")
                    .foregroundStyle(WatchPalette.apricot)
                Text(recorder.message).font(.caption2)
                LabeledContent("本次采集", value: String(format: "%.1f 分钟", recorder.recordedSeconds / 60))
            }
            Section("同步到 Mac") {
                LabeledContent("待上传", value: "\(uploader.snapshot.pending) 段")
                LabeledContent("已上传", value: "\(uploader.snapshot.uploaded) 段")
                if uploader.snapshot.blocked > 0 {
                    LabeledContent("需处理", value: "\(uploader.snapshot.blocked) 段")
                        .foregroundStyle(WatchPalette.orange)
                }
                if uploader.snapshot.orphaned > 0 {
                    LabeledContent("待恢复", value: "\(uploader.snapshot.orphaned) 段")
                        .foregroundStyle(WatchPalette.orange)
                }
                Text(uploader.message).font(.caption2)
                if let error = uploader.snapshot.lastError {
                    Text(error).font(.caption2).foregroundStyle(WatchPalette.orange)
                }
                Button("重试同步") { uploader.retryAfterConfigurationChange() }
            }
        }
        .navigationTitle("录音与同步")
        .tint(WatchPalette.orange)
    }
}

private struct PairingView: View {
    @ObservedObject var uploader: Uploader
    @ObservedObject var pairing: PairingBridge
    @State private var value = Pairing.load()
    @State private var feedback = ""
    @State private var pairingJSON = ""
    var body: some View {
        Form {
            Section("iPhone 自动配对") {
                Text(pairing.message).font(.caption2)
                Text("在 iPhone 的 ExtBrain 粘贴一次，手表会自动保存。")
                    .font(.caption2)
                Button("从 iPhone 获取配对") { pairing.refresh() }
            }
            Section("快速配对") {
                SecureField("粘贴 Mac 配对 JSON", text: $pairingJSON)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button("导入配对信息") {
                    do {
                        value = try Pairing.fromJSON(pairingJSON)
                        pairingJSON = ""
                        feedback = "已导入，请核对下方 Mac 地址，再点保存并同步。"
                    } catch { feedback = error.localizedDescription }
                }
                .disabled(pairingJSON.isEmpty)
                Text("可借配对 iPhone 的键盘一次粘贴。配置只保存到手表，不含云端模型密钥。")
                    .font(.caption2)
            }
            Text("停止录音后自动排队，Wi-Fi 可用时上传。家中地址需能连接到 Mac；离线时保留原音，恢复网络后补传。")
                .font(.caption2)
            TextField("https://主机:端口", text: $value.macURL)
            SecureField("Mac 令牌", text: $value.token)
            Picker("证书验证", selection: $value.tlsMode) {
                Text("家中证书指纹").tag("pin")
                Text("远程系统 TLS").tag("system")
            }
            if value.tlsMode == "pin" { TextField("证书 SHA-256", text: $value.certificateSHA256) }
            TextField("可选 Access Client ID", text: $value.cfAccessClientID)
            SecureField("可选 Access Secret", text: $value.cfAccessClientSecret)
            Text("仅使用 Wi-Fi 同步，不使用手表或 iPhone 的移动数据。")
                .font(.caption2)
            Button("保存并同步") {
                do {
                    try value.save()
                    uploader.retryAfterConfigurationChange()
                    feedback = "已保存到手表钥匙串。"
                } catch { feedback = error.localizedDescription }
            }
            Text(feedback).font(.caption2)
        }.navigationTitle("配对 Mac")
            .tint(WatchPalette.orange)
            .onDisappear { pairingJSON = "" }
            .onChange(of: pairing.macURL) { _, _ in value = Pairing.load() }
    }
}
