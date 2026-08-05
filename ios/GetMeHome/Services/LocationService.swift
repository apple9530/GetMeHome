import CoreLocation
import Observation

/// Wraps CoreLocation for the app.
///
/// Two accuracy modes rather than one: idle browsing does not need the
/// battery cost of best-accuracy GPS, but turn-by-turn absolutely does, and
/// leaving navigation-grade accuracy on all the time is the fastest way to
/// drain a phone that someone is relying on to get home.
@Observable
@MainActor
final class LocationService: NSObject {
    private(set) var location: CLLocation?
    private(set) var heading: CLLocationDirection?
    private(set) var authorizationStatus: CLAuthorizationStatus
    private(set) var isNavigating = false

    /// Set when the user has denied access, so the UI can explain rather than
    /// silently showing nothing.
    var accessDenied: Bool {
        authorizationStatus == .denied || authorizationStatus == .restricted
    }

    private let manager = CLLocationManager()
    private var continuations: [UUID: CheckedContinuation<CLLocation, Error>] = [:]

    override init() {
        authorizationStatus = manager.authorizationStatus
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
        manager.distanceFilter = 10
    }

    func requestAuthorization() {
        guard authorizationStatus == .notDetermined else { return }
        manager.requestWhenInUseAuthorization()
    }

    func startUpdating() {
        guard !accessDenied else { return }
        manager.startUpdatingLocation()
        manager.startUpdatingHeading()
    }

    func stopUpdating() {
        guard !isNavigating else { return }
        manager.stopUpdatingLocation()
        manager.stopUpdatingHeading()
    }

    /// Switch to navigation-grade tracking.
    ///
    /// `allowsBackgroundLocationUpdates` is what keeps guidance running with
    /// the screen off, which is the normal way to walk somewhere at night —
    /// nobody holds a lit phone up for twenty minutes.
    func beginNavigation() {
        guard !accessDenied else { return }
        isNavigating = true
        manager.desiredAccuracy = kCLLocationAccuracyBestForNavigation
        manager.distanceFilter = kCLDistanceFilterNone
        manager.activityType = .fitness
        if manager.authorizationStatus == .authorizedAlways
            || manager.authorizationStatus == .authorizedWhenInUse {
            manager.allowsBackgroundLocationUpdates = true
            manager.pausesLocationUpdatesAutomatically = false
        }
        manager.startUpdatingLocation()
        manager.startUpdatingHeading()
    }

    func endNavigation() {
        isNavigating = false
        manager.allowsBackgroundLocationUpdates = false
        manager.pausesLocationUpdatesAutomatically = true
        manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
        manager.distanceFilter = 10
    }

    /// One fix, awaited. Used when the user taps "route from here" before any
    /// update has arrived.
    func currentLocation(timeout: TimeInterval = 8) async throws -> CLLocation {
        if let location, location.timestamp.timeIntervalSinceNow > -30 {
            return location
        }
        startUpdating()

        let id = UUID()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                continuations[id] = continuation
                Task {
                    try? await Task.sleep(for: .seconds(timeout))
                    if let pending = continuations.removeValue(forKey: id) {
                        pending.resume(throwing: CLError(.locationUnknown))
                    }
                }
            }
        } onCancel: {
            Task { @MainActor in self.continuations.removeValue(forKey: id) }
        }
    }
}

extension LocationService: CLLocationManagerDelegate {
    nonisolated func locationManager(
        _ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]
    ) {
        guard let newest = locations.last else { return }
        Task { @MainActor in
            self.location = newest
            for (_, continuation) in self.continuations {
                continuation.resume(returning: newest)
            }
            self.continuations.removeAll()
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager, didUpdateHeading newHeading: CLHeading
    ) {
        guard newHeading.headingAccuracy >= 0 else { return }
        Task { @MainActor in
            self.heading = newHeading.trueHeading >= 0
                ? newHeading.trueHeading
                : newHeading.magneticHeading
        }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor in
            self.authorizationStatus = status
            if status == .authorizedAlways || status == .authorizedWhenInUse {
                self.startUpdating()
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in
            // A transient `locationUnknown` resolves itself; anything else
            // should release awaiting callers rather than hang them.
            guard (error as? CLError)?.code != .locationUnknown else { return }
            for (_, continuation) in self.continuations {
                continuation.resume(throwing: error)
            }
            self.continuations.removeAll()
        }
    }
}
