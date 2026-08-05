import SwiftUI
import UIKit

/// Destination search and the quick preference toggles.
struct SearchView: View {
    @Environment(PlannerViewModel.self) private var planner
    @Environment(AppSettings.self) private var settings
    @Environment(LocationService.self) private var location

    @State private var showSettings = false
    @FocusState private var searchFocused: Bool

    var body: some View {
        @Bindable var planner = planner
        @Bindable var settings = settings

        VStack(spacing: 0) {
            HStack(spacing: 10) {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(.secondary)
                    TextField("Where to?", text: $planner.searchText)
                        .focused($searchFocused)
                        .submitLabel(.search)
                        .autocorrectionDisabled()
                    if !planner.searchText.isEmpty {
                        Button {
                            planner.clearSearch()
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .foregroundStyle(.tertiary)
                        }
                        .accessibilityLabel("Clear search")
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(Color(.secondarySystemBackground), in: Capsule())

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

            if location.accessDenied {
                permissionNotice
            }

            if !planner.searchResults.isEmpty {
                results
            } else if planner.searchText.isEmpty {
                quickToggles
            }
        }
        .padding(.bottom, 12)
        .background(.regularMaterial)
        .sheet(isPresented: $showSettings) {
            SettingsView()
        }
    }

    private var results: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(planner.searchResults) { result in
                    Button {
                        searchFocused = false
                        Task { await planner.select(result) }
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: "mappin.circle.fill")
                                .font(.title3)
                                .foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(result.name)
                                    .font(.subheadline.weight(.medium))
                                    .lineLimit(1)
                                Text(result.address)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
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
        }
        .frame(maxHeight: 280)
    }

    private var quickToggles: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                toggleChip(
                    "Transit", systemImage: "tram.fill",
                    isOn: settings.includeTransit
                ) {
                    settings.includeTransit.toggle()
                    Task { await planner.refreshRoutes() }
                }
                toggleChip(
                    "Avoid plate readers", systemImage: "camera.fill",
                    isOn: settings.avoidCameras
                ) {
                    settings.avoidCameras.toggle()
                    Task { await planner.refreshRoutes() }
                }
                toggleChip(
                    "Show cameras", systemImage: "eye.fill",
                    isOn: settings.showCameraOverlay
                ) {
                    settings.showCameraOverlay.toggle()
                }
                toggleChip(
                    "Safety overlay", systemImage: "map.fill",
                    isOn: settings.showSafetyOverlay
                ) {
                    settings.showSafetyOverlay.toggle()
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
                "Location is off. Tap to enable it so routes can start from where you are.",
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
    @Environment(\.dismiss) private var dismiss
    @State private var meta: ServerMeta?

    var body: some View {
        @Bindable var settings = settings

        NavigationStack {
            Form {
                Section("Routing") {
                    Toggle("Include public transport", isOn: $settings.includeTransit)
                    Toggle("Avoid licence plate readers", isOn: $settings.avoidCameras)
                    Toggle("Voice guidance", isOn: $settings.voiceGuidance)
                }

                Section("Map overlays") {
                    Toggle("Show plate readers", isOn: $settings.showCameraOverlay)
                    Toggle("Colour streets by safety", isOn: $settings.showSafetyOverlay)
                }

                Section {
                    TextField("Server URL", text: $settings.serverURLString)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                } header: {
                    Text("Server")
                } footer: {
                    Text(
                        "On a physical device, localhost is the phone itself — "
                            + "use your Mac's IP address on the same network."
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
                        row("Plate readers", meta.cameras.formatted())
                        row("Transit stops", meta.transitStops.formatted())
                    }
                }

                Section {
                    Text(
                        """
                        Safety scores combine DDOT streetlight locations with MPD \
                        incident reports. They are an estimate from historical data, \
                        not a prediction. Plate reader locations are crowdsourced \
                        from OpenStreetMap and are incomplete.

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
