import Foundation
import AVFoundation
import Combine
import WatchKit

// The tap copies PCM only; all encoding, hashing, manifest and file operations run on this queue.
private final class SegmentWriter {
    private let queue = DispatchQueue(label: "HemoryLocal.encoder", qos: .utility)
    private let slots = DispatchSemaphore(value: 32)
    private let store: ChunkStore
    private let sessionID: String
    private let onProgress: (Double) -> Void
    private let onChunk: (ChunkRecord) -> Void
    private let onError: (Error) -> Void
    private var file: AVAudioFile?
    private var record: ChunkRecord?
    private var sequence: Int
    private var totalSeconds: Double
    private var lastProgress: Double = 0
    private var progressInterval: Double = 10
    private var failed = false
    private var lastEndHostTime: Double?

    init(store: ChunkStore, sessionID: String, sequence: Int, totalSeconds: Double,
         onProgress: @escaping (Double) -> Void, onChunk: @escaping (ChunkRecord) -> Void,
         onError: @escaping (Error) -> Void) {
        self.store = store; self.sessionID = sessionID; self.sequence = sequence
        self.totalSeconds = totalSeconds; self.onProgress = onProgress
        self.lastProgress = totalSeconds
        self.onChunk = onChunk; self.onError = onError
    }

    func setDisplayActive(_ active: Bool) {
        queue.async {
            self.progressInterval = active ? 1 : 10
            if active {
                self.lastProgress = self.totalSeconds
                self.onProgress(self.totalSeconds)
            }
        }
    }

    func accept(_ input: AVAudioPCMBuffer, at time: AVAudioTime) {
        // A bounded queue prevents a slow encoder from accumulating unbounded PCM in memory.
        guard slots.wait(timeout: .now()) == .success else {
            queue.async { self.fail(RecorderFailure(message: "编码处理跟不上录音，已停止并记录音频缺口。")) }
            return
        }
        guard input.format.commonFormat == .pcmFormatFloat32, !input.format.isInterleaved,
              let source = input.floatChannelData,
              let mono = AVAudioFormat(standardFormatWithSampleRate: input.format.sampleRate, channels: 1),
              let copy = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: input.frameLength),
              let target = copy.floatChannelData else {
            slots.signal()
            queue.async { self.fail(RecorderFailure(message: "麦克风格式不受支持，已停止录音。")) }
            return
        }
        copy.frameLength = input.frameLength
        let channels = Int(input.format.channelCount)
        if channels == 1 {
            target[0].update(from: source[0], count: Int(input.frameLength))
        } else {
            for frame in 0..<Int(input.frameLength) {
                var value: Float = 0
                for channel in 0..<channels { value += source[channel][frame] }
                target[0][frame] = value / Float(channels)
            }
        }
        let hostSeconds = time.isHostTimeValid ? AVAudioTime.seconds(forHostTime: time.hostTime) : nil
        let captureDate = hostSeconds.map { Date(timeIntervalSinceNow: $0 - ProcessInfo.processInfo.systemUptime) } ?? Date()
        queue.async {
            defer { self.slots.signal() }
            guard !self.failed else { return }
            do {
                if let host = hostSeconds, let expected = self.lastEndHostTime, host - expected > 0.1 {
                    self.store.event("capture_gap", sessionID: self.sessionID,
                                     detail: "\(host - expected) seconds before \(ChunkStore.timestamp(captureDate))")
                }
                self.lastEndHostTime = hostSeconds.map { $0 + Double(copy.frameLength) / mono.sampleRate }
                if self.file == nil { try self.open(format: mono, at: captureDate) }
                guard self.file != nil, var record = self.record else { return }
                // No local AVAudioFile strong reference may survive the call to finishChunk().
                try self.file?.write(from: copy)
                record.frames += Int64(copy.frameLength)
                record.metadata.duration_seconds = Double(record.frames) / mono.sampleRate
                self.record = record
                self.totalSeconds += Double(copy.frameLength) / mono.sampleRate
                if floor(self.totalSeconds / self.progressInterval) > floor(self.lastProgress / self.progressInterval) {
                    self.lastProgress = self.totalSeconds
                    self.onProgress(self.totalSeconds)
                }
                if record.metadata.duration_seconds >= 300 { try self.finishChunk() }
            } catch { self.fail(error) }
        }
    }

    private func open(format: AVAudioFormat, at date: Date) throws {
        try store.checkDiskSpace()
        let id = UUID().uuidString.lowercased()
        let metadata = ChunkMetadata(chunk_id: id, session_id: sessionID,
            started_at: ChunkStore.timestamp(date), duration_seconds: 0, sequence: sequence,
            sha256: "", filename: id + ".m4a")
        let item = ChunkRecord(metadata: metadata, state: "writing", error: nil,
                               sample_rate: format.sampleRate, frames: 0)
        try store.save(item)
        record = item
        file = try AVAudioFile(forWriting: store.audioURL(id, partial: true), settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: format.sampleRate,
            AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 32_000,
            AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue
        ], commonFormat: .pcmFormatFloat32, interleaved: false)
        sequence += 1
    }

    private func finishChunk() throws {
        file = nil // Closes AAC container before rename/hash/upload.
        guard var item = record else { return }
        guard item.frames > 0 else { throw RecorderFailure(message: "没有采集到音频；保留空分片以便检查。") }
        let final = store.audioURL(item.metadata.chunk_id)
        try FileManager.default.moveItem(at: store.audioURL(item.metadata.chunk_id, partial: true), to: final)
        item.metadata.sha256 = try ChunkStore.hash(final)
        item.state = "pending"
        try store.save(item)
        record = nil
        onChunk(item)
    }

    private func fail(_ error: Error) {
        guard !failed else { return }
        failed = true
        store.event("recording_error", sessionID: sessionID, detail: error.localizedDescription)
        onError(error)
    }

    func finish(completion: @escaping (Int, Double) -> Void) {
        queue.async {
            do { try self.finishChunk() } catch { self.fail(error) }
            self.onProgress(self.totalSeconds)
            completion(self.sequence, self.totalSeconds)
        }
    }
}

