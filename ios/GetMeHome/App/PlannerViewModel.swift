import CoreLocation
import MapKit
import Observation
import SwiftUI

/// Owns the plan-a-route flow: pick both ends, fetch, select, hand off to
/// navigation.
@Observable
@MainActor
final class PlannerViewModel {
    enum Phase: Equatable {
        case idle
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
    private(set) var crimeCells: [CrimeCell] = []
    private(set) var crimeCellRadius: Double = 0
    private(set) var errorMessage: String?
    private(set) var isSearching = false

    /// Both ends of the route. Origin defaults to wherever the user is, which
    /// is the overwhelmingly common case, but is fully editable.
    var origin: RoutePoint = .currentLocation
    var destination: RoutePoint?

    var editingField: RouteField = .destination
    var selectedItineraryID: String?
    var selectedCell: CrimeCell?

    /// Text bindings for the two fields.
    ///
    /// Both are always live rather than one shared box swapped between rows.
    /// The shared version deadlocked: the text field only existed while
    /// focused, and focus could not be granted to a field that did not yet
    /// exist, so neither row could ever be edited.
    var originText: String = RoutePoint.currentLocation.displayName {
        didSet { handleTextChange(.origin, from: oldValue, to: originText) }
    }

    var destinationText: String = "" {
        didSet { handleTextChange(.destination, from: oldValue, to: destinationText) }
    }

    /// Suppresses the search that a `didSet` would otherwise fire when we set
    /// the text ourselves after a selection.
    private var isApplyingSelection = false

    var selectedItinerary: Itinerary? {
        itineraries.first { $0.id == selectedItineraryID } ?? itineraries.first
    }

    var canRoute: Bool {
        destination != nil
    }

    /// The name shown on the destination marker and spoken on arrival.
    var destinationName: String {
        destination?.displayName ?? "your destination"
    }

    let places: PlaceStore

    private let client: RoutingClient
    private let location: LocationService
    private let settings: AppSettings

    private var searchTask: Task<Void, Never>?
    private var overlayTask: Task<Void, Never>?
    private var lastOverlayBounds: MapBounds?

    init(
        client: RoutingClient,
        location: LocationService,
        settings: AppSettings,
        places: PlaceStore = PlaceStore()
    ) {
        self.client = client
        self.location = location
        self.settings = settings
        self.places = places
    }

    /// Starred places first, then recents — what the picker shows before any
    /// text is typed.
    var suggestions: [SavedPlace] { places.suggestions }

    func toggleStar(_ result: GeocodeResult) {
        places.toggleStar(result)
    }

    // MARK: - Editing the endpoints

    /// Called when a field gains focus.
    ///
    /// Clears an already-committed value so that typing replaces it rather
    /// than appending to it — tapping a field that reads "Union Station" and
    /// typing should start a new search, not edit those characters.
    func beginEditing(_ field: RouteField) {
        editingField = field
        searchResults = []
        searchTask?.cancel()

        let hasCommittedValue = (field == .origin) ? true : destination != nil
        if hasCommittedValue {
            withoutSearching {
                switch field {
                case .origin: originText = ""
                case .destination: destinationText = ""
                }
            }
        }
    }

    /// Called when a field loses focus without a selection being made.
    func endEditing(_ field: RouteField) {
        searchTask?.cancel()
        searchResults = []
        isSearching = false
        // Put back whatever was committed, so an abandoned edit does not leave
        // the field looking empty when a route is still set.
        withoutSearching {
            switch field {
            case .origin:
                originText = origin.displayName
            case .destination:
                destinationText = destination?.displayName ?? ""
            }
        }
    }

    func swapEndpoints() {
        let previousOrigin = origin
        origin = destination ?? .currentLocation
        destination = previousOrigin
        syncFieldText()
        Task { await requestRoutes() }
    }

    func clear(_ field: RouteField) {
        switch field {
        case .origin:
            origin = .currentLocation
        case .destination:
            destination = nil
            itineraries = []
            selectedItineraryID = nil
            phase = .idle
        }
        syncFieldText()
    }

    /// Apply a search result to whichever field is being edited.
    func select(_ result: GeocodeResult) async {
        switch editingField {
        case .origin:
            origin = .place(result)
        case .destination:
            destination = .place(result)
        }
        places.record(result)
        syncFieldText()
        clearSearch()

        // Picking a start with no end yet is a natural point to move on
        // rather than to route.
        if editingField == .origin, destination == nil {
            editingField = .destination
            return
        }
        await requestRoutes()
    }

    func useCurrentLocation(for field: RouteField) async {
        switch field {
        case .origin: origin = .currentLocation
        case .destination: destination = .currentLocation
        }
        syncFieldText()
        clearSearch()

        if field == .origin, destination == nil {
            editingField = .destination
            return
        }
        await requestRoutes()
    }

    /// Bring both text fields back in line with the committed endpoints.
    private func syncFieldText() {
        withoutSearching {
            originText = origin.displayName
            destinationText = destination?.displayName ?? ""
        }
    }

