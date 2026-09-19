import Foundation
import Combine
import CryptoKit
import WatchConnectivity
#if os(watchOS)
import WatchKit
#endif

// The same versioned configuration envelope is compiled for iPhone and Watch.
// Only the latest context is queued; old credentials are never enqueued as user-info history.
struct PairingPacket: Codable {
    let version: Int
    let generation: Int64
    let json: String
    let revision: String

    static func make(_ pairing: Pairing, generation: Int64) throws -> PairingPacket {
        _ = try pairing.endpoint()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(pairing)
        return PairingPacket(version: 1, generation: generation,
            json: String(decoding: data, as: UTF8.self), revision: digest(data))
    }

    static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    func payload() throws -> [String: Any] { ["pairing_v1": try JSONEncoder().encode(self)] }

    static func decode(_ payload: [String: Any]) throws -> (PairingPacket, Pairing) {
        guard let data = payload["pairing_v1"] as? Data, data.count <= 16_384,
              let packet = try? JSONDecoder().decode(PairingPacket.self, from: data),
              packet.version == 1, packet.generation > 0,
              packet.revision == digest(Data(packet.json.utf8)) else {
            throw RecorderFailure(message: "配对信息版本或校验无效，请在 iPhone 重新导入。")
        }
        return (packet, try Pairing.fromJSON(packet.json))
    }
}

final class PairingBridge: NSObject, ObservableObject, WCSessionDelegate {
    @Published private(set) var message = "请在 iPhone 打开 ExtBrain 完成配对"
    @Published private(set) var macURL = Pairing.load().macURL
    @Published private(set) var acknowledged = false
    var onPairingSaved: (() -> Void)?
    private let generationKey = "phonePairingGeneration.v1"
    private var session: WCSession?
    #if os(watchOS)
    private var backgroundTasks: [WKWatchConnectivityRefreshBackgroundTask] = []
    private var pendingObservation: NSKeyValueObservation?
    #endif

    override init() {
        super.init()
        guard WCSession.isSupported() else { message = "此设备不支持 Apple Watch 配对"; return }
        let session = WCSession.default
        self.session = session
        session.delegate = self
        #if os(watchOS)
        pendingObservation = session.observe(\.hasContentPending, options: [.new]) { [weak self] _, _ in
            DispatchQueue.main.async { self?.finishBackgroundTasks() }
        }
        #endif
        session.activate()
    }

    private var generation: Int64 {
        Int64(UserDefaults.standard.double(forKey: generationKey))
    }

    #if os(iOS)
    func importJSON(_ text: String) throws {
        let value = try Pairing.fromJSON(text)
        try value.save()
        let next = max(generation + 1, Int64(Date().timeIntervalSince1970 * 1000))
        UserDefaults.standard.set(Double(next), forKey: generationKey)
        macURL = value.macURL
        acknowledged = false
        refresh()
    }

    private func outgoing() throws -> [String: Any] {
        try PairingPacket.make(Pairing.load(), generation: generation).payload()
    }

    func refresh() {
        guard !macURL.isEmpty else { message = "先在手机粘贴 Mac 配对内容"; return }
        guard let session, session.activationState == .activated else {
            message = "配置已保存在手机，正在连接手表…"; return
        }
        guard session.isPaired else { message = "请先在 iPhone 的 Watch App 中配对手表"; return }
        guard session.isWatchAppInstalled else { message = "配置已保存，请安装或更新手表上的 ExtBrain"; return }
        do {
            let payload = try outgoing()
            try session.updateApplicationContext(payload)
            if !acknowledged { message = "等待手表确认，请打开手表上的 ExtBrain" }
            if session.isReachable {
                session.sendMessage(payload, replyHandler: { [weak self] reply in
                    DispatchQueue.main.async { self?.receiveAcknowledgement(reply) }
                }, errorHandler: { [weak self] _ in
                    DispatchQueue.main.async {
                        guard let self, !self.acknowledged else { return }
                        self.message = "已排队，等手表连接后自动配对"
                    }
                })
            }
        } catch { message = "配对发送失败，请重试：\(error.localizedDescription)" }
    }

