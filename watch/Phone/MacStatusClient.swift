import Foundation
import Combine
import CryptoKit
import Security

// Read-only view of the Mac dashboard: which chunks exist, their transcription
// state, and the transcribed text. Data is fetched over the same TLS-pinned,
// token-authenticated channel the Watch already uses for uploads.

struct ChunkStatus: Codable, Identifiable {
    let chunk_id: String
    let started_at: String
    let duration: Double
    let status: String
    let text: String?
    var id: String { chunk_id }
}

struct StatusResponse: Codable {
    let ok: Bool
    let chunks: [ChunkStatus]
}

final class MacStatusClient: NSObject, ObservableObject, URLSessionTaskDelegate {
    @Published var chunks: [ChunkStatus] = []
    @Published var message = "尚未连接 Mac"
    @Published var loading = false
    @Published var lastUpdated: Date?

    private var activePairing: Pairing?
    private lazy var session: URLSession = URLSession(configuration: .default)

    func refresh() {
        let pairing = Pairing.load()
        guard !pairing.macURL.isEmpty else {
            message = "先在「配对」页粘贴 Mac 配对内容"
            return
        }
        activePairing = pairing
        loading = true
        message = "正在读取 Mac…"
        Task { await fetch() }
    }

    private func fetch() async {
        defer { Task { @MainActor in loading = false } }
        guard let pairing = activePairing,
              let endpoint = try? pairing.statusEndpoint() else {
            await MainActor.run { message = "配对信息无效，请重新配对" }
            return
        }
        var request = URLRequest(url: endpoint)
        request.setValue("Bearer " + pairing.token, forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 20
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await session.data(for: request, delegate: self)
            guard let http = response as? HTTPURLResponse else {
                await MainActor.run { message = "Mac 响应异常" }
                return
            }
            guard http.statusCode == 200 else {
                await MainActor.run { message = "Mac 返回 \(http.statusCode)" }
                return
            }
            let decoded = try JSONDecoder().decode(StatusResponse.self, from: data)
            await MainActor.run {
                chunks = decoded.chunks
                lastUpdated = Date()
                message = "已更新 · \(decoded.chunks.count) 段录音"
            }
        } catch is CancellationError {
            // Cancelled refresh, keep previous data.
        } catch {
            await MainActor.run { message = "连接失败：\(error.localizedDescription)" }
        }
    }

    // Same TLS policy as the Watch uploader: system trust or explicit pin.
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard let pairing = activePairing,
              challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let expectedHost = try? pairing.statusEndpoint().host,
              challenge.protectionSpace.host.caseInsensitiveCompare(expectedHost) == .orderedSame else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        if pairing.tlsMode == "system" {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        guard pairing.tlsMode == "pin", let trust = challenge.protectionSpace.serverTrust,
              let certificate = SecTrustGetCertificateAtIndex(trust, 0) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let digest = SHA256.hash(data: SecCertificateCopyData(certificate) as Data)
            .map { String(format: "%02x", $0) }.joined()
        guard digest == pairing.normalizedPin else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, expectedHost as CFString))
        SecTrustSetAnchorCertificates(trust, [certificate] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        var error: CFError?
        guard SecTrustEvaluateWithError(trust, &error) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

enum ChunkStatusLabel {
    static func title(_ status: String) -> String {
        switch status {
        case "pending", "queued": return "待转录"
        case "processing", "running": return "转录中"
        case "done": return "已完成"
        case "needs_review": return "待核对"
        case "failed": return "失败"
        case "budget_blocked": return "额度受限"
        case "derive_failed": return "派生失败"
        default: return status
        }
    }

    static func time(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        guard let date = formatter.date(from: iso) else { return iso }
        let out = DateFormatter()
        out.locale = Locale(identifier: "zh_CN")
        out.dateFormat = "MM-dd HH:mm"
        return out.string(from: date)
    }

    static func duration(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        if total < 60 { return "\(total) 秒" }
        let m = total / 60, s = total % 60
        return s == 0 ? "\(m) 分钟" : "\(m) 分 \(s) 秒"
    }
}
