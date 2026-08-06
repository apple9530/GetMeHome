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
    private(set) var transitStops: [TransitStop] = []
    /// True when the viewport holds more stops than were returned, so the map
    /// can say "zoom in" rather than implying this is all of them.
    private(set) var transitStopsTruncated = false
    private(set) var errorMessage: String?
    private(set) var isSearching = false
    /// Shown inline beneath the field. Kept apart from `errorMessage`,
    /// which drives a modal alert — an alert per keystroke while the
    /// server is unreachable is unusable.
    private(set) var searchError: String?

    /// Both ends of the route, both starting empty.
    ///
    /// The origin used to default to "current location". It read as the app
    /// having already decided where you are starting from, and clearing a
    /// prefilled field is more work than filling an empty one. "Current
    /// location" is the first row of the suggestion list for either end, which
    /// is a tap either way.
    var origin: RoutePoint?
    var destination: RoutePoint?

    var editingField: RouteField = .destination
    var selectedItineraryID: String?
    var selectedCell: CrimeCell?
    var selectedStop: TransitStop?

    /// Text bindings for the two fields.
    ///
    /// Both are always live rather than one shared box swapped between rows.
    /// The shared version deadlocked: the text field only existed while
    /// focused, and focus could not be granted to a field that did not yet
    /// exist, so neither row could ever be edited.
    var originText: String = "" {
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
        origin != nil && destination != nil
    }

    /// The name shown on the destination marker and spoken on arrival.
    var destinationName: String {
        destination?.displayName ?? "your destination"
    }

    /// Whether either end depends on a live location fix — which is when a
    /// missing location permission is worth mentioning.
    var usesCurrentLocation: Bool {
        origin?.isCurrentLocation == true || destination?.isCurrentLocation == true
    }

    func point(for field: RouteField) -> RoutePoint? {
        switch field {
        case .origin: origin
        case .destination: destination
        }
    }

    let places: PlaceStore

    private let client: RoutingClient
    private let location: LocationService
    private let settings: AppSettings

    /// Increments per search so a stale task can tell it has been
    /// superseded and leave the newer one's state alone.
    private var searchGeneration = 0
    private var searchTask: Task<Void, Never>?
    private var overlayTask: Task<Void, Never>?
    private var lastOverlayBounds: MapBounds?

    /// `places` has no default value on purpose: a default argument is
    /// evaluated in a nonisolated context, so constructing a `@MainActor`
    /// type there does not compile. The store is owned by the app and passed
    /// in, which is the right ownership anyway — the view model should not
    /// quietly create a second copy of the user's saved places.
    init(
        client: RoutingClient,
        location: LocationService,
        settings: AppSettings,
        places: PlaceStore
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
        searchError = nil
        searchTask?.cancel()

        let hasCommittedValue = (field == .origin) ? origin != nil : destination != nil
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
        searchError = nil
        // Put back whatever was committed, so an abandoned edit does not leave
        // the field looking empty when a route is still set.
        withoutSearching {
            switch field {
            case .origin:
                originText = origin?.displayName ?? ""
            case .destination:
                destinationText = destination?.displayName ?? ""
            }
        }
    }

    func swapEndpoints() {
        (origin, destination) = (destination, origin)
        syncFieldText()
        Task { await requestRoutes() }
    }

    func clear(_ field: RouteField) {
        switch field {
        case .origin:
            origin = nil
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

        // Filling one end without the other is a natural point to move on
        // rather than to route.
        guard canRoute else {
            editingField = (origin == nil) ? .origin : .destination
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

        guard canRoute else {
            editingField = (origin == nil) ? .origin : .destination
            return
        }
        await requestRoutes()
    }

    /// Bring both text fields back in line with the committed endpoints.
    private func syncFieldText() {
        withoutSearching {
            originText = origin?.displayName ?? ""
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
        searchError = nil
        let query = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard query.count >= 2 else {
            searchResults = []
            isSearching = false
            return
        }

        isSearching = true
        searchGeneration += 1
        let generation = searchGeneration

        searchTask = Task { [weak self] in
            guard let self else { return }

            // Every exit path has to clear the spinner, including the early
            // returns on cancellation — otherwise a superseded keystroke
            // leaves it turning forever. Guarding on the generation means a
            // stale task cannot switch off a spinner that a newer one owns.
            defer {
                if generation == searchGeneration { isSearching = false }
            }

            // Debounce: a request per keystroke is wasteful, and the external
            // geocoder behind the local index rate-limits.
            try? await Task.sleep(for: .milliseconds(320))
            guard !Task.isCancelled else { return }

            do {
                let results = try await client.geocode(
                    query, near: location.location?.coordinate
                )
                guard !Task.isCancelled, generation == searchGeneration else { return }
                searchResults = results
            } catch is CancellationError {
                return
            } catch let error as URLError where error.code == .cancelled {
                return
            } catch {
                guard !Task.isCancelled, generation == searchGeneration else { return }
                searchResults = []
                // Shown inline under the field rather than as an alert: an
                // alert per keystroke while the server is down is unusable.
                searchError = Self.message(for: error)
            }
        }
    }

    func clearSearch() {
        searchTask?.cancel()
        searchResults = []
        isSearching = false
        searchError = nil
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
        guard let origin, let destination else { return }

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
                forceNight: settings.timeOfDay.forceNight,
                crimeWindowDays: settings.crimeWindow.rawValue
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
        guard canRoute, phase == .showingOptions else { return }
        await requestRoutes()
    }

    /// Re-score for a different time of day. Unlike `refreshRoutes` this also
    /// fires while a request is in flight, since the user has just changed the
    /// question being asked.
    func rescoreForTimeOfDay() async {
        guard canRoute else { return }
        await requestRoutes()
    }

    /// Re-score for a different crime lookback, and repaint the grid with it.
    ///
    /// Both together, always: a map showing a year of incidents next to a
    /// route scored on thirty days would be actively misleading.
    func changeCrimeWindow() async {
        invalidateOverlays()
        guard canRoute else { return }
        await requestRoutes()
    }

    func cancelRouting() {
        itineraries = []
        notices = []
        origin = nil
        destination = nil
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
        guard settings.showCameraOverlay
            || settings.showCrimeGrid
            || settings.showTransitStops
        else {
            cameras = []
            crimeCells = []
            transitStops = []
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
                    in: bounds,
                    nightOnly: settings.crimeGridNightOnly,
                    windowDays: settings.crimeWindow.rawValue
                ), !Task.isCancelled {
                    crimeCells = response.cells
                    crimeCellRadius = response.radius
                }
            } else {
                crimeCells = []
                selectedCell = nil
            }

            if settings.showTransitStops {
                if let response = try? await client.transitStops(in: bounds),
                   !Task.isCancelled {
                    transitStops = response.stops
                    transitStopsTruncated = response.truncated
                }
            } else {
                transitStops = []
                transitStopsTruncated = false
                selectedStop = nil
            }
        }
    }

    /// Refetch the overlays for the viewport already on screen.
    ///
    /// Called when a setting changes what should be drawn. Clearing the cached
    /// bounds alone is not enough: nothing refetches until the map moves, so a
    /// toggle would appear to do nothing until the user panned.
    func invalidateOverlays() {
        guard let bounds = lastOverlayBounds else { return }
        lastOverlayBounds = nil
        refreshOverlays(for: bounds)
    }

    func dismissError() {
        errorMessage = nil
    }

    private static func message(for error: Error) -> String {
        (error as? RoutingError)?.errorDescription ?? error.localizedDescription
    }
}
