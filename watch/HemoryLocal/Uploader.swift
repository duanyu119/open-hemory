import Foundation
import Combine
import CryptoKit
import Security
import WatchKit
import Network

final class Uploader: NSObject, ObservableObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    static let sessionIdentifier = (Bundle.main.bundleIdentifier ?? "org.example.openhemory.watchkitapp") + ".uploads.v1"
    @Published private(set) var snapshot = StoreSnapshot()
    @Published private(set) var message = "尚未配对 Mac"
    private let store: ChunkStore
    private let queue = DispatchQueue(label: "HemoryLocal.uploads", qos: .utility)
    private let delegates = OperationQueue()
    private var config = Pairing.load()
    private var responses: [Int: Data] = [:]
    private var attempted = Set(UserDefaults.standard.stringArray(forKey: "syncAttemptedIDs") ?? [])
    private var filling = false
    private var refreshRequested = false
    private var recording = false
    private let networkMonitor = NWPathMonitor()
    private var usableNetwork = false
    private var backgroundTasks: [WKURLSessionRefreshBackgroundTask] = []
    private var retryTasks: [WKApplicationRefreshBackgroundTask] = []
    private var eventsFinished = false
    private var sessionStorage: URLSession?
    private var pendingConfiguration: Pairing?
    private var session: URLSession {
        if let existing = sessionStorage { return existing }
        let options = URLSessionConfiguration.background(withIdentifier: Self.sessionIdentifier)
        options.sessionSendsLaunchEvents = true
        options.isDiscretionary = false // Queue at stop; the OS still controls background scheduling.
        options.allowsCellularAccess = false
        options.allowsExpensiveNetworkAccess = false
        options.allowsConstrainedNetworkAccess = false
        options.waitsForConnectivity = true // Background sessions always wait; kept explicit for intent.
        options.httpMaximumConnectionsPerHost = 2
        options.timeoutIntervalForRequest = 60
        options.timeoutIntervalForResource = 24 * 60 * 60
        options.urlCache = nil
        let created = URLSession(configuration: options, delegate: self, delegateQueue: delegates)
        sessionStorage = created
        return created
    }

    init(store: ChunkStore) {
        self.store = store
        super.init()
        delegates.maxConcurrentOperationCount = 1
        delegates.underlyingQueue = queue
        delegates.qualityOfService = .utility
        queue.async { _ = self.session; self.publish() }
        networkMonitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            // The paired iPhone may proxy the connection, so do not require a Watch-local Wi-Fi interface.
            // URLSession enforces the no-cellular/no-expensive policy for the actual transfer.
            let usable = path.status == .satisfied && !path.isExpensive && !path.isConstrained
            let recovered = usable && !self.usableNetwork
            self.usableNetwork = usable
            if recovered && !self.recording {
                self.resetRetryBudget()
                self.clearAttempted()
                self.fillQueue()
            }
        }
        networkMonitor.start(queue: queue)
    }

    func setRecording(_ active: Bool) {
        queue.async {
            self.recording = active
            if active {
                self.session.getAllTasks { tasks in
                    self.queue.async {
                        guard self.recording else { return }
                        tasks.filter { $0.state == .running }.forEach { $0.suspend() }
                    }
                }
                self.report("录音保存在手表；停止后通过 Wi-Fi 上传")
            } else {
                self.resetRetryBudget()
                self.clearAttempted()
                self.fillQueue()
            }
        }
    }

    func refreshStatus() { queue.async { self.publish() } }

    func enqueuePending(resetRetryBudget: Bool = true) {
        queue.async {
            if resetRetryBudget { self.resetRetryBudget() }
            self.clearAttempted()
            self.fillQueue()
        }
    }

    private func clearAttempted() {
        attempted.removeAll()
        UserDefaults.standard.removeObject(forKey: "syncAttemptedIDs")
    }

    private func resetRetryBudget() {
        UserDefaults.standard.set(0, forKey: "syncRetryCount")
        UserDefaults.standard.removeObject(forKey: "syncRetryAt")
        for item in store.records() where item.state == "needsmanual" {
            do { try store.update(item.metadata.chunk_id, state: "pending") }
            catch { report("无法更新重试状态：\(error.localizedDescription)") }
        }
    }

    private func requestDeferredRetry(for id: String) {
        let defaults = UserDefaults.standard
        if let scheduled = defaults.object(forKey: "syncRetryAt") as? Date, scheduled > Date() { return }
        let count = defaults.integer(forKey: "syncRetryCount")
        guard count < 3 else {
            let detail = "自动重试已达 3 次，原音保留；检查网络后打开 App 或点重试。"
            try? store.update(id, state: "needsmanual", error: detail)
            report(detail)
            return
        }
        let preferredDate = Date(timeIntervalSinceNow: 15 * 60 * pow(2, Double(count)))
        defaults.set(count + 1, forKey: "syncRetryCount")
        defaults.set(preferredDate, forKey: "syncRetryAt")
        store.event("sync_retry_requested", detail: "attempt=\(count + 1), preferred=\(ChunkStore.timestamp(preferredDate))")
        DispatchQueue.main.async {
            WKApplication.shared().scheduleBackgroundRefresh(withPreferredDate: preferredDate,
                userInfo: "HemoryLocalSyncRetry" as NSString) { error in
                guard let error = error else { return }
                self.queue.async {
                    defaults.removeObject(forKey: "syncRetryAt")
                    let detail = "系统未接受后台重试请求，原音保留；打开 App 可重试：\(error.localizedDescription)"
                    try? self.store.update(id, state: "needsmanual", error: detail)
                    self.report(detail)
                    self.publish()
                }
            }
        }
    }

    func handleRetryTask(_ task: WKApplicationRefreshBackgroundTask) {
        queue.async {
            self.retryTasks.append(task)
            task.expirationHandler = { [weak self, weak task] in
                guard let self = self, let task = task else { return }
                self.queue.async {
                    if self.retryTasks.contains(where: { $0 === task }) {
                        self.retryTasks.removeAll { $0 === task }
                        task.setTaskCompletedWithSnapshot(false)
                    }
                }
            }
            UserDefaults.standard.removeObject(forKey: "syncRetryAt")
            self.clearAttempted()
            self.fillQueue()
        }
    }

    private func finishRetryTasks() {
        retryTasks.forEach { $0.setTaskCompletedWithSnapshot(false) }
        retryTasks.removeAll()
    }

    func retryAfterConfigurationChange() {
        queue.async {
            self.resetRetryBudget()
            for item in self.store.records() where item.state == "blocked" {
                do { try self.store.update(item.metadata.chunk_id, state: "pending") }
                catch { self.report("无法更新重试状态：\(error.localizedDescription)") }
            }
            let updated = Pairing.load()
            if updated != self.config {
                self.pendingConfiguration = updated
                self.report("正在更新连接设置；原音保留")
                self.session.invalidateAndCancel()
            } else { self.clearAttempted(); self.fillQueue() }
        }
    }

    func urlSession(_ session: URLSession, didBecomeInvalidWithError error: Error?) {
        if let updated = pendingConfiguration {
            config = updated
            pendingConfiguration = nil
            sessionStorage = nil
            clearAttempted()
            _ = self.session
            fillQueue()
        }
    }

    private func fillQueue() {
        guard pendingConfiguration == nil, !recording else { finishRetryTasks(); return }
        if filling { refreshRequested = true; return }
        let endpoint: URL
        do { endpoint = try config.endpoint() }
        catch { report("未完成 Mac 配对；音频继续保留在手表。"); publish(); finishRetryTasks(); return }
        filling = true
        session.getAllTasks { tasks in
            self.queue.async {
                defer {
                    self.finishRetryTasks() // Transfers are now queued with the system; do not wait for network.
                    self.filling = false
                    if self.refreshRequested { self.refreshRequested = false; self.fillQueue() }
                }
                let live = tasks.filter { $0.state != .completed && $0.state != .canceling }
                guard self.pendingConfiguration == nil, !self.recording else { return }
                // Requeue legacy build tasks under the Wi-Fi-only request policy; keep every local file.
                let legacy = live.filter { $0.originalRequest?.allowsCellularAccess != false }
                if !legacy.isEmpty {
                    legacy.forEach { task in
                        if let id = task.taskDescription { self.attempted.remove(id) }
                        task.cancel()
                    }
                    UserDefaults.standard.set(Array(self.attempted), forKey: "syncAttemptedIDs")
                    self.refreshRequested = true
                    return
                }
                live.filter { $0.state == .suspended }.forEach { $0.resume() }
                let activeIDs = Set(live.compactMap { $0.taskDescription })
                // Enqueue a whole batch before suspension. Connection count, not task count, is limited to 2.
                // 144 chunks is 12 hours at five minutes per chunk and bounds a single scheduling pass.
                let available = max(0, 144 - live.count)
                let pending = self.store.records().filter {
                    $0.state == "pending" && !activeIDs.contains($0.metadata.chunk_id) &&
                        !self.attempted.contains($0.metadata.chunk_id)
                }.prefix(available)
                for item in pending {
                    let id = item.metadata.chunk_id
                    self.attempted.insert(id)
                    UserDefaults.standard.set(Array(self.attempted), forKey: "syncAttemptedIDs")
                    do {
                        let url = self.store.audioURL(id)
                        guard try ChunkStore.hash(url) == item.metadata.sha256 else {
                            throw RecorderFailure(message: "本地文件 SHA-256 已变化，未发送。")
                        }
                        var request = URLRequest(url: endpoint)
                        request.httpMethod = "POST"
                        request.allowsCellularAccess = false
                        request.allowsExpensiveNetworkAccess = false
                        request.allowsConstrainedNetworkAccess = false
                        request.setValue("Bearer " + self.config.token, forHTTPHeaderField: "Authorization")
                        request.setValue("audio/mp4", forHTTPHeaderField: "Content-Type")
                        request.setValue(try JSONEncoder().encode(item.metadata).base64EncodedString(),
                                         forHTTPHeaderField: "X-Chunk-Metadata")
                        if !self.config.cfAccessClientID.isEmpty {
                            request.setValue(self.config.cfAccessClientID, forHTTPHeaderField: "CF-Access-Client-Id")
                            request.setValue(self.config.cfAccessClientSecret, forHTTPHeaderField: "CF-Access-Client-Secret")
                        }
                        let task = self.session.uploadTask(with: request, fromFile: url)
                        task.taskDescription = id
                        self.eventsFinished = false
                        task.resume()
                        self.report("同步已排队；等待 Wi-Fi 和系统调度")
                    } catch {
                        try? self.store.update(id, state: "blocked", error: error.localizedDescription)
                        self.report(error.localizedDescription)
                    }
                }
                self.publish()
            }
        }
    }

    private func report(_ value: String) { DispatchQueue.main.async { self.message = value } }
    private func publish() {
        let value = store.snapshot()
        DispatchQueue.main.async { self.snapshot = value }
    }

    func handleBackgroundTask(_ task: WKURLSessionRefreshBackgroundTask) {
        queue.async {
            guard task.sessionIdentifier == Self.sessionIdentifier else {
                task.setTaskCompletedWithSnapshot(false)
                return
            }
            _ = self.session
            if self.eventsFinished { task.setTaskCompletedWithSnapshot(false) }
            else {
                self.backgroundTasks.append(task)
                task.expirationHandler = { [weak self, weak task] in
                    guard let self = self, let task = task else { return }
                    self.queue.async {
                        self.backgroundTasks.removeAll { $0 === task }
                        task.setTaskCompletedWithSnapshot(false)
                    }
                }
            }
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        eventsFinished = true
        backgroundTasks.forEach { $0.setTaskCompletedWithSnapshot(false) }
        backgroundTasks.removeAll()
        publish()
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let expectedHost = try? config.endpoint().host,
              challenge.protectionSpace.host.caseInsensitiveCompare(expectedHost) == .orderedSame else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        if config.tlsMode == "system" {
            // System CA + hostname validation, never a fallback from pin mode.
            completionHandler(.performDefaultHandling, nil)
            return
        }
        guard config.tlsMode == "pin", let trust = challenge.protectionSpace.serverTrust,
              let certificate = SecTrustGetCertificateAtIndex(trust, 0) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let digest = SHA256.hash(data: SecCertificateCopyData(certificate) as Data)
            .map { String(format: "%02x", $0) }.joined()
        guard digest == config.normalizedPin else {
            report("Mac 证书指纹不匹配；已阻止上传。")
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        // The explicit pin is a private trust anchor; still validate hostname and certificate dates.
        SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, expectedHost as CFString))
        SecTrustSetAnchorCertificates(trust, [certificate] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        var error: CFError?
        guard SecTrustEvaluateWithError(trust, &error) else {
            report("Mac 证书名称或有效期验证失败；已阻止上传。")
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        // Background URLSession may automatically follow redirects on some OS versions.
        // Server endpoint must never redirect; final response URL is also checked before accepting ACK.
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        let current = responses[dataTask.taskIdentifier, default: Data()]
        guard current.count + data.count <= 65_536 else { dataTask.cancel(); return }
        responses[dataTask.taskIdentifier, default: Data()].append(data)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        defer { responses.removeValue(forKey: task.taskIdentifier); publish(); fillQueue() }
        guard let id = task.taskDescription, let item = store.record(id), item.state != "uploaded" else { return }
        let responseData = responses[task.taskIdentifier] ?? Data()
        do {
            if let error = error {
                let detail = "网络/TLS 未完成，原音保留；恢复 Wi-Fi 后重试：\(error.localizedDescription)"
                try store.update(id, state: "pending", error: detail)
                report(detail)
                if (error as NSError).code != NSURLErrorCancelled { requestDeferredRetry(for: id) }
                return
            }
            guard let response = task.response as? HTTPURLResponse else {
                throw RecorderFailure(message: "没有有效 HTTP 回执，未标记同步。")
            }
            let status = response.statusCode
            guard response.url == (try config.endpoint()) else {
                throw RecorderFailure(message: "服务器发生重定向或地址不匹配，未接受回执。")
            }
            if status == 429 || status >= 500 {
                let detail = "Mac HTTP \(status)，保留分片等待下次重试。"
                try store.update(id, state: "pending", error: detail)
                report(detail)
                requestDeferredRetry(for: id)
                return
            }
            guard status == 200 || status == 201 else {
                let detail = status == 401 ? "Mac 鉴权失败 (401)，请核对配对令牌后点重试。" :
                    "Mac 拒绝分片 (HTTP \(status))，请检查 Mac 日志后点重试。"
                throw RecorderFailure(message: detail)
            }
            struct Receipt: Decodable { let chunk_id: String; let sha256: String; let stored: Bool }
            let receipt = try JSONDecoder().decode(Receipt.self, from: responseData)
            guard receipt.stored, receipt.chunk_id == id, receipt.sha256 == item.metadata.sha256 else {
                throw RecorderFailure(message: "Mac 回执 ID/SHA-256 不匹配，未标记同步。")
            }
            try store.update(id, state: "uploaded")
            store.event("uploaded", sessionID: item.metadata.session_id, detail: id)
            report("Mac 已确认接收；手表原音保留")
        } catch {
            do { try store.update(id, state: "blocked", error: error.localizedDescription) }
            catch { report("无法保存同步状态，原音保留：\(error.localizedDescription)") }
            report(error.localizedDescription)
        }
    }
}
