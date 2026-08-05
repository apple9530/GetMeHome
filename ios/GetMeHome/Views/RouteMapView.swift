import CoreLocation
import MapKit
import SwiftUI

/// The map: route lines, ALPR camera cones, and the street-safety overlay.
struct RouteMapView: View {
    @Environment(PlannerViewModel.self) private var planner
    @Environment(AppSettings.self) private var settings
    @Environment(LocationService.self) private var location

    @Binding var cameraPosition: MapCameraPosition

    @State private var selectedCamera: ALPRCamera?

    var body: some View {
        MapReader { proxy in
            Map(position: $cameraPosition, interactionModes: .all) {
                UserAnnotation()

                if settings.showSafetyOverlay {
                    safetyOverlay
                }

                // Unselected routes first so the chosen one draws on top.
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

                if let destination = planner.destination {
                    Marker(destination.name, systemImage: "flag.fill", coordinate: destination.coordinate)
                        .tint(.red)
                }
            }
            .mapStyle(.standard(elevation: .flat, pointsOfInterest: .excludingAll))
            .mapControls {
                MapCompass()
                MapUserLocationButton()
                MapScaleView()
            }
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
                // Tapping a camera icon opens its detail. Anything else on the
                // map is left alone so panning stays the primary interaction.
                if let coordinate = proxy.convert(screenPoint, from: .local) {
                    selectedCamera = nearestCamera(to: coordinate, within: 40)
                }
            }
            .sheet(item: $selectedCamera) { camera in
                CameraDetailSheet(camera: camera)
                    .presentationDetents([.height(280)])
            }
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
                            // Walking legs are dashed so a glance at the map
                            // distinguishes them from a train leg without
                            // needing the legend.
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

    // MARK: - Overlays

    @MapContentBuilder
    private var safetyOverlay: some MapContent {
        ForEach(Array(planner.safetySegments.enumerated()), id: \.offset) { _, segment in
            MapPolyline(coordinates: segment.coordinates)
                .stroke(
                    Theme.riskColor(segment.risk).opacity(0.55),
                    style: StrokeStyle(lineWidth: 3, lineCap: .round)
                )
        }
    }

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
                    .accessibilityLabel(
                        "License plate reader\(camera.operatorName.isEmpty ? "" : ", \(camera.operatorName)")"
                    )
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

    private func nearestCamera(
        to coordinate: CLLocationCoordinate2D, within metres: Double
    ) -> ALPRCamera? {
        let target = CLLocation(latitude: coordinate.latitude, longitude: coordinate.longitude)
        return planner.cameras
            .map { ($0, CLLocation(latitude: $0.lat, longitude: $0.lon).distance(from: target)) }
            .filter { $0.1 <= metres }
            .min { $0.1 < $1.1 }?.0
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
                    Text("Licence plate reader")
                        .font(.headline)
                    if !camera.operatorName.isEmpty {
                        Text(camera.operatorName)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
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
                have one.
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
