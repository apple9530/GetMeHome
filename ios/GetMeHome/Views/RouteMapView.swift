import CoreLocation
import MapKit
import SwiftUI

/// The map: route lines, Flock camera cones, and the crime grid.
struct RouteMapView: View {
    @Environment(PlannerViewModel.self) private var planner
    @Environment(AppSettings.self) private var settings
    @Environment(ConnectivityMonitor.self) private var connectivity

    @Binding var cameraPosition: MapCameraPosition

    @State private var selectedCamera: ALPRCamera?
    @State private var showLayers = false
    /// The map controls are placed by hand rather than by `.mapControls`, so
    /// they need the map's scope to stay wired to it.
    @Namespace private var mapScope

    var body: some View {
        MapReader { proxy in
            Map(position: $cameraPosition, interactionModes: .all, scope: mapScope) {
                UserAnnotation()

                if settings.showCrimeGrid {
                    crimeGrid
                }

                ForEach(unselectedItineraries) { itinerary in
                    MapPolyline(coordinates: itinerary.allCoordinates)
                        .stroke(
                            Theme.alternateRouteLine,
                            style: StrokeStyle(lineWidth: 4, lineCap: .round, lineJoin: .round)
                        )
                }

                if let selected = planner.selectedItinerary {
                    routeLines(for: selected)
                }

                if settings.showCameraOverlay {
                    cameraOverlay
                }

                if settings.showTransitStops {
                    transitStops
                }

                endpointMarkers
            }
            .mapStyle(.standard(elevation: .flat, pointsOfInterest: .excludingAll))
            // Suppress the automatic placement, which tucks the controls under
            // the status bar when the map ignores the top safe area.
            .mapControlVisibility(.hidden)
            .onMapCameraChange(frequency: .onEnd) { context in
                let region = context.region
                planner.refreshOverlays(
                    for: MapBounds(
                        minLat: region.center.latitude - region.span.latitudeDelta / 2,
                        minLon: region.center.longitude - region.span.longitudeDelta / 2,
                        maxLat: region.center.latitude + region.span.latitudeDelta / 2,
                        maxLon: region.center.longitude + region.span.longitudeDelta / 2
                    )
                )
            }
            .onTapGesture(coordinateSpace: .local) { screenPoint in
                guard let coordinate = proxy.convert(screenPoint, from: .local) else { return }
                handleTap(at: coordinate)
            }
            .overlay(alignment: .topTrailing) { controls }
            .overlay(alignment: .top) { topBanners }
            .sheet(isPresented: $showLayers) {
                MapLayersView()
            }
            .sheet(item: $selectedCamera) { camera in
                CameraDetailSheet(camera: camera)
                    .presentationDetents([.height(300)])
            }
            .sheet(item: selectedCellBinding) { cell in
                CrimeCellSheet(
                    cell: cell,
                    radius: planner.crimeCellRadius,
                    isOffline: planner.crimeCellsAreOffline
                )
                    .presentationDetents([.height(420), .medium])
            }
            .sheet(item: selectedStopBinding) { stop in
                TransitStopView(stop: stop)
                    .presentationDetents([.medium, .large])
            }
        }
        .mapScope(mapScope)
    }

    /// Everything that sits along the top edge, stacked.
    private var topBanners: some View {
        VStack(spacing: 6) {
            if connectivity.isOffline {
                offlineBanner
            }
            crimeGridNotice
            truncationNotice
        }
        .padding(.top, 14)
        .safeAreaPadding(.top)
    }

