import MapKit
import SwiftUI

/// The departure board for one stop, and from there a vehicle's whole journey.
///
/// Two things this screen has to be honest about, because getting them wrong
/// is worse than not showing them at all:
///
/// * **Scheduled and live are different claims.** A timetable time is what is
///   meant to happen; a prediction is what the operator currently expects. The
///   board marks which one each row is showing rather than presenting both as
///   the same kind of fact.
/// * **A train's position is an estimate.** WMATA publishes track circuits
///   rather than coordinates, so a train dot is interpolated between stations.
///   It says so wherever it appears. A bus dot is reported and does not.
struct TransitStopView: View {
    let stop: TransitStop

    @Environment(AppSettings.self) private var settings
    @Environment(\.routingClient) private var client
    @Environment(\.dismiss) private var dismiss

    @State private var board: StopBoard?
    @State private var error: String?
    @State private var isLoading = true
    @State private var selected: Departure?
    /// Set when the server says this stop does not exist, which no amount of
    /// retrying will fix.
    @State private var isUnrecoverable = false

    var body: some View {
        NavigationStack {
            Group {
                if let board, !board.departures.isEmpty {
                    departureList(board)
                } else if isLoading {
                    ProgressView("Loading departures…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    emptyState
                }
            }
            .navigationTitle(stop.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .task { await pollWhileVisible() }
        .sheet(item: $selected) { departure in
            TransitTripView(departure: departure, fromStopId: stop.id)
        }
    }

    // MARK: - Board

    private func departureList(_ board: StopBoard) -> some View {
        List {
            Section {
                ForEach(board.departures) { departure in
                    Button {
                        selected = departure
                    } label: {
                        departureRow(departure)
                    }
                    .buttonStyle(.plain)
                }
            } header: {
                HStack(spacing: 6) {
                    Image(systemName: stop.symbolName)
                    Text(stop.routes.prefix(6).joined(separator: " · "))
                        .lineLimit(1)
                }
            } footer: {
                if !board.liveNote.isEmpty {
                    Text(board.liveNote)
                } else if board.live {
                    Text(
                        "Times marked live come from WMATA's own predictions. "
                            + "The rest are from the timetable."
                    )
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    private func departureRow(_ departure: Departure) -> some View {
        HStack(spacing: 12) {
            Text(departure.routeName)
                .font(.subheadline.weight(.bold))
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 5)
                .frame(minWidth: 46)
                .background(
                    departure.isRail ? Theme.railTint : Theme.busTint,
                    in: RoundedRectangle(cornerRadius: 7)
                )

            VStack(alignment: .leading, spacing: 2) {
                Text(departure.headsign.isEmpty ? "Service" : departure.headsign)
                    .font(.subheadline)
                    .lineLimit(1)
                HStack(spacing: 5) {
                    if departure.isLive {
                        // Only claimed where it is true. A live badge on a
                        // timetable row would be a lie the user cannot check.
                        Image(systemName: "dot.radiowaves.left.and.right")
                            .font(.system(size: 9))
                            .foregroundStyle(.green)
                        Text("Live")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.green)
                        Text("· timetable \(departure.scheduledTime)")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("Timetable \(departure.scheduledTime)")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            Spacer(minLength: 4)

            VStack(alignment: .trailing, spacing: 1) {
                Text(departure.minutesLabel)
                    .font(.subheadline.weight(.semibold).monospacedDigit())
                Image(systemName: "chevron.right")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 3)
        .contentShape(Rectangle())
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "clock.badge.xmark")
                .font(.largeTitle)
                .foregroundStyle(.secondary)
            Text(error ?? "Nothing scheduled from here in the next few hours.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            if error != nil {
                Button("Try again") {
                    // Clear the flag as well, or a stop that 404'd once can
                    // never be retried even after the server is rebuilt.
                    isUnrecoverable = false
                    Task { await load() }
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Loading

    /// Reload every 30 seconds for as long as the sheet is on screen.
    ///
    /// The loop lives directly inside `.task` rather than in a `Task` this
    /// view stores. SwiftUI cancels a `.task` when the view goes away or its
    /// identity changes; a hand-rolled one is only cancelled by `onDisappear`,
    /// so a redraw that rebuilt the view left the old loop running and started
    /// another beside it. Two became four, and the server saw a burst of
    /// identical requests.
    ///
    /// Thirty seconds matches how often WMATA's predictions actually move.
    /// Polling faster spends the API quota redrawing identical numbers.
    private func pollWhileVisible() async {
        while !Task.isCancelled {
            await load()
            if isUnrecoverable { return }
            try? await Task.sleep(for: .seconds(30))
        }
    }

    private func load() async {
        do {
            let fetched = try await client.stopBoard(stop.id, city: settings.citySlug)
            guard !Task.isCancelled else { return }
            board = fetched
            error = nil
        } catch {
            guard !Task.isCancelled else { return }
            // A 404 means this stop id is not one the server knows. Retrying
            // cannot help, and hammering it thirty seconds apart forever is
            // just noise in someone's log.
            if case let RoutingError.server(status, _) = error, status == 404 {
                isUnrecoverable = true
            }
            // Otherwise keep whatever is already on screen: a stale board
            // beats an error page when only the refresh failed.
            if board == nil {
                self.error = (error as? RoutingError)?.errorDescription
                    ?? error.localizedDescription
            }
        }
        isLoading = false
    }
}

/// One vehicle's journey: where it is, its route, and its time at every
/// remaining stop.
struct TransitTripView: View {
    let departure: Departure
    let fromStopId: String

    @Environment(AppSettings.self) private var settings
    @Environment(\.routingClient) private var client
    @Environment(\.dismiss) private var dismiss

    @State private var detail: TripDetail?
    @State private var error: String?
    @State private var cameraPosition: MapCameraPosition = .automatic
    @State private var isUnrecoverable = false

    var body: some View {
        NavigationStack {
            Group {
                if let detail {
                    content(detail)
                } else if let error {
                    ContentUnavailableView(
                        "Can't follow this vehicle", systemImage: "antenna.radiowaves.left.and.right.slash",
                        description: Text(error)
                    )
                } else {
                    ProgressView("Loading…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .navigationTitle("\(departure.routeName) · \(departure.headsign)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .task { await pollWhileVisible() }
    }

    private func content(_ detail: TripDetail) -> some View {
        VStack(spacing: 0) {
            tripMap(detail)
                .frame(height: 220)

            if let punctuality = detail.punctuality {
                Label(punctuality, systemImage: "clock.arrow.circlepath")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(detail.deviationSeconds > 120 ? .orange : .secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal)
                    .padding(.top, 8)
            }

            if !detail.liveNote.isEmpty {
                Text(detail.liveNote)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal)
                    .padding(.top, 6)
            }

            stopList(detail)
        }
    }

    private func tripMap(_ detail: TripDetail) -> some View {
        Map(position: $cameraPosition) {
            MapPolyline(coordinates: detail.coordinates)
                .stroke(
                    departure.isRail ? Theme.railTint : Theme.busTint,
                    style: StrokeStyle(lineWidth: 5, lineCap: .round, lineJoin: .round)
                )

            ForEach(detail.stops) { stop in
                Annotation("", coordinate: stop.coordinate) {
                    Circle()
                        .fill(stop.passed ? Color.secondary.opacity(0.4) : .white)
                        .stroke(
                            departure.isRail ? Theme.railTint : Theme.busTint,
                            lineWidth: 2
                        )
                        .frame(width: 8, height: 8)
                }
                .annotationTitles(.hidden)
            }

            if let vehicle = detail.vehicle {
                Annotation("", coordinate: vehicle.coordinate) {
                    vehicleMarker(estimated: vehicle.estimated)
                }
                .annotationTitles(.hidden)
            }
        }
        .mapStyle(.standard(pointsOfInterest: .excludingAll))
        .onAppear { frame(detail) }
    }

    /// A reported position is solid; an estimated one is drawn dashed and
    /// slightly translucent, so the difference is visible without reading the
    /// caption underneath.
    private func vehicleMarker(estimated: Bool) -> some View {
        Image(systemName: departure.isRail ? "tram.fill" : "bus.fill")
            .font(.system(size: 12))
            .foregroundStyle(.white)
            .padding(6)
            .background(
                (departure.isRail ? Theme.railTint : Theme.busTint)
                    .opacity(estimated ? 0.7 : 1.0),
                in: Circle()
            )
            .overlay(
                Circle().strokeBorder(
                    .white,
                    style: StrokeStyle(
                        lineWidth: 2, dash: estimated ? [3, 2] : []
                    )
                )
            )
            .shadow(radius: 2)
    }

    private func stopList(_ detail: TripDetail) -> some View {
        List(detail.stops) { stop in
            HStack(spacing: 12) {
                Circle()
                    .fill(stop.passed ? Color.secondary.opacity(0.35) : Color.accentColor)
                    .frame(width: 8, height: 8)

                Text(stop.name)
                    .font(.subheadline)
                    .foregroundStyle(stop.passed ? .secondary : .primary)
                    .lineLimit(1)

                Spacer(minLength: 4)

                VStack(alignment: .trailing, spacing: 0) {
                    Text(stop.arrivalTime)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(stop.passed ? .tertiary : .secondary)
                    if !stop.passed, stop.minutes >= 0 {
                        Text(stop.minutes == 0 ? "now" : "\(stop.minutes) min")
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .listRowBackground(
                stop.stopId == fromStopId
                    ? Color.accentColor.opacity(0.12)
                    : Color.clear
            )
        }
        .listStyle(.plain)
    }

    private func frame(_ detail: TripDetail) {
        let coordinates = detail.coordinates
        guard !coordinates.isEmpty else { return }
        let lats = coordinates.map(\.latitude)
        let lons = coordinates.map(\.longitude)
        guard let minLat = lats.min(), let maxLat = lats.max(),
              let minLon = lons.min(), let maxLon = lons.max() else { return }

        cameraPosition = .region(
            MKCoordinateRegion(
                center: CLLocationCoordinate2D(
                    latitude: (minLat + maxLat) / 2, longitude: (minLon + maxLon) / 2
                ),
                span: MKCoordinateSpan(
                    latitudeDelta: max(0.006, (maxLat - minLat) * 1.35),
                    longitudeDelta: max(0.006, (maxLon - minLon) * 1.35)
                )
            )
        )
    }

    /// As above: the loop belongs to `.task` so SwiftUI owns its lifetime.
    private func pollWhileVisible() async {
        while !Task.isCancelled {
            await load()
            if isUnrecoverable { return }
            try? await Task.sleep(for: .seconds(20))
        }
    }

    private func load() async {
        do {
            let fetched = try await client.tripDetail(
                patternId: departure.patternId,
                tripId: departure.tripId,
                fromStop: fromStopId,
                vehicleId: departure.vehicleId,
                city: settings.citySlug
            )
            guard !Task.isCancelled else { return }
            detail = fetched
            error = nil
        } catch {
            guard !Task.isCancelled else { return }
            if case let RoutingError.server(status, _) = error, status == 404 {
                // The trip has finished, or the timetable was rebuilt under
                // us. Either way it will not come back.
                isUnrecoverable = true
            }
            guard detail == nil else { return }
            self.error = (error as? RoutingError)?.errorDescription
                ?? error.localizedDescription
        }
    }
}
