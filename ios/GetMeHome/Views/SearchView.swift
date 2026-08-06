import SwiftUI
import UIKit

/// Endpoint entry (A → B) and the quick preference toggles.
struct SearchView: View {
    @Environment(PlannerViewModel.self) private var planner
    @Environment(AppSettings.self) private var settings
    @Environment(LocationService.self) private var location

    @State private var showSettings = false
    /// Which field holds the keyboard, if any. Both text fields exist at all
    /// times so focus always has somewhere to land.
    @FocusState private var focusedField: RouteField?

    var body: some View {
        @Bindable var planner = planner

        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 10) {
                endpointFields
                Button {
                    showSettings = true
                } label: {
                    Image(systemName: "slider.horizontal.3")
                        .frame(width: 40, height: 40)
                        .background(Color(.secondarySystemBackground), in: Circle())
                }
                .accessibilityLabel("Settings")
            }
            .padding(.horizontal)
            .padding(.top, 12)

            if location.accessDenied, planner.origin.isCurrentLocation {
                permissionNotice
            }

            if focusedField != nil {
                results
            } else {
                quickToggles
            }
        }
        .padding(.bottom, 12)
        .background(.regularMaterial)
        .onChange(of: focusedField) { previous, current in
            if let current {
                planner.beginEditing(current)
            } else if let previous {
                planner.endEditing(previous)
            }
        }
        .sheet(isPresented: $showSettings) {
            SettingsView()
        }
    }

    // MARK: - A and B

    private var endpointFields: some View {
        @Bindable var planner = planner

        return HStack(spacing: 10) {
            // The connector rail, which is what makes the two rows read as one
            // journey rather than two unrelated search boxes.
            VStack(spacing: 3) {
                Circle().fill(Color.accentColor).frame(width: 8, height: 8)
                ForEach(0..<3, id: \.self) { _ in
                    Circle().fill(Color.secondary.opacity(0.4)).frame(width: 2.5, height: 2.5)
                }
                Image(systemName: "mappin.circle.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(.red)
            }
            .padding(.top, 13)

            VStack(spacing: 6) {
                endpointRow(
                    field: .origin,
                    placeholder: "Choose starting point",
                    text: $planner.originText,
                    icon: planner.origin.symbolName
                )
                endpointRow(
                    field: .destination,
                    placeholder: "Where to?",
                    text: $planner.destinationText,
                    icon: planner.destination?.symbolName ?? "magnifyingglass"
                )
            }

            Button {
                focusedField = nil
                planner.swapEndpoints()
            } label: {
                Image(systemName: "arrow.up.arrow.down")
                    .font(.subheadline)
                    .frame(width: 32, height: 32)
                    .background(Color(.tertiarySystemFill), in: Circle())
            }
            .disabled(planner.destination == nil)
            .accessibilityLabel("Swap start and destination")
        }
    }

    /// One endpoint field.
    ///
    /// The text field is always present — swapping a `TextField` in only while
    /// focused deadlocks, because focus cannot be granted to a view that does
    /// not exist yet.
    private func endpointRow(
        field: RouteField,
        placeholder: String,
        text: Binding<String>,
        icon: String
    ) -> some View {
        let isFocused = focusedField == field

        return HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.caption)
                .frame(width: 14)
                .foregroundStyle(
                    field == .origin && planner.origin.isCurrentLocation
                        ? Color.accentColor
                        : .secondary
                )

            TextField(placeholder, text: text)
                .focused($focusedField, equals: field)
                .submitLabel(.search)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.words)

            if isFocused, !text.wrappedValue.isEmpty {
                Button {
                    planner.clearEditingText()
                } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary)
                }
                .accessibilityLabel("Clear")
            }
        }
        .font(.subheadline)
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(isFocused ? Color.accentColor : .clear, lineWidth: 1.5)
        )
    }

    // MARK: - Results

    private var results: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                // Always offered, so "current location" is reachable for the
                // destination too and not just as the origin's default.
                resultRow(
                    icon: "location.fill",
                    title: "Current location",
                    subtitle: "Use where I am now",
                    tint: .accentColor
                ) {
                    let field = planner.editingField
                    focusedField = nil
                    Task { await planner.useCurrentLocation(for: field) }
                }

                if currentText.isEmpty {
                    savedPlaces
                } else {
                    searchResults
                }
            }
        }
        .frame(maxHeight: 300)
        // Without this, a drag that begins on the list dismisses the keyboard
        // and the row under the finger disappears before the tap lands.
        .scrollDismissesKeyboard(.never)
    }

    /// Starred places and history, shown before anything is typed.
    @ViewBuilder
    private var savedPlaces: some View {
        let suggestions = planner.suggestions

        if suggestions.isEmpty {
            Text("Places you search for will appear here.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding()
        } else {
            let starred = suggestions.filter(\.isStarred)
            let recents = suggestions.filter { !$0.isStarred }

            if !starred.isEmpty {
                sectionHeader("Starred")
                ForEach(starred) { saved in
                    placeRow(saved.place, isStarred: true)
                }
            }
            if !recents.isEmpty {
                sectionHeader("Recent")
                ForEach(recents) { saved in
                    placeRow(saved.place, isStarred: false)
                }
            }
        }
    }

    @ViewBuilder
    private var searchResults: some View {
        ForEach(planner.searchResults) { result in
            placeRow(result, isStarred: planner.places.isStarred(result))
        }

        if let searchError = planner.searchError {
            Label(searchError, systemImage: "exclamationmark.triangle")
                .font(.caption)
                .foregroundStyle(.orange)
                .padding()
        } else if planner.isSearching {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Searching…").font(.caption).foregroundStyle(.secondary)
            }
            .padding()
        } else if planner.searchResults.isEmpty, currentText.count >= 2 {
            Text("No places found for “\(currentText)”")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding()
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.secondary)
            .textCase(.uppercase)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal)
            .padding(.top, 10)
            .padding(.bottom, 4)
    }

    private var currentText: String {
        switch planner.editingField {
        case .origin: planner.originText
        case .destination: planner.destinationText
        }
    }

    /// A place, with its star toggle.
    ///
    /// The star is a separate button rather than a swipe action so it is
    /// discoverable — a swipe on a row in a search dropdown is not something
    /// anyone goes looking for.
    private func placeRow(_ result: GeocodeResult, isStarred: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Button {
                    focusedField = nil
                    Task { await planner.select(result) }
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: isStarred ? "star.fill" : "mappin.circle.fill")
                            .font(.title3)
                            .foregroundStyle(isStarred ? Color.yellow : .secondary)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(result.name)
                                .font(.subheadline.weight(.medium))
                                .lineLimit(1)
                            if !result.address.isEmpty {
                                Text(result.address)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)

                Button {
                    planner.toggleStar(result)
                } label: {
                    Image(systemName: isStarred ? "star.fill" : "star")
                        .font(.subheadline)
                        // Both branches must be the same type, and there is no
                        // `Color.tertiary` — `.tertiary` is a hierarchical
                        // style, usable only where the type is not pinned.
                        .foregroundStyle(isStarred ? Color.yellow : Color.secondary.opacity(0.45))
                        .frame(width: 36, height: 36)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(isStarred ? "Unstar \(result.name)" : "Star \(result.name)")
            }
            .padding(.vertical, 6)
            .padding(.horizontal)

            Divider().padding(.leading, 48)
        }
    }

    private func resultRow(
        icon: String, title: String, subtitle: String, tint: Color,
        action: @escaping () -> Void
    ) -> some View {
        VStack(spacing: 0) {
            Button(action: action) {
                HStack(spacing: 12) {
                    Image(systemName: icon)
                        .font(.title3)
                        .foregroundStyle(tint)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(title)
                            .font(.subheadline.weight(.medium))
                            .lineLimit(1)
                        if !subtitle.isEmpty {
                            Text(subtitle)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                    Spacer()
                }
                .contentShape(Rectangle())
                .padding(.vertical, 10)
                .padding(.horizontal)
            }
            .buttonStyle(.plain)
            Divider().padding(.leading, 48)
        }
    }

    // MARK: - Toggles

    private var quickToggles: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                toggleChip(
                    "Transit", systemImage: "tram.fill", isOn: settings.includeTransit
                ) {
                    settings.includeTransit.toggle()
                    Task { await planner.refreshRoutes() }
                }
                toggleChip(
                    "Avoid Flock Cameras", systemImage: "camera.fill",
                    isOn: settings.avoidCameras
                ) {
                    settings.avoidCameras.toggle()
                    Task { await planner.refreshRoutes() }
                }
                toggleChip(
                    "Show Flock Cameras", systemImage: "eye.fill",
                    isOn: settings.showCameraOverlay
                ) {
                    settings.showCameraOverlay.toggle()
                    planner.invalidateOverlays()
                }
                toggleChip(
                    "Crime grid", systemImage: "hexagon.fill",
                    isOn: settings.showCrimeGrid
                ) {
                    settings.showCrimeGrid.toggle()
                    planner.invalidateOverlays()
                }
            }
            .padding(.horizontal)
            .padding(.top, 10)
        }
    }

    private func toggleChip(
        _ title: String, systemImage: String, isOn: Bool, action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .font(.caption.weight(.medium))
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(
                    isOn ? Color.accentColor : Color(.secondarySystemBackground),
                    in: Capsule()
                )
                .foregroundStyle(isOn ? .white : .primary)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isOn ? [.isSelected] : [])
    }

    private var permissionNotice: some View {
        Button {
            if let url = URL(string: UIApplication.openSettingsURLString) {
                UIApplication.shared.open(url)
            }
        } label: {
            Label(
                "Location is off. Turn it on, or pick a starting point above.",
                systemImage: "location.slash"
            )
            .font(.caption)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Color.orange.opacity(0.18), in: RoundedRectangle(cornerRadius: 12))
            .padding(.horizontal)
            .padding(.top, 10)
        }
        .buttonStyle(.plain)
    }
}

