import Foundation
import AVFoundation
import CryptoKit
import Security

struct RecorderFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct Pairing: Codable, Equatable {
    var macURL: String = ""
    var token: String = ""
    var tlsMode: String = "pin"
    var certificateSHA256: String = ""
    var cfAccessClientID: String = ""
    var cfAccessClientSecret: String = ""
    var allowMobileData: Bool = false

    enum CodingKeys: String, CodingKey {
        case macURL = "mac_url", token, tlsMode = "tls_mode"
        case certificateSHA256 = "certificate_sha256"
        case cfAccessClientID = "cf_access_client_id", cfAccessClientSecret = "cf_access_client_secret"
        case allowMobileData = "allow_mobile_data"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        macURL = try values.decodeIfPresent(String.self, forKey: .macURL) ?? ""
        token = try values.decodeIfPresent(String.self, forKey: .token) ?? ""
        tlsMode = try values.decodeIfPresent(String.self, forKey: .tlsMode) ?? "pin"
        certificateSHA256 = try values.decodeIfPresent(String.self, forKey: .certificateSHA256) ?? ""
        cfAccessClientID = try values.decodeIfPresent(String.self, forKey: .cfAccessClientID) ?? ""
        cfAccessClientSecret = try values.decodeIfPresent(String.self, forKey: .cfAccessClientSecret) ?? ""
        allowMobileData = try values.decodeIfPresent(Bool.self, forKey: .allowMobileData) ?? false
    }

    var normalizedPin: String {
        certificateSHA256.lowercased().replacingOccurrences(of: ":", with: "")
            .filter { !$0.isWhitespace }
    }

    func baseURL() throws -> URL {
        guard let parts = URLComponents(string: macURL.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme == "https", let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/", let base = parts.url,
              token.count >= 32, ["pin", "system"].contains(tlsMode),
              (tlsMode == "system" || (normalizedPin.count == 64 &&
                  normalizedPin.allSatisfy({ $0.isHexDigit && $0.isASCII }))),
              cfAccessClientID.isEmpty == cfAccessClientSecret.isEmpty else {
            throw RecorderFailure(message: "填写 HTTPS 主机地址和至少 32 字符令牌。指纹模式需 64 位 SHA-256；Access ID/Secret 必须同时填写。")
        }
        return base
    }

    func endpoint() throws -> URL {
        try baseURL().appendingPathComponent("v1/chunks")
    }

    func statusEndpoint() throws -> URL {
        try baseURL().appendingPathComponent("v1/status")
    }

    static func fromJSON(_ text: String) throws -> Pairing {
        guard let data = text.data(using: .utf8), !data.isEmpty, data.count <= 8192,
              let value = try? JSONDecoder().decode(Pairing.self, from: data) else {
            throw RecorderFailure(message: "配对内容格式不正确，请粘贴 Mac 生成的完整配对 JSON。")
        }
        _ = try value.endpoint()
        return value
    }

    static func load() -> Pairing {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "HemoryLocalPairing", kSecAttrAccount as String: "mac",
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data, let value = try? JSONDecoder().decode(Pairing.self, from: data)
        else { return Pairing() }
        return value
    }

    func save() throws {
        _ = try endpoint()
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "HemoryLocalPairing", kSecAttrAccount as String: "mac"]
        let data = try JSONEncoder().encode(self)
        let update = [kSecValueData as String: data]
        let status = SecItemUpdate(query as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = data
            add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            guard SecItemAdd(add as CFDictionary, nil) == errSecSuccess else {
                throw RecorderFailure(message: "无法保存配对信息到钥匙串。")
            }
        } else if status != errSecSuccess {
            throw RecorderFailure(message: "无法更新钥匙串：\(status)")
        }
    }
}

struct ChunkMetadata: Codable {
    var chunk_id: String
    var session_id: String
    var started_at: String
    var duration_seconds: Double
    var sequence: Int
    var sha256: String
    var filename: String
}

struct ChunkRecord: Codable {
    var metadata: ChunkMetadata
    // writing -> pending -> uploaded. blocked requires an explicit retry/config fix.
    var state: String
    var error: String?
    var sample_rate: Double
    var frames: Int64
}

struct StoreSnapshot {
    var pending: Int = 0
    var uploaded: Int = 0
    var blocked: Int = 0
    var orphaned: Int = 0
    var lastError: String?
}

final class ChunkStore {
    let root: URL
    private let lock = NSRecursiveLock()
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private var recoveryErrors: [String] = []

    init() throws {
        root = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                          appropriateFor: nil, create: true)
            .appendingPathComponent("HemoryLocal", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        var value = root
        var resourceValues = URLResourceValues()
        resourceValues.isExcludedFromBackup = true
        try value.setResourceValues(resourceValues)
        encoder.outputFormatting = [.sortedKeys]
        recover()
    }

