import CoreLocation
import MapKit
import SwiftUI

/// The map: route lines, Flock camera cones, and the crime grid.
struct RouteMapView: View {
    @Environment(PlannerViewModel.self) private var planner
    @Environment(AppSettings.self) private var settings

    @Binding var cameraPosition: MapCameraPosition

    @State private var selectedCamera: ALPRCamera?
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
            .sheet(item: $selectedCamera) { camera in
                CameraDetailSheet(camera: camera)
                    .presentationDetents([.height(300)])
            }
            .sheet(item: selectedCellBinding) { cell in
                CrimeCellSheet(cell: cell, radius: planner.crimeCellRadius)
                    .presentationDetents([.height(420), .medium])
            }
        }
        .mapScope(mapScope)
    }

    // MARK: - Controls

    /// Hand-placed so they clear the status bar and Dynamic Island. The
    /// automatic placement sits flush with the top of the map, which the map
    /// deliberately extends under.
    private var controls: some View {
        VStack(spacing: 10) {
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

    private var selectedCellBinding: Binding<CrimeCell?> {
        @Bindable var planner = planner
        return $planner.selectedCell
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
        if let coordinate = planner.origin.fixedCoordinate {
            Annotation(planner.origin.displayName, coordinate: coordinate) {
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
            MapPolygon(coordinates: cell.polygon)
                .foregroundStyle(Theme.crimeCellColor(cell.intensity))
                .stroke(
                    planner.selectedCell?.id == cell.id
                        ? Color.primary
                        : Theme.crimeCellColor(cell.intensity).opacity(0.9),
                    lineWidth: planner.selectedCell?.id == cell.id ? 2.5 : 0.5
                )
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
        if settings.showCrimeGrid, let cell = cell(containing: coordinate) {
            planner.selectedCell = planner.selectedCell?.id == cell.id ? nil : cell
        }
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

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(cell.total) incident\(cell.total == 1 ? "" : "s")")
                        .font(.title2.bold())
                    Text("Within about \(Int(radius)) m of here")
                        .font(.caption)
                        .foregroundStyle(.secondary)
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
                    "Violent or sexual",
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
                                Text(entry.displayName)
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
                    + "neighbourhood — this is not a measure of the people "
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
