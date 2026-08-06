import SwiftUI

/// The card list the user picks a route from.
struct RouteOptionsView: View {
    @Environment(PlannerViewModel.self) private var planner
    @Environment(AppSettings.self) private var settings

    var onStart: () -> Void
    var onCancel: () -> Void

    /// Which kind of journey is on screen.
    ///
    /// Walking and transit itineraries used to share one scrolling list, which
    /// put a 40-minute walk and a 12-minute Metro ride in the same column of
    /// near-identical cards. They answer different questions and are not really
    /// alternatives to each other, so they get their own tab.
    enum Mode: Hashable {
        case walk
        case transit

        var title: String {
            switch self {
            case .walk: "Walk"
            case .transit: "Transit"
            }
        }

        var symbolName: String {
            switch self {
            case .walk: "figure.walk"
            case .transit: "tram.fill"
            }
        }
    }

    @State private var mode: Mode = .walk

    var body: some View {
        @Bindable var planner = planner

        VStack(spacing: 0) {
            header

            if hasBothModes {
                modePicker
            }

            ScrollView {
                LazyVStack(spacing: 12) {
                    if visibleItineraries.isEmpty {
                        emptyModeNotice
                    }

                    ForEach(visibleItineraries) { itinerary in
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
        .onAppear { selectInitialMode() }
        .onChange(of: planner.itineraries) { _, _ in selectInitialMode() }
        .onChange(of: mode) { _, _ in selectFirstOfMode() }
    }

    // MARK: - Mode tabs

    private var walkItineraries: [Itinerary] {
        planner.itineraries.filter { !$0.isTransit }
    }

    private var transitItineraries: [Itinerary] {
        planner.itineraries.filter(\.isTransit)
    }

    private var visibleItineraries: [Itinerary] {
        mode == .walk ? walkItineraries : transitItineraries
    }

    private var hasBothModes: Bool {
        !walkItineraries.isEmpty && !transitItineraries.isEmpty
    }

    private var modePicker: some View {
        Picker("Journey type", selection: $mode.animation(.snappy)) {
            ForEach([Mode.walk, Mode.transit], id: \.self) { option in
                Label(
                    "\(option.title) (\(option == .walk ? walkItineraries.count : transitItineraries.count))",
                    systemImage: option.symbolName
                )
                .tag(option)
            }
        }
        .pickerStyle(.segmented)
        .padding(.horizontal)
        .padding(.bottom, 10)
    }

    private var emptyModeNotice: some View {
        Label(
            mode == .transit
                ? "No transit route found for this trip."
                : "No walking route found for this trip.",
            systemImage: "info.circle"
        )
        .font(.footnote)
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 8)
    }

    /// Open on whichever tab holds the currently selected route, so a fresh
    /// set of results never lands on an empty tab.
    private func selectInitialMode() {
        if let selected = planner.selectedItinerary {
            mode = selected.isTransit ? .transit : .walk
        } else if walkItineraries.isEmpty {
            mode = .transit
        } else {
            mode = .walk
        }
    }

    /// Switching tabs selects that tab's best option, so the map and the Start
    /// button always match what is on screen.
    private func selectFirstOfMode() {
        guard let first = visibleItineraries.first else { return }
        if planner.selectedItinerary?.isTransit != (mode == .transit) {
            planner.selectedItineraryID = first.id
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                HStack(spacing: 5) {
                    Text(planner.origin?.displayName ?? "Start")
                        .lineLimit(1)
                    Image(systemName: "arrow.right")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Text(planner.destinationName)
                        .lineLimit(1)
                }
                .font(.headline)

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

            timeOfDayControl
        }
        .padding(.horizontal)
        .padding(.top, 14)
        .padding(.bottom, 10)
    }

    /// Day / night scoring, with Auto following the sun.
    ///
    /// Exposed here rather than buried in Settings because it changes what the
    /// safety scores *mean* — lighting carries no weight in daylight, so a
    /// route planned at noon shows no streetlight information at all until you
    /// switch it. Planning tonight's walk home at 3pm is the normal case, not
    /// an edge case.
    private var timeOfDayControl: some View {
        @Bindable var settings = settings

        return VStack(alignment: .leading, spacing: 5) {
            Picker("Scored for", selection: $settings.timeOfDay) {
                ForEach(TimeOfDay.allCases) { option in
                    Label(option.label, systemImage: option.symbolName)
                        .tag(option)
                }
            }
            .pickerStyle(.segmented)

            HStack(spacing: 5) {
                Image(systemName: planner.isNight ? "moon.stars.fill" : "sun.max.fill")
                    .font(.caption2)
                Text(scoringDescription)
                    .font(.caption)
            }
            .foregroundStyle(.secondary)
        }
    }

    private var scoringDescription: String {
        let auto = settings.timeOfDay == .auto ? "Auto · " : ""
        return planner.isNight
            ? "\(auto)scored for night — street lighting counts"
            : "\(auto)scored for daytime — lighting isn't a factor"
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
                    "Passes \(itinerary.cameras.camerasPassed) Flock camera\(itinerary.cameras.camerasPassed == 1 ? "" : "s")",
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
