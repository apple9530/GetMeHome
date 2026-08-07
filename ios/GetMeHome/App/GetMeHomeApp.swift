import MapKit
import SwiftUI

@main
struct GetMeHomeApp: App {
    @State private var settings = AppSettings()
    @State private var location = LocationService()
    @State private var speech = SpeechService()
    @State private var planner: PlannerViewModel
    @State private var client: RoutingClient
    @State private var places: PlaceStore
    @State private var connectivity: ConnectivityMonitor

    init() {
        let settings = AppSettings()
        let location = LocationService()
        let client = RoutingClient(baseURL: settings.serverURL)
        let places = PlaceStore(city: settings.citySlug, bounds: settings.cityBBox)
        let connectivity = ConnectivityMonitor(client: client)

        _settings = State(initialValue: settings)
        _location = State(initialValue: location)
        _client = State(initialValue: client)
        _places = State(initialValue: places)
        _connectivity = State(initialValue: connectivity)
        _planner = State(
            initialValue: PlannerViewModel(
                client: client,
                location: location,
                settings: settings,
                places: places,
                connectivity: connectivity
            )
        )
    }

    var body: some Scene {
        WindowGroup {
            RootView(client: client)
                .environment(settings)
                .environment(location)
                .environment(speech)
                .environment(planner)
                .environment(places)
                .environment(connectivity)
                .environment(\.routingClient, client)
                .task {
                    location.requestAuthorization()
                    location.startUpdating()
                    // Reclaim whatever the downloaded-crime-data feature left
                    // on disk before it was removed.
                    StaleData.purge()
                    // One probe at launch, so the first thing the app does is
                    // not a route request that fails.
                    connectivity.start()
                }
                .onChange(of: settings.citySlug) { _, slug in
                    // Recents and starred places are per city: a Washington
                    // address is not a suggestion worth offering in New York.
                    // The bounds go with the slug so the store can also drop
                    // anything a previous version filed under the wrong city.
                    places.switchTo(city: slug, bounds: settings.cityBBox)
                }
                .onChange(of: settings.cityBBox) { _, bounds in
                    // Bounds can arrive without the slug changing — the
                    // backfill on first launch after upgrading does exactly
                    // that — and the place filter is inert until they do.
                    places.switchTo(city: settings.citySlug, bounds: bounds)
                }
                .onChange(of: settings.serverURLString) { _, newValue in
                    guard let url = URL(string: newValue) else { return }
                    Task {
                        await client.updateBaseURL(url)
                        // A new address is a new question about reachability.
                        await connectivity.probe()
                    }
                }
        }
    }
}

struct RootView: View {
    let client: RoutingClient

    @Environment(PlannerViewModel.self) private var planner
    @Environment(LocationService.self) private var location
    @Environment(SpeechService.self) private var speech
    @Environment(AppSettings.self) private var settings

    /// Framed on the user's location once there is a fix, and on nothing in
    /// particular before then.
    ///
    /// It used to be hardcoded to DC's centre, which is wrong the moment there
    /// is a second city — and briefly showing the wrong city is a worse first
    /// impression than showing a wide view for a second. Picking a city moves
    /// it explicitly.
    @State private var cameraPosition: MapCameraPosition = .userLocation(
        fallback: .region(
            MKCoordinateRegion(
                center: CLLocationCoordinate2D(latitude: 39.5, longitude: -75.5),
                span: MKCoordinateSpan(latitudeDelta: 6, longitudeDelta: 6)
            )
        )
    )
    @State private var navigationModel: NavigationViewModel?
    @State private var showCityPicker = false

    var body: some View {
        @Bindable var planner = planner

        ZStack(alignment: .bottom) {
            RouteMapView(cameraPosition: $cameraPosition)
                .ignoresSafeArea(edges: .top)

            VStack(spacing: 0) {
                Spacer()
                bottomPanel
            }

            if planner.phase == .routing {
                ProgressView("Finding routes…")
                    .padding(20)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
            }
        }
        .onChange(of: planner.itineraries) { _, itineraries in
            guard let first = itineraries.first else { return }
            withAnimation { fit(to: first) }
        }
        .task {
            // Learn the chosen city's bounds if they were never cached. Only
            // happens once, on the first launch after upgrading from a build
            // that stored the slug alone; until it completes the saved-places
            // filter has nothing to compare against and lets everything past.
            guard settings.hasChosenCity, settings.cityBBox.count != 4 else { return }
            guard let response = try? await client.cities() else { return }
            for city in response.cities {
                settings.backfillBounds(from: city)
            }
        }
        // Settings that change what is drawn or what was asked for are
        // reacted to here rather than in each control. A preference can
        // surface in more than one place, and a handler per control would
        // mean two refetches for one change; keeping them here also means a
        // control inside a sheet still triggers a refresh after the sheet has
        // gone.
        .onChange(of: settings.crimeWindow) { _, _ in
            Task { await planner.changeCrimeWindow() }
        }
        .onChange(of: settings.timeOfDay) { _, _ in
            Task { await planner.rescoreForTimeOfDay() }
        }
        .onChange(of: settings.showCrimeGrid) { _, _ in planner.invalidateOverlays() }
        .onChange(of: settings.crimeGridNightOnly) { _, _ in planner.invalidateOverlays() }
        .onChange(of: settings.showCameraOverlay) { _, _ in planner.invalidateOverlays() }
        .onChange(of: settings.showTransitStops) { _, _ in planner.invalidateOverlays() }
        .onChange(of: settings.avoidCameras) { _, _ in
            Task { await planner.refreshRoutes() }
        }
        .onChange(of: settings.includeTransit) { _, _ in
            Task { await planner.refreshRoutes() }
        }
        // Blocks everything until a city is chosen. There is nothing
        // meaningful to show before then: the map, the search index and the
        // safety data are all city-specific.
        .fullScreenCover(isPresented: .constant(!settings.hasChosenCity)) {
            CityPickerView(current: nil) { city in
                adopt(city)
            }
        }
        .sheet(isPresented: $showCityPicker) {
            CityPickerView(
                current: settings.citySlug,
                onSelect: { city in
                    showCityPicker = false
                    adopt(city)
                },
                onCancel: { showCityPicker = false }
            )
        }
        .onReceive(NotificationCenter.default.publisher(for: .changeCityRequested)) { _ in
            showCityPicker = true
        }
        .fullScreenCover(isPresented: isNavigating) {
            if let navigationModel {
                NavigationScreen(model: navigationModel) {
                    endNavigation()
                }
            }
        }
        .alert(
            "Something went wrong",
            isPresented: Binding(
                get: { planner.errorMessage != nil },
                set: { if !$0 { planner.dismissError() } }
            )
        ) {
            Button("OK", role: .cancel) { planner.dismissError() }
        } message: {
            Text(planner.errorMessage ?? "")
        }
    }