    private func receiveAcknowledgement(_ payload: [String: Any]) {
        guard let expected = try? PairingPacket.make(Pairing.load(), generation: generation),
              payload["saved_revision"] as? String == expected.revision,
              (payload["generation"] as? NSNumber)?.int64Value == generation else { return }
        acknowledged = true
        message = "手表已保存配对，可以开始同步录音"
    }

    func sessionDidBecomeInactive(_ session: WCSession) {
        DispatchQueue.main.async { self.acknowledged = false; self.message = "正在切换手表…" }
    }
    func sessionDidDeactivate(_ session: WCSession) { session.activate() }
    func sessionWatchStateDidChange(_ session: WCSession) {
        DispatchQueue.main.async { self.acknowledged = false; self.refresh() }
    }
    #else
    func refresh() {
        guard let session, session.activationState == .activated else { return }
        guard session.isReachable else {
            if macURL.isEmpty { message = "请在附近的 iPhone 打开 ExtBrain，粘贴并发送配对内容" }
            return
        }
        session.sendMessage(["request_pairing_v1": true], replyHandler: { [weak self] payload in
            DispatchQueue.main.async {
                guard payload["pairing_v1"] != nil else { return }
                _ = self?.receivePairing(payload)
            }
        }, errorHandler: { _ in })
    }

    private func receivePairing(_ payload: [String: Any]) -> [String: Any] {
        defer { finishBackgroundTasks() }
        do {
            let (packet, value) = try PairingPacket.decode(payload)
            guard packet.generation >= generation else { return ["ignored_older_configuration": true] }
            let changed = value != Pairing.load()
            try value.save()
            UserDefaults.standard.set(Double(packet.generation), forKey: generationKey)
            macURL = value.macURL
            acknowledged = true
            message = "已从 iPhone 自动配对"
            if changed { onPairingSaved?() }
            let ack: [String: Any] = ["saved_revision": packet.revision, "generation": NSNumber(value: packet.generation)]
            try? session?.updateApplicationContext(ack)
            return ack
        } catch {
            message = "配对未保存：\(error.localizedDescription)"
            return ["pairing_error": true]
        }
    }

    func handleBackgroundTask(_ task: WKWatchConnectivityRefreshBackgroundTask) {
        backgroundTasks.append(task)
        finishBackgroundTasks()
    }

    private func finishBackgroundTasks() {
        guard let session, session.activationState == .activated, !session.hasContentPending else { return }
        let completed = backgroundTasks
        backgroundTasks.removeAll()
        completed.forEach { $0.setTaskCompletedWithSnapshot(false) }
    }
    #endif

    func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        DispatchQueue.main.async {
            guard activationState == .activated else { self.message = "手表连接未就绪，请重新打开两端 App"; return }
            #if os(iOS)
            self.receiveAcknowledgement(session.receivedApplicationContext)
            #else
            if session.receivedApplicationContext["pairing_v1"] != nil {
                _ = self.receivePairing(session.receivedApplicationContext)
            }
            self.finishBackgroundTasks()
            #endif
            self.refresh()
        }
    }

    func sessionReachabilityDidChange(_ session: WCSession) {
        DispatchQueue.main.async { if session.isReachable { self.refresh() } }
    }

    func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        DispatchQueue.main.async {
            #if os(iOS)
            self.receiveAcknowledgement(applicationContext)
            #else
            _ = self.receivePairing(applicationContext)
            #endif
        }
    }

    func session(_ session: WCSession, didReceiveMessage message: [String: Any], replyHandler: @escaping ([String: Any]) -> Void) {
        DispatchQueue.main.async {
            #if os(iOS)
            replyHandler(message["request_pairing_v1"] as? Bool == true ? (try? self.outgoing()) ?? [:] : [:])
            #else
            replyHandler(self.receivePairing(message))
            #endif
        }
    }
}