    private func withoutSearching(_ body: () -> Void) {
        isApplyingSelection = true
        body()
        isApplyingSelection = false
    }

    private func handleTextChange(_ field: RouteField, from old: String, to new: String) {
        guard !isApplyingSelection, new != old else { return }
        editingField = field
        scheduleSearch(new)
    }

    /// Name a dropped pin so the endpoint card is not just coordinates.
    func resolvePin(at coordinate: CLLocationCoordinate2D, for field: RouteField) async {
        let fallback = GeocodeResult(
            name: "Dropped pin",
            address: String(format: "%.5f, %.5f", coordinate.latitude, coordinate.longitude),
            lat: coordinate.latitude,
            lon: coordinate.longitude
        )
        let resolved = (try? await client.reverseGeocode(coordinate)) ?? fallback
        switch field {
        case .origin: origin = .place(resolved)
        case .destination: destination = .place(resolved)
        }
        await requestRoutes()
    }

    // MARK: - Search

    private func scheduleSearch(_ text: String) {
        searchTask?.cancel()
        let query = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard query.count >= 2 else {
            searchResults = []
            isSearching = false
            return
        }

        isSearching = true
        searchTask = Task { [weak self] in
            guard let self else { return }
            // Debounce: the geocoder is rate-limited and a request per
            // keystroke gets throttled within a few words.
            try? await Task.sleep(for: .milliseconds(320))
            guard !Task.isCancelled else { return }

            do {
                let results = try await client.geocode(
                    query, near: location.location?.coordinate
                )
                guard !Task.isCancelled else { return }
                searchResults = results
            } catch {
                guard !Task.isCancelled else { return }
                searchResults = []
                errorMessage = Self.message(for: error)
            }
            isSearching = false
        }
    }

    func clearSearch() {
        searchTask?.cancel()
        searchResults = []
        isSearching = false
    }

    /// Empty the field being edited, for the clear button.
    func clearEditingText() {
        searchTask?.cancel()
        searchResults = []
        isSearching = false
        withoutSearching {
            switch editingField {
            case .origin: originText = ""
            case .destination: destinationText = ""
            }
        }
    }

    // MARK: - Routing

    func requestRoutes() async {
        guard let destination else { return }

        phase = .routing
        errorMessage = nil

        do {
            let start = try await resolve(origin)
            let end = try await resolve(destination)

            let response = try await client.route(
                from: start,
                to: end,
                destinationName: destinationName,
                modes: settings.modes,
                avoidCameras: settings.avoidCameras,
                forceNight: settings.timeOfDay.forceNight
            )

            itineraries = response.itineraries
            notices = response.notices
            isNight = response.isNight
            selectedItineraryID = response.itineraries.first?.id
            phase = response.itineraries.isEmpty ? .idle : .showingOptions

            if response.itineraries.isEmpty {
                errorMessage = "No route found to \(destinationName)."
            }
        } catch let error as CLError where error.code == .denied {
            phase = .idle
            errorMessage = "Location access is off. Turn it on in Settings, "
                + "or set a starting point instead of using your location."
        } catch is CLError {
            phase = .idle
            errorMessage = "Couldn't get your location. Try setting a starting "
                + "point manually."
        } catch {
            phase = .idle
            errorMessage = Self.message(for: error)
        }
    }

    /// Turn a `RoutePoint` into a coordinate, taking a live fix if needed.
    private func resolve(_ point: RoutePoint) async throws -> CLLocationCoordinate2D {
        if let fixed = point.fixedCoordinate { return fixed }
        return try await location.currentLocation().coordinate
    }

    func refreshRoutes() async {
        guard destination != nil, phase == .showingOptions else { return }
        await requestRoutes()
    }

    /// Re-score for a different time of day. Unlike `refreshRoutes` this also
    /// fires while a request is in flight, since the user has just changed the
    /// question being asked.
    func rescoreForTimeOfDay() async {
        guard destination != nil else { return }
        await requestRoutes()
    }

    func cancelRouting() {
        itineraries = []
        notices = []
        destination = nil
        origin = .currentLocation
        selectedItineraryID = nil
        editingField = .destination
        phase = .idle
        syncFieldText()
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
        guard settings.showCameraOverlay || settings.showCrimeGrid else {
            cameras = []
            crimeCells = []
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

            if settings.showCrimeGrid {
                if let response = try? await client.crimeGrid(
                    in: bounds, nightOnly: settings.crimeGridNightOnly
                ), !Task.isCancelled {
                    crimeCells = response.cells
                    crimeCellRadius = response.radius
                }
            } else {
                crimeCells = []
                selectedCell = nil
            }
        }
    }

    /// Force the next `refreshOverlays` to refetch even if the viewport has
    /// not moved — used when a toggle changes what should be shown.
    func invalidateOverlays() {
        lastOverlayBounds = nil
    }

    func dismissError() {
        errorMessage = nil
    }

    private static func message(for error: Error) -> String {
        (error as? RoutingError)?.errorDescription ?? error.localizedDescription
    }
}