    private var isNavigating: Binding<Bool> {
        Binding(
            get: { planner.phase == .navigating && navigationModel != nil },
            set: { if !$0 { endNavigation() } }
        )
    }

    @ViewBuilder
    private var bottomPanel: some View {
        switch planner.phase {
        case .idle, .routing:
            SearchView()
                .clipShape(RoundedRectangle(cornerRadius: 22))
                .padding(.horizontal, 8)
                .padding(.bottom, 8)
                .transition(.move(edge: .bottom))
        case .showingOptions:
            RouteOptionsView(
                onStart: startNavigation,
                onCancel: { withAnimation { planner.cancelRouting() } }
            )
            .clipShape(RoundedRectangle(cornerRadius: 22))
            .padding(.horizontal, 8)
            .padding(.bottom, 8)
            .frame(maxHeight: 460)
            .transition(.move(edge: .bottom))
        case .navigating:
            EmptyView()
        }
    }

    /// Switch cities.
    ///
    /// Everything held from the previous city is dropped rather than
    /// reinterpreted — a route, a set of overlays and a search history all
    /// refer to places that no longer exist on this map, and leaving them on
    /// screen would be worse than a moment's blank.
    private func adopt(_ city: CityInfo) {
        let changed = settings.citySlug != city.slug
        settings.select(city)
        if changed {
            planner.cancelRouting()
            planner.clearOverlays()
            withAnimation { cameraPosition = .region(city.mapRegion) }
        }
    }

    private func startNavigation() {
        guard let itinerary = planner.selectedItinerary,
              let destination = planner.destination else { return }

        // The destination is normally a fixed place, but it can be "current
        // location" if the user swapped the endpoints. Fall back to the end of
        // the route geometry, which is where the router actually terminated.
        let target = destination.fixedCoordinate
            ?? itinerary.allCoordinates.last
            ?? location.location?.coordinate
        guard let target else { return }

        navigationModel = NavigationViewModel(
            itinerary: itinerary,
            destination: target,
            destinationName: planner.destinationName,
            modes: settings.modes,
            avoidCameras: settings.avoidCameras,
            city: settings.citySlug,
            speech: speech,
            client: client
        )
        planner.beginNavigation()
    }

    private func endNavigation() {
        navigationModel?.stop()
        navigationModel = nil
        planner.endNavigation()
    }

    /// Frame the map around a route with room for the card stack below it.
    private func fit(to itinerary: Itinerary) {
        let coordinates = itinerary.allCoordinates
        guard !coordinates.isEmpty else { return }

        let lats = coordinates.map(\.latitude)
        let lons = coordinates.map(\.longitude)
        guard let minLat = lats.min(), let maxLat = lats.max(),
              let minLon = lons.min(), let maxLon = lons.max() else { return }

        // Pad generously at the bottom: the options sheet covers roughly the
        // lower half of the screen, and a route centred in the full viewport
        // ends up hidden behind it.
        let latSpan = max(0.004, (maxLat - minLat) * 2.4)
        let lonSpan = max(0.004, (maxLon - minLon) * 1.5)

        cameraPosition = .region(
            MKCoordinateRegion(
                center: CLLocationCoordinate2D(
                    latitude: (minLat + maxLat) / 2 - latSpan * 0.18,
                    longitude: (minLon + maxLon) / 2
                ),
                span: MKCoordinateSpan(latitudeDelta: latSpan, longitudeDelta: lonSpan)
            )
        )
    }
}

extension Notification.Name {
    /// Raised from Settings, which is presented as a sheet from the search
    /// panel and so cannot itself present the city picker over the top.
    static let changeCityRequested = Notification.Name("GetMeHome.changeCityRequested")
}