final class Recorder: NSObject, ObservableObject {
    @Published private(set) var recording = false
    @Published private(set) var interrupted = false
    @Published private(set) var busy = false
    @Published private(set) var recordedSeconds: Double = 0
    @Published private(set) var message = "尚未开始录音"
    @Published private(set) var failureMessage: String?
    private let store: ChunkStore
    private let uploader: Uploader
    private var engine: AVAudioEngine?
    private var writer: SegmentWriter?
    private var sessionID: String?
    private var sequence = 0
    private var userWantsRecording = false
    private var interruptionEnded = false
    private var stopError: String?
    private var displayActive = false

    init(store: ChunkStore, uploader: Uploader) {
        self.store = store; self.uploader = uploader
        super.init()
        WKInterfaceDevice.current().isBatteryMonitoringEnabled = true
        NotificationCenter.default.addObserver(self, selector: #selector(audioInterrupted(_:)),
            name: AVAudioSession.interruptionNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(mediaReset),
            name: AVAudioSession.mediaServicesWereResetNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(configurationChanged),
            name: .AVAudioEngineConfigurationChange, object: nil)
    }

    func setDisplayActive(_ active: Bool) {
        displayActive = active
        writer?.setDisplayActive(active)
    }

    func start() {
        guard !busy, !userWantsRecording else { return }
        failureMessage = nil
        busy = true
        uploader.setRecording(true)
        AVAudioApplication.requestRecordPermission { allowed in
            DispatchQueue.main.async {
                self.busy = false
                guard allowed else {
                    self.message = "麦克风未授权，请在设置中允许录音。"
                    self.failureMessage = self.message
                    self.uploader.setRecording(false)
                    return
                }
                self.sessionID = UUID().uuidString.lowercased()
                self.sequence = 0; self.recordedSeconds = 0
                self.stopError = nil
                self.userWantsRecording = true
                self.beginEngine()
            }
        }
    }