    /// Shown for as long as the server is unreachable.
    ///
    /// Persistent rather than a toast: this is a state, not an event, and
    /// everything the app can do is different while it lasts. It says what is
    /// still working — the downloaded crime grid, if there is one — because
    /// "no connection" on its own reads as "nothing works".
    private var offlineBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "wifi.slash")
                .font(.caption)
                .foregroundStyle(.orange)

            VStack(alignment: .leading, spacing: 1) {
                Text("No connection to the server")
                    .font(.caption.weight(.medium))
                Text(offlineDetail)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }

            Spacer(minLength: 4)

            if connectivity.isRetrying {
                ProgressView().controlSize(.small)
            } else {
                Button("Retry") {
                    Task { await connectivity.probe() }
                }
                .font(.caption.weight(.semibold))
                .buttonStyle(.borderless)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(Color.orange.opacity(0.35), lineWidth: 1)
        )
        .padding(.horizontal, 12)
        .transition(.move(edge: .top).combined(with: .opacity))
        .animation(.snappy, value: connectivity.isOffline)
    }

    private var offlineDetail: String {
        if planner.crimeCellsAreOffline {
            return "Showing downloaded crime data. Retrying every 15 seconds."
        }
        if connectivity.isRetrying {
            return "Reconnecting…"
        }
        return "Retrying every 15 seconds. Routing needs a connection."
    }

    /// Why the crime overlay is drawing nothing.
    ///
    /// The overlay switched on and nothing appearing is the single most
    /// confusing state this map has, because it is exactly what a genuinely
    /// safe neighbourhood looks like. The commonest real cause is a lookback
    /// window shorter than the city's publishing lag — New York's police data
    /// arrives in quarterly batches, so a 30-day window there can match
    /// nothing at all — and that has a one-tap fix the user cannot guess at.
    ///
    /// Suppressed while offline, where the connection banner is already saying
    /// the more important thing.
    @ViewBuilder
    private var crimeGridNotice: some View {
        if settings.showCrimeGrid,
           !connectivity.isOffline,
           let notice = planner.crimeGridNotice {
            HStack(spacing: 8) {
                Image(systemName: "square.grid.3x3.slash")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(notice)
                    .font(.caption2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
            .padding(.horizontal, 12)
            .transition(.move(edge: .top).combined(with: .opacity))
            .animation(.snappy, value: notice)
        }
    }

    /// Said out loud rather than implied.
    ///
    /// DC has roughly eleven thousand bus stops and the server caps what it
    /// returns. Showing four hundred of them without a word would read as the
    /// map being complete, which is a quieter and worse failure than saying so.
    @ViewBuilder
    private var truncationNotice: some View {
        if settings.showTransitStops, planner.transitStopsTruncated {
            Label("Zoom in for every stop", systemImage: "arrow.up.left.and.arrow.down.right")
                .font(.caption2.weight(.medium))
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(.regularMaterial, in: Capsule())
        }
    }

    // MARK: - Controls

    /// Hand-placed so they clear the status bar and Dynamic Island. The
    /// automatic placement sits flush with the top of the map, which the map
    /// deliberately extends under.
    private var controls: some View {
        VStack(spacing: 10) {
            layersButton
            MapUserLocationButton(scope: mapScope)
            MapScaleView(scope: mapScope)
        }
        .mapControlVisibility(.visible)
        .buttonBorderShape(.circle)
        .padding(.trailing, 10)
        // Clears the status bar / Dynamic Island; the safe area inset is
        // added on top because the map itself ignores it.
        .padding(.top, 14)
        .safeAreaPadding(.top)
    }

    /// One button for every overlay, styled to sit with MapKit's own controls.
    ///
    /// Badged when something is on, so the map never shows a layer the user
    /// has forgotten they enabled without there being a visible reason for it.
    private var layersButton: some View {
        Button {
            showLayers = true
        } label: {
            Image(systemName: activeLayers > 0 ? "square.3.layers.3d.top.filled" : "square.3.layers.3d")
                .font(.system(size: 17))
                .frame(width: 44, height: 44)
                .background(.regularMaterial, in: Circle())
                .overlay(alignment: .topTrailing) {
                    if activeLayers > 0 {
                        Text("\(activeLayers)")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(.white)
                            .frame(width: 16, height: 16)
                            .background(Color.accentColor, in: Circle())
                            .offset(x: 2, y: -2)
                    }
                }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(
            activeLayers == 0 ? "Map layers" : "Map layers, \(activeLayers) on"
        )
    }

    private var activeLayers: Int {
        [settings.showTransitStops, settings.showCrimeGrid, settings.showCameraOverlay]
            .filter { $0 }.count
    }

    private var selectedCellBinding: Binding<CrimeCell?> {
        @Bindable var planner = planner
        return $planner.selectedCell
    }

    private var selectedStopBinding: Binding<TransitStop?> {
        @Bindable var planner = planner
        return $planner.selectedStop
    }

    // MARK: - Transit stops

    /// Metro stations and bus stops.
    ///
    /// Rail is drawn larger and labelled; buses are small unlabelled dots.
    /// There are two orders of magnitude more bus stops than stations, and
    /// labelling them all turns a map of the city into a wall of text.
    @MapContentBuilder
    private var transitStops: some MapContent {
        ForEach(planner.transitStops) { stop in
            Annotation(
                stop.isRail ? stop.name : "",
                coordinate: stop.coordinate,
                anchor: .center
            ) {
                Image(systemName: stop.symbolName)
                    .font(.system(size: stop.isRail ? 11 : 8))
                    .foregroundStyle(.white)
                    .padding(stop.isRail ? 5 : 3)
                    .background(
                        stop.isRail ? Theme.railTint : Theme.busTint,
                        in: Circle()
                    )
                    .overlay(Circle().strokeBorder(.white, lineWidth: 1.5))
                    .onTapGesture { planner.selectedStop = stop }
            }
            .annotationTitles(stop.isRail ? .automatic : .hidden)
        }
    }

    // MARK: - Route rendering

    private var unselectedItineraries: [Itinerary] {
        planner.itineraries.filter { $0.id != planner.selectedItinerary?.id }
    }

    @MapContentBuilder
    private func routeLines(for itinerary: Itinerary) -> some MapContent {
        ForEach(Array(itinerary.legs.enumerated()), id: \.offset) { _, leg in
            if leg.isWalking {
                MapPolyline(coordinates: leg.coordinates)
                    .stroke(
                        Theme.routeLine,
                        style: StrokeStyle(
                            lineWidth: 7,
                            lineCap: .round,
                            lineJoin: .round,
                            // Walking legs are dashed so a glance distinguishes
                            // them from a train leg without needing the legend.
                            dash: [1, 11]
                        )
                    )
            } else {
                MapPolyline(coordinates: leg.coordinates)
                    .stroke(
                        Color.green,
                        style: StrokeStyle(lineWidth: 7, lineCap: .round, lineJoin: .round)
                    )
            }
        }

        ForEach(Array(itinerary.transitLegs.enumerated()), id: \.offset) { _, leg in
            if let first = leg.coordinates.first {
                Annotation(leg.fromStopName, coordinate: first) {
                    Image(systemName: leg.transitSymbol)
                        .font(.caption)
                        .padding(5)
                        .background(.green, in: Circle())
                        .foregroundStyle(.white)
                }
            }
        }
    }

    @MapContentBuilder
    private var endpointMarkers: some MapContent {
        if let origin = planner.origin, let coordinate = origin.fixedCoordinate {
            Annotation(origin.displayName, coordinate: coordinate) {
                Image(systemName: "a.circle.fill")
                    .font(.title2)
                    .foregroundStyle(.white, Color.accentColor)
            }
        }
        if let destination = planner.destination,
           let coordinate = destination.fixedCoordinate {
            Annotation(destination.displayName, coordinate: coordinate) {
                Image(systemName: "b.circle.fill")
                    .font(.title2)
                    .foregroundStyle(.white, Color.red)
            }
        }
    }

    // MARK: - Crime grid

    @MapContentBuilder
    private var crimeGrid: some MapContent {
        ForEach(planner.crimeCells) { cell in
            // Only the selected cell is stroked. Every stroked polygon costs
            // a second draw pass, and across a couple of hundred cells that
            // outline was the single biggest contributor to the stutter — the
            // fill alone reads perfectly well as a grid.
            if planner.selectedCell?.id == cell.id {
                MapPolygon(coordinates: cell.polygon)
                    .foregroundStyle(Theme.crimeCellColor(cell.intensity))
                    .stroke(Color.primary, lineWidth: 2.5)
            } else {
                MapPolygon(coordinates: cell.polygon)
                    .foregroundStyle(Theme.crimeCellColor(cell.intensity))
            }
        }
    }

    // MARK: - Cameras

    @MapContentBuilder
    private var cameraOverlay: some MapContent {
        ForEach(planner.cameras) { camera in
            if let direction = camera.direction {
                // A mapped bearing draws as a cone showing roughly what the
                // camera can see.
                MapPolygon(coordinates: Self.coneCoordinates(
                    from: camera.coordinate, bearing: direction
                ))
                .foregroundStyle(Theme.cameraTint.opacity(0.22))
                .stroke(Theme.cameraTint.opacity(0.5), lineWidth: 1)
            } else {
                // No bearing in the data, so no direction is implied.
                MapCircle(center: camera.coordinate, radius: 45)
                    .foregroundStyle(Theme.cameraTint.opacity(0.14))
                    .stroke(Theme.cameraTint.opacity(0.4), lineWidth: 1)
            }

            Annotation("", coordinate: camera.coordinate) {
                Image(systemName: "camera.fill")
                    .font(.system(size: 9))
                    .padding(4)
                    .background(Theme.cameraTint, in: Circle())
                    .foregroundStyle(.white)
                    .accessibilityLabel("Flock camera")
            }
        }
    }

    /// Vertices of a camera's field-of-view wedge.
    ///
    /// Matches the backend's `CameraConfig` so what the map shows is what the
    /// router actually costed — a cone drawn wider than the one being routed
    /// around would make the avoidance look broken.
    static func coneCoordinates(
        from center: CLLocationCoordinate2D,
        bearing: Double,
        rangeMetres: Double = 70,
        halfAngle: Double = 35,
        steps: Int = 12
    ) -> [CLLocationCoordinate2D] {
        var points = [center]
        let metresPerDegreeLat = 111_320.0
        let metresPerDegreeLon = 111_320.0 * cos(center.latitude * .pi / 180)

        for i in 0...steps {
            let angle = bearing - halfAngle + (2 * halfAngle) * Double(i) / Double(steps)
            let radians = angle * .pi / 180
            points.append(
                CLLocationCoordinate2D(
                    latitude: center.latitude + (rangeMetres * cos(radians)) / metresPerDegreeLat,
                    longitude: center.longitude + (rangeMetres * sin(radians)) / metresPerDegreeLon
                )
            )
        }
        points.append(center)
        return points
    }

    // MARK: - Tap handling

    private func handleTap(at coordinate: CLLocationCoordinate2D) {
        // Cameras win over cells: they are much smaller targets, so if the tap
        // is near one that is almost certainly what was meant.
        if settings.showCameraOverlay,
           let camera = nearestCamera(to: coordinate, within: 45) {
            selectedCamera = camera
            return
        }
        // Stops before cells, for the same reason: a stop is a small target
        // sitting on top of a very large one.
        if settings.showTransitStops,
           let stop = nearestStop(to: coordinate, within: 60) {
            planner.selectedStop = stop
            return
        }
        if settings.showCrimeGrid, let cell = cell(containing: coordinate) {
            planner.selectedCell = planner.selectedCell?.id == cell.id ? nil : cell
        }
    }

    private func nearestStop(
        to coordinate: CLLocationCoordinate2D, within metres: Double
    ) -> TransitStop? {
        let target = CLLocation(latitude: coordinate.latitude, longitude: coordinate.longitude)
        return planner.transitStops
            .map { ($0, CLLocation(latitude: $0.lat, longitude: $0.lon).distance(from: target)) }
            .filter { $0.1 <= metres }
            .min { $0.1 < $1.1 }?.0
    }

    private func nearestCamera(
        to coordinate: CLLocationCoordinate2D, within metres: Double
    ) -> ALPRCamera? {
        let target = CLLocation(latitude: coordinate.latitude, longitude: coordinate.longitude)
        return planner.cameras
            .map { ($0, CLLocation(latitude: $0.lat, longitude: $0.lon).distance(from: target)) }
            .filter { $0.1 <= metres }
            .min { $0.1 < $1.1 }?.0
    }

    /// The cell a tap fell in.
    ///
    /// Nearest-centre rather than a point-in-polygon test: for a regular
    /// hexagonal tiling the two are equivalent, and the distance check is far
    /// cheaper across several hundred cells.
    private func cell(containing coordinate: CLLocationCoordinate2D) -> CrimeCell? {
        guard planner.crimeCellRadius > 0 else { return nil }
        let target = CLLocation(latitude: coordinate.latitude, longitude: coordinate.longitude)
        return planner.crimeCells
            .map { ($0, CLLocation(latitude: $0.centerLat, longitude: $0.centerLon).distance(from: target)) }
            .filter { $0.1 <= planner.crimeCellRadius }
            .min { $0.1 < $1.1 }?.0
    }
}

// MARK: - Detail sheets

/// What happened inside one hexagon.
struct CrimeCellSheet: View {
    let cell: CrimeCell
    let radius: Double

    @Environment(AppSettings.self) private var settings
    @Environment(\.dismiss) private var dismiss

    /// Set when this cell came from a downloaded pack rather than the server.
    var isOffline: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(cell.total) incident\(cell.total == 1 ? "" : "s")")
                        .font(.title2.bold())
                    // Naming the window matters: "12 incidents" means
                    // something very different over 30 days than over a year,
                    // and the number alone does not say which you are seeing.
                    Text(
                        "Within about \(Int(radius)) m · last "
                            + settings.crimeWindow.label
                            + (isOffline ? " · downloaded data" : "")
                    )
                    .font(.caption)
                    .foregroundStyle(isOffline ? .orange : .secondary)
                }
                Spacer()
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.title2)
                        .foregroundStyle(.secondary)
                }
                .accessibilityLabel("Close")
            }

            HStack(spacing: 22) {
                stat(
                    "\(cell.seriousCount)",
                    "Violent crimes",
                    tint: cell.seriousCount > 0 ? Theme.seriousCrimeTint : .primary
                )
                stat("\(Int(cell.nightShare * 100))%", "At night")
                if !cell.latest.isEmpty {
                    stat(cell.latest, "Most recent")
                }
            }

            Divider()

            VStack(alignment: .leading, spacing: 2) {
                Text("What drives the risk here")
                    .font(.subheadline.weight(.semibold))
                // Without this the ordering looks wrong: a type with a smaller
                // count can sit above one with a larger count.
                Text("Ordered by weight, not by how many")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            ScrollView {
                VStack(spacing: 10) {
                    ForEach(cell.byOffense, id: \.offense) { entry in
                        VStack(spacing: 4) {
                            HStack(spacing: 6) {
                                Circle()
                                    .fill(Theme.categoryTint(entry.category))
                                    .frame(width: 7, height: 7)
                                Text(entry.label)
                                    .font(.subheadline)
                                    .lineLimit(1)
                                Spacer()
                                Text("\(entry.count)")
                                    .font(.subheadline.monospacedDigit())
                                    .foregroundStyle(.secondary)
                                Text("\(Int(entry.share * 100))%")
                                    .font(.caption.monospacedDigit())
                                    .foregroundStyle(Theme.categoryTint(entry.category))
                                    .frame(width: 38, alignment: .trailing)
                            }
                            // The bar shows share of risk, not share of count,
                            // so the picture matches the ordering.
                            GeometryReader { geometry in
                                Capsule()
                                    .fill(Theme.categoryTint(entry.category).opacity(0.85))
                                    .frame(width: geometry.size.width * CGFloat(entry.share))
                            }
                            .frame(height: 4)
                        }
                    }
                }
            }

            Text(
                "Reported incidents from MPD over the last three years, "
                    + "weighted by how much each offence bears on the safety "
                    + "of someone walking past. Reporting varies by "
                    + "neighborhood — this is not a measure of the people "
                    + "who live here."
            )
            .font(.footnote)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
        }
        .padding(20)
    }

    private func stat(
        _ value: String, _ label: String, tint: Color = .primary
    ) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(value).font(.headline.monospacedDigit()).foregroundStyle(tint)
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
    }
}

