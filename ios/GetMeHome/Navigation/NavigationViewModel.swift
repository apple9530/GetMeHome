import CoreLocation
import Observation
import SwiftUI

/// Drives an active turn-by-turn session.
@Observable
@MainActor
final class NavigationViewModel {
    // MARK: - Tunables

    /// Deviation beyond which a fix counts as off-route. Generous, because GPS
    /// in downtown DC bounces off buildings by 20-30m routinely and rerouting
    /// on a single bad fix is far more annoying than a moment's lag.
    private static let offRouteThreshold: CLLocationDistance = 40

    /// Consecutive off-route fixes before rerouting. Combined with the
    /// threshold this means a genuine wrong turn triggers in a few seconds
    /// while a multipath glitch never does.
    private static let offRouteFixesBeforeReroute = 4

    /// How close counts as arrived.
    private static let arrivalRadius: CLLocationDistance = 25

    /// Ignore fixes worse than this; they corrupt progress more than they help.
    private static let maxAcceptableAccuracy: CLLocationAccuracy = 65

    private static let minimumRerouteInterval: TimeInterval = 12

    // MARK: - State

    private(set) var itinerary: Itinerary
    private(set) var tracker: RouteTracker
    private(set) var progress: RouteTracker.Progress?
    private(set) var isRerouting = false
    private(set) var hasArrived = false
    private(set) var offRouteCount = 0
    private(set) var lastError: String?

    var currentStep: NavStep? {
        guard let progress, tracker.steps.indices.contains(progress.stepIndex) else {
            return tracker.steps.first
        }
        return tracker.steps[progress.stepIndex]
    }

    var nextStep: NavStep? {
        guard let progress else { return nil }
        let next = progress.stepIndex + 1
        return tracker.steps.indices.contains(next) ? tracker.steps[next] : nil
    }

    var distanceToManeuver: CLLocationDistance { progress?.distanceToManeuver ?? 0 }
    var remainingDistance: CLLocationDistance {
        progress?.remainingDistance ?? itinerary.walkDistance
    }
    var remainingDuration: TimeInterval { progress?.remainingDuration ?? itinerary.duration }
    var estimatedArrival: Date { Date().addingTimeInterval(remainingDuration) }

    /// The upcoming stretch's safety warning, if any.
    var activeSafetyNote: String? {
        guard let note = currentStep?.safetyNote, !note.isEmpty else { return nil }
        return note
    }

    // MARK: - Collaborators

    private let speech: SpeechService
    private let client: RoutingClient
    private let destination: CLLocationCoordinate2D
    private let destinationName: String
    private let modes: [String]
    private let avoidCameras: Bool

    /// Nil until the first fix, and reset on reroute, so those searches scan
    /// the whole route instead of a window around a position on the old one.
    private var lastDistanceAlong: Double?
    private var spokenTriggers: Set<String> = []
    private var lastRerouteAt: Date = .distantPast
    private var rerouteTask: Task<Void, Never>?

    init(
        itinerary: Itinerary,
        destination: CLLocationCoordinate2D,
        destinationName: String,
        modes: [String],
        avoidCameras: Bool,
        speech: SpeechService,
        client: RoutingClient
    ) {
        self.itinerary = itinerary
        self.destination = destination
        self.destinationName = destinationName
        self.modes = modes
        self.avoidCameras = avoidCameras
        self.speech = speech
        self.client = client
        self.tracker = RouteTracker(
            coordinates: itinerary.allCoordinates,
            steps: itinerary.walkingSteps,
            duration: itinerary.duration
        )
    }

    // MARK: - Lifecycle

    func start() {
        if let first = tracker.steps.first {
            var opening = first.voice
            if !first.safetyNote.isEmpty {
                opening += ". \(first.safetyNote)"
            }
            speech.speak(opening)
            spokenTriggers.insert(triggerKey(stepIndex: 0, trigger: -1))
        }
    }

    func stop() {
        rerouteTask?.cancel()
        rerouteTask = nil
        speech.stop()
    }

    // MARK: - Location updates

    func update(with location: CLLocation) {
        guard !hasArrived else { return }

        // A fix this poor will bounce progress around and can fake a wrong
        // turn; better to coast on the previous position.
        guard location.horizontalAccuracy > 0,
              location.horizontalAccuracy <= Self.maxAcceptableAccuracy else { return }

        let fix = tracker.progress(
            for: location.coordinate, previousDistance: lastDistanceAlong
        )
        lastDistanceAlong = fix.distanceAlong
        progress = fix

        checkArrival(at: location)
        guard !hasArrived else { return }

        speakIfNeeded(for: fix)
        checkOffRoute(fix, from: location)
    }

