import SwiftUI

@main
struct ExtBrainPhoneApp: App {
    @StateObject private var pairing = PairingBridge()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup { PhonePairingView(pairing: pairing) }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { pairing.refresh() }
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