    static func timestamp(_ date: Date = Date()) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

    static func hash(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let data = try handle.read(upToCount: 128 * 1024), !data.isEmpty { hasher.update(data: data) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    func audioURL(_ id: String, partial: Bool = false) -> URL {
        root.appendingPathComponent(id + (partial ? ".partial.m4a" : ".m4a"))
    }

    func save(_ record: ChunkRecord) throws {
        lock.lock(); defer { lock.unlock() }
        try encoder.encode(record).write(to: root.appendingPathComponent(record.metadata.chunk_id + ".json"),
                                        options: .atomic)
    }

    func records() -> [ChunkRecord] {
        lock.lock(); defer { lock.unlock() }
        guard let files = try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        else { return [] }
        return files.filter { $0.pathExtension == "json" }.compactMap {
            guard let data = try? Data(contentsOf: $0) else { return nil }
            return try? decoder.decode(ChunkRecord.self, from: data)
        }.sorted { $0.metadata.started_at < $1.metadata.started_at }
    }

    func record(_ id: String) -> ChunkRecord? { records().first { $0.metadata.chunk_id == id } }

    func update(_ id: String, state: String, error: String? = nil) throws {
        lock.lock(); defer { lock.unlock() }
        guard var item = record(id) else { throw RecorderFailure(message: "找不到分片清单：\(id)") }
        item.state = state
        item.error = error
        try save(item)
    }

    func event(_ type: String, sessionID: String? = nil, detail: String = "", telemetry: [String: Any] = [:]) {
        lock.lock(); defer { lock.unlock() }
        var value: [String: Any] = ["at": Self.timestamp(), "type": type, "session_id": sessionID ?? "", "detail": detail]
        value["telemetry"] = telemetry
        guard var data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        data.append(0x0a)
        let url = root.appendingPathComponent("events.jsonl")
        do {
            if !FileManager.default.fileExists(atPath: url.path) { try Data().write(to: url, options: .atomic) }
            let handle = try FileHandle(forWritingTo: url)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
        } catch { recoveryErrors.append("事件日志写入失败：\(error.localizedDescription)") }
    }

    func checkDiskSpace() throws {
        let attributes = try FileManager.default.attributesOfFileSystem(forPath: root.path)
        guard let bytes = attributes[.systemFreeSize] as? NSNumber, bytes.int64Value > 50 * 1024 * 1024 else {
            throw RecorderFailure(message: "手表存储剩余不足 50 MB，已停止录音。原音保留，请导出后人工清理。")
        }
    }

    func snapshot() -> StoreSnapshot {
        lock.lock(); defer { lock.unlock() }
        var result = StoreSnapshot()
        for record in records() {
            switch record.state {
            case "pending": result.pending += 1
            case "uploaded": result.uploaded += 1
            case "blocked", "needsmanual": result.blocked += 1
            case "orphan": result.orphaned += 1
            default: break
            }
            if let error = record.error { result.lastError = error }
        }
        result.orphaned += untrackedFiles().count
        result.lastError = recoveryErrors.last ?? result.lastError
        return result
    }

    private func untrackedFiles() -> [URL] {
        let known = Set(records().map { $0.metadata.chunk_id })
        return ((try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "m4a" && !known.contains($0.lastPathComponent.components(separatedBy: ".")[0]) }
    }

    private func recover() {
        if let previous = UserDefaults.standard.string(forKey: "activeSessionID") {
            event("process_relaunched", sessionID: previous, detail: "上次录音意外结束；没有自动重开麦克风。")
            UserDefaults.standard.removeObject(forKey: "activeSessionID")
        }
        for var item in records() where item.state == "writing" {
            do {
                let final = audioURL(item.metadata.chunk_id)
                let partial = audioURL(item.metadata.chunk_id, partial: true)
                let source = FileManager.default.fileExists(atPath: final.path) ? final : partial
                let file = try AVAudioFile(forReading: source)
                guard file.length > 0 else { throw RecorderFailure(message: "文件没有可读取音频。") }
                item.frames = file.length
                item.sample_rate = file.processingFormat.sampleRate
                item.metadata.duration_seconds = Double(file.length) / item.sample_rate
                if source != final { try FileManager.default.moveItem(at: source, to: final) }
                item.metadata.sha256 = try Self.hash(final)
                item.state = "pending"
                item.error = nil
                try save(item)
                event("recovered_chunk", sessionID: item.metadata.session_id, detail: item.metadata.chunk_id)
            } catch {
                item.state = "orphan"
                item.error = "中断文件无法完整恢复，已保留：\(item.metadata.chunk_id)"
                try? save(item)
                recoveryErrors.append(item.error!)
            }
        }
        if !untrackedFiles().isEmpty { recoveryErrors.append("存在无清单音频，已保留，需人工核对。") }
    }
}