    private func checkArrival(at location: CLLocation) {
        let target = CLLocation(latitude: destination.latitude, longitude: destination.longitude)
        let straightLine = location.distance(from: target)

        // Both conditions matter. Distance alone fires early when the route
        // passes near the destination on its way round a block; route-progress
        // alone fires late if the polyline overshoots the door.
        guard straightLine <= Self.arrivalRadius,
              (progress?.remainingDistance ?? .infinity) <= Self.arrivalRadius * 2
        else { return }

        hasArrived = true
        rerouteTask?.cancel()
        speech.speak("You have arrived at \(destinationName)", priority: .high)
    }

    private func speakIfNeeded(for fix: RouteTracker.Progress) {
        guard let step = currentStep else { return }
        let upcoming = nextStep ?? step

        for trigger in step.voiceTriggers.sorted(by: >) {
            guard fix.distanceToManeuver <= trigger else { continue }
            let key = triggerKey(stepIndex: fix.stepIndex, trigger: trigger)
            guard !spokenTriggers.contains(key) else { continue }
            spokenTriggers.insert(key)

            let phrase: String
            if trigger <= 20 {
                phrase = upcoming.voice
            } else {
                phrase = "In \(Self.spokenDistance(trigger)), \(lowercasedFirst(upcoming.voice))"
            }
            speech.speak(phrase, priority: trigger <= 20 ? .high : .normal)

            // Warn about a dark or high-incident stretch as it is entered,
            // not at the end of it.
            if trigger <= 60, !upcoming.safetyNote.isEmpty {
                let noteKey = "note-\(fix.stepIndex)"
                if !spokenTriggers.contains(noteKey) {
                    spokenTriggers.insert(noteKey)
                    speech.speak(upcoming.safetyNote)
                }
            }
            break
        }
    }

    private func checkOffRoute(_ fix: RouteTracker.Progress, from location: CLLocation) {
        // Scale the threshold by the fix's own accuracy: a ±50m fix should not
        // be trusted to say someone is 45m off route.
        let tolerance = Self.offRouteThreshold + max(0, location.horizontalAccuracy - 15)

        guard fix.deviation > tolerance else {
            offRouteCount = 0
            return
        }

        offRouteCount += 1
        guard offRouteCount >= Self.offRouteFixesBeforeReroute else { return }
        guard Date().timeIntervalSince(lastRerouteAt) > Self.minimumRerouteInterval else {
            return
        }
        reroute(from: location.coordinate)
    }

    // MARK: - Rerouting

    private func reroute(from origin: CLLocationCoordinate2D) {
        guard rerouteTask == nil else { return }
        lastRerouteAt = Date()
        isRerouting = true
        speech.speak("Rerouting", priority: .high)

        // The task body inherits this class's main-actor isolation, so state
        // mutations here need no further hopping; only the network call
        // suspends onto the client actor.
        rerouteTask = Task { [weak self] in
            guard let self else { return }
            defer {
                isRerouting = false
                rerouteTask = nil
            }

            do {
                let response = try await client.route(
                    from: origin,
                    to: destination,
                    destinationName: destinationName,
                    modes: modes,
                    avoidCameras: avoidCameras
                )
                guard !Task.isCancelled else { return }

                // Prefer the same kind of route the user originally chose —
                // someone who picked the safest option did not ask to be put
                // on the fastest one just because they took a wrong turn.
                let replacement = response.itineraries.first { $0.label == itinerary.label }
                    ?? response.itineraries.first

                if let replacement {
                    adopt(replacement)
                }
            } catch {
                lastError = (error as? RoutingError)?.errorDescription
                    ?? error.localizedDescription
                // Keep guiding on the old route. A stale route is more use
                // than a blank screen when someone is out at night.
                offRouteCount = 0
            }
        }
    }

    private func adopt(_ replacement: Itinerary) {
        itinerary = replacement
        tracker = RouteTracker(
            coordinates: replacement.allCoordinates,
            steps: replacement.walkingSteps,
            duration: replacement.duration
        )
        lastDistanceAlong = nil
        offRouteCount = 0
        spokenTriggers.removeAll()
        progress = nil
        lastError = nil

        if let first = tracker.steps.first {
            speech.speak(first.voice, priority: .high)
            spokenTriggers.insert(triggerKey(stepIndex: 0, trigger: -1))
        }
    }

    // MARK: - Helpers

    private func triggerKey(stepIndex: Int, trigger: Double) -> String {
        "\(stepIndex)-\(Int(trigger))"
    }

    private func lowercasedFirst(_ text: String) -> String {
        guard let first = text.first else { return text }
        return first.lowercased() + text.dropFirst()
    }

    private static func spokenDistance(_ metres: Double) -> String {
        if metres >= 1000 {
            return String(format: "%.1f kilometres", metres / 1000)
        }
        return "\(Int(metres.rounded(.toNearestOrEven) / 10) * 10) metres"
    }
}
