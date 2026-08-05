import CoreLocation
import MapKit
import Observation
import SwiftUI

/// Owns the plan-a-route flow: search, fetch, select, hand off to navigation.
@Observable
@MainActor
final class PlannerViewModel {
    enum Phase: Equatable {
        case idle
        case searching
        case routing
        case showingOptions
        case navigating
    }

    private(set) var phase: Phase = .idle
    private(set) var itineraries: [Itinerary] = []
    private(set) var notices: [String] = []
    private(set) var isNight = false
    private(set) var searchResults: [GeocodeResult] = []
    private(set) var cameras: [ALPRCamera] = []
    private(set) var safetySegments: [SafetySegment] = []
    private(set) var errorMessage: String?

    var selectedItineraryID: String?
    var destination: GeocodeResult?
    var searchText = "" {
        didSet { scheduleSearch() }
    }

    var selectedItinerary: Itinerary? {
        itineraries.first { $0.id == selectedItineraryID } ?? itineraries.first
    }

    private let client: RoutingClient
    private let location: LocationService
    private let settings: AppSettings

    private var searchTask: Task<Void, Never>?
    private var overlayTask: Task<Void, Never>?
    private var lastOverlayBounds: MapBounds?

    init(client: RoutingClient, location: LocationService, settings: AppSettings) {
        self.client = client
        self.location = location
        self.settings = settings
    }

    // MARK: - Search

    private func scheduleSearch() {
        searchTask?.cancel()
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard query.count >= 2 else {
            searchResults = []
            return
        }

        searchTask = Task { [weak self] in
            guard let self else { return }
            // Debounce: the geocoder is rate-limited and a keystroke-per-request
            // pattern gets throttled within a few words.
            try? await Task.sleep(for: .milliseconds(320))
            guard !Task.isCancelled else { return }

            do {
                let results = try await client.geocode(query)
                guard !Task.isCancelled else { return }
                searchResults = results
            } catch {
                guard !Task.isCancelled else { return }
                searchResults = []
                errorMessage = Self.message(for: error)
            }
        }
    }

    func clearSearch() {
        searchTask?.cancel()
        searchText = ""
        searchResults = []
    }

    /// Name a dropped pin so the destination card is not just coordinates.
    func resolvePin(at coordinate: CLLocationCoordinate2D) async {
        let fallback = GeocodeResult(
            name: "Dropped pin",
            address: String(format: "%.5f, %.5f", coordinate.latitude, coordinate.longitude),
            lat: coordinate.latitude,
            lon: coordinate.longitude
        )
        destination = (try? await client.reverseGeocode(coordinate)) ?? fallback
        await requestRoutes()
    }

    // MARK: - Routing

    func select(_ result: GeocodeResult) async {
        destination = result
        clearSearch()
        await requestRoutes()
    }

    func requestRoutes() async {
        guard let destination else { return }

        phase = .routing
        errorMessage = nil

        do {
            let origin = try await location.currentLocation()
            let response = try await client.route(
                from: origin.coordinate,
                to: destination.coordinate,
                destinationName: destination.name,
                modes: settings.modes,
                avoidCameras: settings.avoidCameras
            )

            itineraries = response.itineraries
            notices = response.notices
            isNight = response.isNight
            selectedItineraryID = response.itineraries.first?.id
            phase = response.itineraries.isEmpty ? .idle : .showingOptions

            if response.itineraries.isEmpty {
                errorMessage = "No route found to \(destination.name)."
            }
        } catch let error as CLError where error.code == .denied {
            phase = .idle
            errorMessage = "Location access is off. Turn it on in Settings to plan a route."
        } catch {
            phase = .idle
            errorMessage = Self.message(for: error)
        }
    }

    /// Re-run the current request after a preference change.
    func refreshRoutes() async {
        guard destination != nil, phase == .showingOptions else { return }
        await requestRoutes()
    }

    func cancelRouting() {
        itineraries = []
        notices = []
        destination = nil
        selectedItineraryID = nil
        phase = .idle
    }

    func beginNavigation() {
        guard selectedItinerary != nil else { return }
        phase = .navigating
    }

    func endNavigation() {
        phase = itineraries.isEmpty ? .idle : .showingOptions
    }

    // MARK: - Overlays

    func refreshOverlays(for bounds: MapBounds) {
        guard settings.showCameraOverlay || settings.showSafetyOverlay else {
            cameras = []
            safetySegments = []
            return
        }
        // Panning fires continuously; only refetch on a real viewport change.
        if let last = lastOverlayBounds, !bounds.differs(from: last) { return }
        lastOverlayBounds = bounds

        overlayTask?.cancel()
        overlayTask = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }

            if settings.showCameraOverlay {
                if let response = try? await client.cameras(in: bounds), !Task.isCancelled {
                    cameras = response.cameras
                }
            } else {
                cameras = []
            }

            if settings.showSafetyOverlay {
                if let response = try? await client.safetyOverlay(in: bounds, night: nil),
                   !Task.isCancelled {
                    safetySegments = response.segments
                    isNight = response.isNight
                }
            } else {
                safetySegments = []
            }
        }
    }

    func dismissError() {
        errorMessage = nil
    }

    private static func message(for error: Error) -> String {
        (error as? RoutingError)?.errorDescription ?? error.localizedDescription
    }
}
