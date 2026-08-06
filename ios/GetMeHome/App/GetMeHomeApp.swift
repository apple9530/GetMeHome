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

    init() {
        let settings = AppSettings()
        let location = LocationService()
        let client = RoutingClient(baseURL: settings.serverURL)
        let places = PlaceStore()

        _settings = State(initialValue: settings)
        _location = State(initialValue: location)
        _client = State(initialValue: client)
        _places = State(initialValue: places)
        _planner = State(
            initialValue: PlannerViewModel(
                client: client, location: location, settings: settings, places: places
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
                .task {
                    location.requestAuthorization()
                    location.startUpdating()
                }
                .onChange(of: settings.serverURLString) { _, newValue in
                    guard let url = URL(string: newValue) else { return }
                    Task { await client.updateBaseURL(url) }
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

    @State private var cameraPosition: MapCameraPosition = .region(
        MKCoordinateRegion(
            center: CLLocationCoordinate2D(latitude: 38.9047, longitude: -77.0164),
            span: MKCoordinateSpan(latitudeDelta: 0.09, longitudeDelta: 0.09)
        )
    )
    @State private var navigationModel: NavigationViewModel?

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
        // Settings that change what is drawn or what was asked for are
        // reacted to here, once, rather than in each control. The same
        // preference is deliberately offered in more than one place — the
        // crime window belongs both beside the routes and beside the map
        // layer it governs — and a handler per control means two refetches
        // for one change.
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
