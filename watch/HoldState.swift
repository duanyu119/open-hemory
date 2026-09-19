import Foundation
import CoreGraphics

/// One contact owns one intent and generation. Cancellation stays latched until release.
/// Callers supply monotonic seconds (never Date) so tests exercise the production reducer.
struct HoldState {
    static let stopDuration: TimeInterval = 2
    static let tapDuration: TimeInterval = 0.35
    static let movementTolerance: CGFloat = 24

    enum Intent { case stop, start }
    enum Effect: Equatable { case none, stop, start }
    enum Phase: Equatable { case idle, holdingStop, trackingTap, cancelled, consumed }

    private(set) var phase: Phase = .idle
    private(set) var generation: UInt64 = 0
    private var startedAt: TimeInterval = 0
    private var origin: CGPoint = .zero

    var isHolding: Bool { phase == .holdingStop }
    var deadline: TimeInterval? { isHolding ? startedAt + Self.stopDuration : nil }

    mutating func begin(at now: TimeInterval, origin: CGPoint, intent: Intent,
                        allowed: Bool, bounds: CGRect) {
        guard phase == .idle else { return }
        generation &+= 1
        startedAt = now
        self.origin = origin
        phase = allowed && bounds.contains(origin)
            ? (intent == .stop ? .holdingStop : .trackingTap) : .cancelled
    }

    mutating func move(to location: CGPoint, allowed: Bool, bounds: CGRect) {
        guard phase == .holdingStop || phase == .trackingTap else { return }
        let dx = location.x - origin.x
        let dy = location.y - origin.y
        if !allowed || !bounds.contains(location) ||
            dx * dx + dy * dy > Self.movementTolerance * Self.movementTolerance {
            cancel()
        }
    }

    func progress(at now: TimeInterval) -> Double {
        guard isHolding else { return 0 }
        return min(1, max(0, (now - startedAt) / Self.stopDuration))
    }

    mutating func fire(generation token: UInt64, at now: TimeInterval,
                       allowed: Bool, canStop: Bool) -> Effect {
        guard token == generation, isHolding else { return .none }
        guard allowed && canStop else { cancel(); return .none }
        guard now >= startedAt + Self.stopDuration else { return .none }
        phase = .consumed
        return .stop
    }

    mutating func end(at now: TimeInterval, location: CGPoint, bounds: CGRect,
                      allowed: Bool, canStop: Bool, canStart: Bool) -> Effect {
        move(to: location, allowed: allowed, bounds: bounds)
        defer { release() }
        switch phase {
        case .holdingStop:
            // Handle a delayed main-queue timer at release using the same deadline.
            return fire(generation: generation, at: now, allowed: allowed, canStop: canStop)
        case .trackingTap:
            let duration = now - startedAt
            return allowed && canStart && duration >= 0 && duration <= Self.tapDuration ? .start : .none
        case .idle, .cancelled, .consumed:
            return .none
        }
    }

    mutating func cancel() {
        guard phase != .idle && phase != .cancelled else { return }
        generation &+= 1
        phase = .cancelled
    }

    /// Also used when SwiftUI resets GestureState without delivering onEnded.
    mutating func release() {
        generation &+= 1
        phase = .idle
    }
}