    private func beginEngine() {
        guard let sessionID = sessionID, userWantsRecording else { return }
        do {
            try store.checkDiskSpace()
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .default, options: [])
            // watchOS does not support setPreferredSampleRate; use the actual hardware format.
            try session.setActive(true)
            let engine = AVAudioEngine()
            let format = engine.inputNode.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else {
                throw RecorderFailure(message: "麦克风没有可用输入。")
            }
            let writer = SegmentWriter(store: store, sessionID: sessionID, sequence: sequence,
                totalSeconds: recordedSeconds,
                onProgress: { seconds in DispatchQueue.main.async { self.recordedSeconds = seconds } },
                onChunk: { item in DispatchQueue.main.async {
                    self.store.event("chunk_saved", sessionID: sessionID, detail: item.metadata.chunk_id,
                        telemetry: self.batteryTelemetry(["frames": item.frames, "sample_rate": item.sample_rate,
                                                         "duration_seconds": item.metadata.duration_seconds,
                                                         "sequence": item.metadata.sequence]))
                    self.uploader.refreshStatus()
                } },
                onError: { error in DispatchQueue.main.async { self.stop(reason: error.localizedDescription) } })
            self.writer = writer; self.engine = engine
            writer.setDisplayActive(displayActive)
            engine.inputNode.installTap(onBus: 0, bufferSize: 8192, format: format) { buffer, time in
                writer.accept(buffer, at: time)
            }
            engine.prepare()
            try engine.start()
            recording = true; interrupted = false; busy = false
            message = "正在录音 · 停止后通过 Wi-Fi 同步"
            UserDefaults.standard.set(sessionID, forKey: "activeSessionID")
            store.event("recording_started", sessionID: sessionID,
                        telemetry: batteryTelemetry(["sample_rate": format.sampleRate]))
        } catch {
            stop(reason: "无法开始或恢复录音：\(error.localizedDescription)")
        }
    }

    func stop(reason: String? = nil) {
        if let reason = reason { stopError = reason; failureMessage = reason }
        userWantsRecording = false
        interrupted = false
        interruptionEnded = false
        UserDefaults.standard.removeObject(forKey: "activeSessionID")
        message = reason ?? "正在保存最后一个分片"
        drain { [weak self] in
            guard let self = self else { return }
            self.store.event("recording_stopped", sessionID: self.sessionID, detail: self.stopError ?? "用户停止",
                telemetry: self.batteryTelemetry(["recorded_seconds": self.recordedSeconds]))
            self.message = self.stopError ?? "已停止 · 等待 Wi-Fi 上传"
            self.uploader.setRecording(false)
        }
    }

    private func batteryTelemetry(_ fields: [String: Any] = [:]) -> [String: Any] {
        var result = fields
        let level = WKInterfaceDevice.current().batteryLevel
        if level >= 0 { result["battery_fraction"] = level }
        else { result["battery_fraction"] = NSNull() }
        result["battery_state"] = WKInterfaceDevice.current().batteryState.rawValue
        return result
    }

    private func drain(completion: @escaping () -> Void) {
        recording = false; busy = true
        if let engine = engine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        engine = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        let finishing = writer
        writer = nil
        guard let finishing = finishing else { busy = false; completion(); return }
        finishing.finish { nextSequence, seconds in
            DispatchQueue.main.async {
                self.sequence = nextSequence; self.recordedSeconds = seconds; self.busy = false
                completion()
            }
        }
    }

    @objc private func audioInterrupted(_ note: Notification) {
        DispatchQueue.main.async {
            guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
            if type == .began && self.userWantsRecording {
                self.interrupted = true; self.interruptionEnded = false
                self.message = "录音被系统中断；此段时间没有录音"
                self.store.event("interruption_began", sessionID: self.sessionID)
                self.drain {
                    if self.interruptionEnded && self.userWantsRecording { self.beginEngine() }
                }
            } else if type == .ended && self.userWantsRecording && self.interrupted {
                self.store.event("interruption_ended", sessionID: self.sessionID)
                let rawOptions = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
                guard AVAudioSession.InterruptionOptions(rawValue: rawOptions).contains(.shouldResume) else {
                    self.stop(reason: "中断结束但系统未允许自动恢复，请重新点开始。")
                    return
                }
                self.interruptionEnded = true
                if !self.busy { self.beginEngine() }
            }
        }
    }

    @objc private func mediaReset() {
        DispatchQueue.main.async {
            if self.userWantsRecording { self.stop(reason: "系统音频服务重置，已停止；请重新点开始。") }
        }
    }

    @objc private func configurationChanged(_ note: Notification) {
        DispatchQueue.main.async {
            guard self.userWantsRecording, !self.interrupted, let current = self.engine,
                  let changed = note.object as? AVAudioEngine, changed === current else { return }
            self.stop(reason: "麦克风路由改变，已保存录音；请重新点开始。")
        }
    }
}