struct SettingsView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(PlannerViewModel.self) private var planner
    @Environment(PlaceStore.self) private var places
    @Environment(\.dismiss) private var dismiss
    @State private var meta: ServerMeta?
    @State private var confirmClearHistory = false
    @State private var health: ServerHealth?
    @State private var healthError: String?
    @State private var isTesting = false

    var body: some View {
        @Bindable var settings = settings

        NavigationStack {
            Form {
                Section {
                    Picker("Score journey for", selection: $settings.timeOfDay) {
                        ForEach(TimeOfDay.allCases) { option in
                            Label(option.label, systemImage: option.symbolName)
                                .tag(option)
                        }
                    }
                } header: {
                    Text("Time of day")
                } footer: {
                    Text(
                        "Auto follows the sun where you are. Street lighting "
                            + "only counts towards a score at night, so force "
                            + "Night to plan a walk home this evening."
                    )
                }

                Section("Routing") {
                    Toggle("Include public transport", isOn: $settings.includeTransit)
                    Toggle("Avoid Flock Cameras", isOn: $settings.avoidCameras)
                    Toggle("Voice guidance", isOn: $settings.voiceGuidance)
                }

                Section {
                    Toggle("Show Flock Cameras", isOn: $settings.showCameraOverlay)
                    Toggle("Show crime grid", isOn: $settings.showCrimeGrid)
                    if settings.showCrimeGrid {
                        Toggle("Night incidents only", isOn: $settings.crimeGridNightOnly)
                    }
                } header: {
                    Text("Map overlays")
                } footer: {
                    Text(
                        "The crime grid bins reported incidents into hexagons. "
                            + "Tap one to see what was reported there."
                    )
                }
                .onChange(of: settings.showCrimeGrid) { _, _ in planner.invalidateOverlays() }
                .onChange(of: settings.crimeGridNightOnly) { _, _ in planner.invalidateOverlays() }
                .onChange(of: settings.showCameraOverlay) { _, _ in planner.invalidateOverlays() }

                Section {
                    TextField("Server URL", text: $settings.serverURLString)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                    Button {
                        Task { await testConnection() }
                    } label: {
                        HStack {
                            Text("Test connection")
                            Spacer()
                            if isTesting {
                                ProgressView().controlSize(.small)
                            }
                        }
                    }
                    .disabled(isTesting)

                    if let health {
                        connectionResult(health)
                    } else if let healthError {
                        Label(healthError, systemImage: "xmark.circle.fill")
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                } header: {
                    Text("Server")
                } footer: {
                    Text(
                        "On a physical device, localhost is the phone itself — "
                            + "use your Mac's IP address on the same Wi-Fi, e.g. "
                            + "http://192.168.1.42:8000. Find it with "
                            + "`ipconfig getifaddr en0`."
                    )
                }

                Section {
                    HStack {
                        Text("Starred places")
                        Spacer()
                        Text("\(places.starred.count)")
                            .foregroundStyle(.secondary).monospacedDigit()
                    }
                    HStack {
                        Text("Recent searches")
                        Spacer()
                        Text("\(places.saved.count - places.starred.count)")
                            .foregroundStyle(.secondary).monospacedDigit()
                    }
                    Button("Clear search history", role: .destructive) {
                        confirmClearHistory = true
                    }
                    .disabled(places.saved.count == places.starred.count)
                } header: {
                    Text("Places")
                } footer: {
                    Text(
                        "Saved on this device only — where you go regularly "
                            + "never leaves your phone. Starred places are kept "
                            + "when you clear history."
                    )
                }

                if let meta {
                    Section("Loaded data") {
                        row("Street segments", meta.segments.formatted())
                        row("Streetlights", meta.streetlights.formatted())
                        row(
                            "Crime incidents",
                            "\(meta.crimeIncidents.formatted()) over \(meta.crimeHistoryYears) yr"
                        )
                        row("Flock cameras", meta.cameras.formatted())
                        row("Transit stops", meta.transitStops.formatted())
                        row("Searchable places", meta.places.formatted())
                    }
                }

                Section {
                    Text(
                        """
                        Safety scores combine DDOT streetlight locations with MPD \
                        incident reports. They are an estimate from historical data, \
                        not a prediction. Camera locations are crowdsourced from \
                        OpenStreetMap and are incomplete.

                        Use your own judgement.
                        """
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .task {
                let client = RoutingClient(baseURL: settings.serverURL)
                meta = try? await client.meta()
            }
            .confirmationDialog(
                "Clear search history?",
                isPresented: $confirmClearHistory,
                titleVisibility: .visible
            ) {
                Button("Clear history", role: .destructive) {
                    places.clearHistory()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Starred places are kept.")
            }
        }
    }

    /// What the server reported, so a half-built backend is visible here
    /// rather than showing up later as features quietly doing nothing.
    @ViewBuilder
    private func connectionResult(_ health: ServerHealth) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(
                health.isReady ? "Connected" : "Connected, but no graph loaded",
                systemImage: health.isReady
                    ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
            )
            .font(.caption)
            .foregroundStyle(health.isReady ? .green : .orange)

            if health.isReady {
                Text(
                    [
                        "\(health.segments ?? 0) segments",
                        "\(health.streetlights ?? 0) lights",
                        "\(health.searchablePlaces ?? 0) places",
                        (health.transit ?? false) ? "transit on" : "no transit",
                    ].joined(separator: " · ")
                )
                .font(.caption2)
                .foregroundStyle(.secondary)

                if (health.searchablePlaces ?? 0) == 0 {
                    Text("No search index — rebuild the graph to enable search.")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
            }
        }
    }

    private func testConnection() async {
        isTesting = true
        health = nil
        healthError = nil
        defer { isTesting = false }

        let client = RoutingClient(baseURL: settings.serverURL)
        do {
            health = try await client.health()
        } catch {
            healthError = (error as? RoutingError)?.errorDescription
                ?? error.localizedDescription
        }
    }

    private func row(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title)
            Spacer()
            Text(value).foregroundStyle(.secondary).monospacedDigit()
        }
    }
}
