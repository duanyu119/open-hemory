import Foundation
import CoreGraphics

@main
struct HoldStateTests {
    static let bounds = CGRect(x: 0, y: 0, width: 200, height: 200)
    static let center = CGPoint(x: 100, y: 100)
    static var assertions = 0
    static var scenarios = 0

    static func expect(_ value: @autoclosure () -> Bool, _ message: String,
                       file: StaticString = #file, line: UInt = #line) {
        assertions += 1
        precondition(value(), message, file: file, line: line)
    }

    static func test(_ name: String, _ body: () -> Void) {
        body()
        scenarios += 1
        print("PASS \(name)")
    }

    static func began(_ intent: HoldState.Intent = .stop, at: Double = 100) -> HoldState {
        var state = HoldState()
        state.begin(at: at, origin: center, intent: intent, allowed: true, bounds: bounds)
        return state
    }

    static func end(_ state: inout HoldState, at: Double, location: CGPoint = center,
                    allowed: Bool = true, canStop: Bool = true) -> HoldState.Effect {
        state.end(at: at, location: location, bounds: bounds, allowed: allowed,
                  canStop: canStop, canStart: !canStop)
    }

    static func main() {
        test("progress starts at zero and shares the two-second deadline") {
            let state = began()
            expect(state.deadline == 102, "deadline")
            expect(state.progress(at: 100) == 0, "initial progress")
            expect(state.progress(at: 100.5) == 0.25, "quarter progress at 0.5 seconds")
            expect(state.progress(at: 101) == 0.5, "halfway progress")
            expect(state.progress(at: 102) == 1, "complete progress")
            expect(state.progress(at: 103) == 1, "clamped progress")
            expect(state.progress(at: 99) == 0, "negative sample clamp")
        }
        test("short recording press does not stop") {
            var state = began()
            expect(end(&state, at: 101.999) == .none, "early release")
            expect(state.phase == .idle, "released")
        }
        test("early timer cannot stop; exact deadline stops once") {
            var state = began()
            let token = state.generation
            expect(state.fire(generation: token, at: 101.999, allowed: true, canStop: true) == .none, "early timer")
            expect(state.fire(generation: token, at: 102, allowed: true, canStop: true) == .stop, "deadline")
            expect(state.fire(generation: token, at: 105, allowed: true, canStop: true) == .none, "no duplicate stop")
        }
        test("late release after automatic stop never restarts") {
            var state = began()
            expect(state.fire(generation: state.generation, at: 102, allowed: true, canStop: true) == .stop, "stop")
            expect(end(&state, at: 120, canStop: false) == .none, "late release must not start")
            expect(state.phase == .idle, "release consumed touch")
        }
        test("release at deadline handles delayed timer") {
            var state = began()
            let token = state.generation
            expect(end(&state, at: 102) == .stop, "release stop")
            expect(state.fire(generation: token, at: 103, allowed: true, canStop: true) == .none, "old timer")
        }
        for intent in [HoldState.Intent.stop, .start] {
            test("24pt boundary is accepted for \(intent)") {
                var state = began(intent)
                let point = CGPoint(x: 124, y: 100)
                state.move(to: point, allowed: true, bounds: bounds)
                expect(state.phase != .cancelled, "exact boundary")
                expect(end(&state, at: intent == .stop ? 102 : 100.2,
                           location: point, canStop: intent == .stop) == (intent == .stop ? .stop : .start), "valid boundary action")
            }
            test("over 24pt latches cancellation despite returning inside for \(intent)") {
                var state = began(intent)
                let token = state.generation
                state.move(to: CGPoint(x: 124.01, y: 100), allowed: true, bounds: bounds)
                state.move(to: center, allowed: true, bounds: bounds)
                state.begin(at: 101, origin: center, intent: intent, allowed: true, bounds: bounds)
                expect(state.phase == .cancelled, "cannot rearm same touch")
                expect(state.progress(at: 103) == 0, "cancel clears progress")
                expect(state.fire(generation: token, at: 105, allowed: true, canStop: true) == .none, "cancel invalidates timer")
                expect(end(&state, at: 105, canStop: intent == .stop) == .none, "no release action")
            }
            test("leaving face cancels even below movement tolerance for \(intent)") {
                var state = HoldState()
                state.begin(at: 100, origin: CGPoint(x: 5, y: 5), intent: intent, allowed: true, bounds: bounds)
                state.move(to: CGPoint(x: -1, y: 5), allowed: true, bounds: bounds)
                expect(state.phase == .cancelled, "left bounds")
                expect(end(&state, at: 100.1, location: CGPoint(x: 5, y: 5), canStop: intent == .stop) == .none, "no reentry action")
            }
        }
        test("diagonal movement uses radial distance") {
            var state = began()
            state.move(to: CGPoint(x: 118, y: 118), allowed: true, bounds: bounds)
            expect(state.phase == .cancelled, "diagonal exceeds 24pt")
        }
        test("final release location is validated without onChanged") {
            var state = began()
            expect(end(&state, at: 103, location: CGPoint(x: 130, y: 100)) == .none, "invalid final location")
        }
        for (duration, expected) in [(0.0, HoldState.Effect.start), (0.2, .start), (0.35, .start), (0.351, .none), (2.0, .none), (10.0, .none)] {
            test("stopped contact duration \(duration)s") {
                var state = began(.start, at: 0)
                expect(state.deadline == nil, "tap schedules no stop")
                expect(state.progress(at: duration) == 0, "tap shows no progress")
                expect(end(&state, at: duration, canStop: false) == expected, "tap duration gate")
            }
        }
        for reason in ["inactive", "background", "disappear", "luminance", "busy"] {
            test("\(reason) cancellation survives reactivation and stale callbacks") {
                var state = began()
                let token = state.generation
                state.cancel()
                state.begin(at: 101, origin: center, intent: .stop, allowed: true, bounds: bounds)
                expect(state.phase == .cancelled, "same contact remains cancelled")
                expect(state.fire(generation: token, at: 103, allowed: true, canStop: true) == .none, "cancelled callback")
                expect(end(&state, at: 103) == .none, "cancelled end")
                state.begin(at: 104, origin: center, intent: .stop, allowed: true, bounds: bounds)
                expect(state.fire(generation: token, at: 107, allowed: true, canStop: true) == .none, "old callback cannot stop new hold")
                expect(state.fire(generation: state.generation, at: 106, allowed: true, canStop: true) == .stop, "new contact works")
            }
        }
        test("timer independently checks display eligibility and recorder state") {
            for (allowed, canStop) in [(false, true), (true, false)] {
                var state = began()
                expect(state.fire(generation: state.generation, at: 102, allowed: allowed, canStop: canStop) == .none, "invalid context")
                expect(state.phase == .cancelled, "invalid context latches")
            }
        }
        test("gesture reset without onEnded invalidates old work and allows a fresh tap") {
            var state = began()
            let token = state.generation
            state.release()
            expect(state.fire(generation: token, at: 103, allowed: true, canStop: true) == .none, "reset invalidates timer")
            state.begin(at: 104, origin: center, intent: .start, allowed: true, bounds: bounds)
            expect(end(&state, at: 104.2, canStop: false) == .start, "new tap")
        }
        test("unavailable begin cannot rearm mid-contact") {
            var state = HoldState()
            state.begin(at: 100, origin: center, intent: .start, allowed: false, bounds: bounds)
            state.begin(at: 100.1, origin: center, intent: .start, allowed: true, bounds: bounds)
            expect(end(&state, at: 100.2, canStop: false) == .none, "busy begin remains cancelled")
        }
        test("recording becoming stopped does not turn original hold into start") {
            var state = began()
            expect(end(&state, at: 100.2, canStop: false) == .none, "fixed contact intent")
        }
        test("stopped becoming recording does not turn original tap into stop") {
            var state = began(.start)
            expect(end(&state, at: 102, canStop: true) == .none, "fixed contact intent")
        }
        test("inactive release cannot start") {
            var state = began(.start)
            expect(end(&state, at: 100.1, allowed: false, canStop: false) == .none, "invalid release context")
        }
        test("absolute monotonic offset does not change elapsed behavior") {
            var state = began(at: 9_000_000)
            expect(state.progress(at: 9_000_001) == 0.5, "elapsed progress")
            expect(end(&state, at: 9_000_002) == .stop, "elapsed stop")
        }
        print("PASS: \(scenarios) scenarios, \(assertions) assertions")
    }
}
