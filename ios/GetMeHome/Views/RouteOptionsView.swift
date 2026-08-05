import SwiftUI

/// The card list the user picks a route from.
struct RouteOptionsView: View {
    @Environment(PlannerViewModel.self) private var planner
    @Environment(AppSettings.self) private var settings

    var onStart: () -> Void
    var onCancel: () -> Void

    var body: some View {
        @Bindable var planner = planner

        VStack(spacing: 0) {
            header

            ScrollView {
                LazyVStack(spacing: 12) {
                    ForEach(planner.itineraries) { itinerary in
                        RouteCard(
                            itinerary: itinerary,
                            isSelected: itinerary.id == planner.selectedItinerary?.id,
                            isNight: planner.isNight,
                            showCameras: settings.avoidCameras || settings.showCameraOverlay
                        )
                        .onTapGesture {
                            withAnimation(.snappy) {
                                planner.selectedItineraryID = itinerary.id
                            }
                        }
                    }

                    ForEach(planner.notices, id: \.self) { notice in
                        Label(notice, systemImage: "info.circle")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 4)
                    }
                }
                .padding(.horizontal)
                .padding(.bottom, 8)
            }

            startButton
        }
        .background(.regularMaterial)
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(planner.destination?.name ?? "Route")
                    .font(.headline)
                    .lineLimit(1)
                HStack(spacing: 5) {
                    Image(systemName: planner.isNight ? "moon.stars.fill" : "sun.max.fill")
                        .font(.caption2)
                    Text(
                        planner.isNight
                            ? "Scored for night — lighting counts"
                            : "Scored for daytime"
                    )
                    .font(.caption)
                }
                .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                onCancel()
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.title2)
                    .foregroundStyle(.secondary)
            }
            .accessibilityLabel("Cancel route")
        }
        .padding(.horizontal)
        .padding(.top, 14)
        .padding(.bottom, 10)
    }

    private var startButton: some View {
        Button(action: onStart) {
            Label("Start", systemImage: "location.north.fill")
                .font(.headline)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 6)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .padding(.horizontal)
        .padding(.bottom, 10)
        .disabled(planner.selectedItinerary == nil)
    }
}

struct RouteCard: View {
    let itinerary: Itinerary
    let isSelected: Bool
    let isNight: Bool
    let showCameras: Bool

    @State private var showBreakdown = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(itinerary.label)
                        .font(.headline)
                    Text(itinerary.summary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                SafetyBadge(score: itinerary.safety)
            }

            HStack(spacing: 14) {
                Label(Format.duration(itinerary.duration), systemImage: "clock")
                Label(Format.distance(itinerary.walkDistance), systemImage: "figure.walk")
                if itinerary.isTransit {
                    Label(
                        itinerary.numTransfers == 0
                            ? "Direct"
                            : "\(itinerary.numTransfers) transfer\(itinerary.numTransfers == 1 ? "" : "s")",
                        systemImage: "arrow.triangle.swap"
                    )
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            if itinerary.isTransit {
                transitSummary
            }

            if showCameras, itinerary.cameras.camerasPassed > 0 {
                Label(
                    "Passes \(itinerary.cameras.camerasPassed) plate reader\(itinerary.cameras.camerasPassed == 1 ? "" : "s")",
                    systemImage: "camera.fill"
                )
                .font(.caption)
                .foregroundStyle(Theme.cameraTint)
            }

            DisclosureGroup("Why this score", isExpanded: $showBreakdown) {
                SafetyBreakdownView(score: itinerary.safety, isNight: isNight)
                    .padding(.top, 6)
            }
            .font(.caption)
            .tint(.secondary)
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: Theme.cardCorner)
                .fill(Color(.secondarySystemBackground))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.cardCorner)
                .strokeBorder(
                    isSelected ? Color.accentColor : Color.clear,
                    lineWidth: 2
                )
        )
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "\(itinerary.label), \(Format.duration(itinerary.duration)), "
                + "safety \(itinerary.safety.overall) out of 100, "
                + itinerary.safety.bandLabel
        )
    }

    private var transitSummary: some View {
        HStack(spacing: 6) {
            ForEach(Array(itinerary.transitLegs.enumerated()), id: \.offset) { index, leg in
                if index > 0 {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8))
                        .foregroundStyle(.tertiary)
                }
                HStack(spacing: 3) {
                    Image(systemName: leg.transitSymbol)
                        .font(.system(size: 10))
                    Text(leg.routeName)
                        .font(.caption2.weight(.medium))
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .background(Color.green.opacity(0.16), in: Capsule())
            }
            Spacer()
            if !itinerary.departureTime.isEmpty {
                Text("\(itinerary.departureTime)–\(itinerary.arrivalTime)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct SafetyBadge: View {
    let score: SafetyScore

    var body: some View {
        VStack(spacing: 1) {
            Text("\(score.overall)")
                .font(.title3.bold().monospacedDigit())
            Text(score.bandLabel)
                .font(.system(size: 9, weight: .medium))
                .textCase(.uppercase)
        }
        .foregroundStyle(.white)
        .frame(width: 62, height: 46)
        .background(Theme.safetyColor(score.overall), in: RoundedRectangle(cornerRadius: 10))
    }
}

/// Breaks the headline number into the factors that produced it.
///
/// A single opaque "safety: 72" invites either blind trust or dismissal.
/// Showing that it is 72 because the route is well lit but passes through a
/// higher-incident block lets someone weigh it against what they know about
/// the neighbourhood themselves.
struct SafetyBreakdownView: View {
    let score: SafetyScore
    let isNight: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if isNight {
                factor("Lighting", value: score.lighting, detail: "\(score.pctWellLit)% well lit")
            } else {
                HStack {
                    Text("Lighting")
                    Spacer()
                    Text("Not a factor in daylight")
                        .foregroundStyle(.secondary)
                }
                .font(.caption)
            }

            factor(
                "Low crime",
                value: score.crime,
                detail: score.pctHighCrime > 0
                    ? "\(score.pctHighCrime)% through higher-incident areas"
                    : "Avoids higher-incident areas"
            )
            factor("Street exposure", value: score.isolation, detail: nil)

            if score.worstStretchRisk > 55, !score.worstStretchName.isEmpty {
                Label(
                    "Worst stretch: \(score.worstStretchName)",
                    systemImage: "exclamationmark.triangle"
                )
                .font(.caption2)
                .foregroundStyle(Theme.safetyColor(100 - score.worstStretchRisk))
                .padding(.top, 2)
            }

            Text(
                "Based on DDOT streetlight locations and MPD incident reports. "
                    + "Guidance only — trust your own judgement."
            )
            .font(.system(size: 10))
            .foregroundStyle(.tertiary)
            .padding(.top, 2)
        }
    }

    private func factor(_ title: String, value: Int, detail: String?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(title)
                Spacer()
                Text("\(value)")
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            .font(.caption)

            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(Color(.tertiarySystemFill))
                    Capsule()
                        .fill(Theme.safetyColor(value))
                        .frame(width: geometry.size.width * CGFloat(value) / 100)
                }
            }
            .frame(height: 5)

            if let detail {
                Text(detail)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
        }
    }
}