/// Detail for a tapped camera, including the honest caveat about the data.
struct CameraDetailSheet: View {
    let camera: ALPRCamera
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: "camera.fill")
                    .font(.title3)
                    .padding(9)
                    .background(Theme.cameraTint, in: Circle())
                    .foregroundStyle(.white)
                VStack(alignment: .leading) {
                    Text("Flock camera")
                        .font(.headline)
                    Text(
                        camera.operatorName.isEmpty
                            ? "Licence plate reader"
                            : camera.operatorName
                    )
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                }
                Spacer()
            }

            if let direction = camera.direction {
                Label(
                    "Faces \(Self.compass(direction)) (\(Int(direction))°)",
                    systemImage: "location.north.line"
                )
                .font(.subheadline)
            } else {
                Label("Direction not recorded", systemImage: "questionmark.circle")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Text(
                """
                Camera locations come from OpenStreetMap contributors. Coverage \
                is incomplete — a street with no camera shown here may still \
                have one. Not every plate reader is operated by Flock.
                """
            )
            .font(.footnote)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)

            Spacer()

            Button("Done") { dismiss() }
                .buttonStyle(.borderedProminent)
                .frame(maxWidth: .infinity)
        }
        .padding(20)
    }

    private static func compass(_ bearing: Double) -> String {
        let names = ["north", "northeast", "east", "southeast",
                     "south", "southwest", "west", "northwest"]
        let index = Int((bearing.truncatingRemainder(dividingBy: 360) + 22.5) / 45) % 8
        return names[index]
    }
}
