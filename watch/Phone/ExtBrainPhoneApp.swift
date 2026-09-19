import SwiftUI

@main
struct ExtBrainPhoneApp: App {
    @StateObject private var pairing = PairingBridge()
    @StateObject private var status = MacStatusClient()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup { PhoneRootView(pairing: pairing, status: status) }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    pairing.refresh()
                    status.refresh()
                }
            }
    }
}

private struct PhoneRootView: View {
    @ObservedObject var pairing: PairingBridge
    @ObservedObject var status: MacStatusClient

    var body: some View {
        TabView {
            PhonePairingView(pairing: pairing)
                .tabItem { Label("配对", systemImage: "applewatch") }
            RecordingStatusView(status: status)
                .tabItem { Label("录音", systemImage: "waveform") }
        }
    }
}

private struct PhonePairingView: View {
    @ObservedObject var pairing: PairingBridge
    @State private var pastedJSON = ""
    @State private var error: String?
    private let orange = Color(red: 243 / 255, green: 183 / 255, blue: 125 / 255)

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label("让手表专心录音", systemImage: "applewatch")
                        .font(.title2.weight(.semibold)).padding(.vertical, 8)
                    Text("手机完成一次配对，配置会自动送到手表。之后手表录音、Mac 保存并转写。")
                        .foregroundStyle(.secondary)
                }
                Section("1 · 从 Mac 复制配对内容") {
                    Text("在 Mac 打开 mac-pairing.json，全选复制；然后在这里粘贴。")
                    PasteButton(payloadType: String.self) { strings in
                        if let text = strings.first { pastedJSON = text }
                    }
                    SecureField("也可长按此处粘贴", text: $pastedJSON)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .privacySensitive()
                    Button("保存并自动配对手表") {
                        do {
                            try pairing.importJSON(pastedJSON)
                            pastedJSON = ""; error = nil
                        } catch { self.error = error.localizedDescription }
                    }
                    .disabled(pastedJSON.isEmpty)
                    if let error { Text(error).foregroundStyle(.red) }
                }
                Section("2 · 手表确认") {
                    Label(pairing.message, systemImage: pairing.acknowledged ? "checkmark.circle.fill" : "applewatch.radiowaves.left.and.right")
                        .foregroundStyle(pairing.acknowledged ? .green : orange)
                    if !pairing.macURL.isEmpty {
                        LabeledContent("Mac", value: pairing.macURL)
                            .font(.footnote)
                        Button("重新发送到手表") { pairing.refresh() }
                    }
                    Text("首次请把手表放在手机旁，并打开手表上的 ExtBrain。只有手表保存成功，才会显示确认。")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Section("录音同步") {
                    Text("打开手表 App 自动录音，长按钟面 2 秒停止；停止后在 Wi-Fi 下自动上传。家中配对地址需要能连接到 Mac。手表左下角可查看待上传、已上传和错误提示。")
                    Text("更新 App 即可保留现有录音；未同步前请勿卸载手表 App。")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("ExtBrain")
            .tint(orange)
        }
    }
}

private struct RecordingStatusView: View {
    @ObservedObject var status: MacStatusClient
    private let orange = Color(red: 243 / 255, green: 183 / 255, blue: 125 / 255)

    var body: some View {
        NavigationStack {
            Group {
                if status.chunks.isEmpty {
                    ContentUnavailableView(
                        "还没有读到录音",
                        systemImage: "waveform",
                        description: Text(status.message)
                    )
                } else {
                    List(status.chunks) { chunk in
                        ChunkRow(chunk: chunk)
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("录音")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        status.refresh()
                    } label: {
                        if status.loading {
                            ProgressView()
                        } else {
                            Image(systemName: "arrow.clockwise")
                        }
                    }
                    .disabled(status.loading)
                }
            }
            .overlay(alignment: .bottom) {
                if let updated = status.lastUpdated {
                    Text("更新于 \(updated, format: .dateTime.hour().minute())")
                        .font(.caption2).foregroundStyle(.secondary)
                        .padding(4).background(.thinMaterial).clipShape(Capsule())
                        .padding(.bottom, 6)
                }
            }
            .refreshable { status.refresh() }
        }
        .tint(orange)
    }
}

private struct ChunkRow: View {
    let chunk: ChunkStatus
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(ChunkStatusLabel.time(chunk.started_at))
                    .font(.headline)
                Spacer()
                Text(ChunkStatusLabel.duration(chunk.duration))
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                statusBadge
                if chunk.text == nil {
                    Text("尚未生成文字").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let text = chunk.text, !text.isEmpty {
                Text(text)
                    .font(.subheadline)
                    .lineLimit(expanded ? nil : 3)
                    .foregroundStyle(.primary)
                Button(expanded ? "收起" : "展开全文") {
                    withAnimation { expanded.toggle() }
                }
                .font(.caption)
                .foregroundStyle(.tint)
            }
        }
        .padding(.vertical, 4)
    }

    private var statusBadge: some View {
        let title = ChunkStatusLabel.title(chunk.status)
        let done = chunk.status == "done"
        return Text(title)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background((done ? Color.green : Color.orange).opacity(0.15))
            .foregroundStyle(done ? .green : .orange)
            .clipShape(Capsule())
    }
}
