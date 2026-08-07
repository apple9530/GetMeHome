import MapKit
import SwiftUI

/// Choosing which city to route in.
///
/// Shown once on first launch and reachable from Settings afterwards. It is a
/// full screen rather than a picker buried in a form because the choice
/// changes everything downstream — the map, the search index, the crime data,
/// the transit network — and because the server loads a city's graph on first
/// use, so the moment someone picks is the right moment to start that.
struct CityPickerView: View {
    /// Nil while choosing for the first time; set when changing later, so the
    /// sheet can be dismissed without picking.
    var current: String?
    var onSelect: (CityInfo) -> Void
    var onCancel: (() -> Void)?

    @Environment(AppSettings.self) private var settings
    @Environment(\.routingClient) private var client

    @State private var cities: [CityInfo] = []
    @State private var error: String?
    @State private var isLoading = true

    var body: some View {
        NavigationStack {
            Group {
                if isLoading {
                    ProgressView("Finding cities…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if cities.isEmpty {
                    unreachable
                } else {
                    list
                }
            }
            .navigationTitle(current == nil ? "Where are you?" : "Change city")
            .navigationBarTitleDisplayMode(.large)
            .toolbar {
                if let onCancel {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel", action: onCancel)
                    }
                }
            }
        }
        .task { await load() }
    }

    // MARK: - The list

    private var list: some View {
        List {
            Section {
                ForEach(cities) { city in
                    cityRow(city)
                }
            } footer: {
                Text(
                    "You can change this later in Settings. Safety scores, "
                        + "search and transit are all specific to the city "
                        + "you pick."
                )
            }

            if cities.contains(where: { !$0.available }) {
                Section {
                    Label(
                        "A greyed-out city is configured on the server but its "
                            + "map has not been built yet.",
                        systemImage: "info.circle"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func cityRow(_ city: CityInfo) -> some View {
        Button {
            guard city.available else { return }
            onSelect(city)
        } label: {
            HStack(spacing: 14) {
                Image(systemName: city.symbolName)
                    .font(.title2)
                    .frame(width: 36)
                    .foregroundStyle(city.available ? Color.accentColor : .secondary)

                VStack(alignment: .leading, spacing: 2) {
                    Text(city.name)
                        .font(.headline)
                    Text(city.region)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if !city.available {
                        Text("Not built on this server yet")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                    } else if !city.loaded {
                        // Honest about the wait rather than letting the first
                        // route look like a hang.
                        Text("First use takes a few seconds to load")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }

                Spacer()

                if city.slug == current {
                    Image(systemName: "checkmark")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Color.accentColor)
                }
            }
            .padding(.vertical, 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!city.available)
    }

    // MARK: - When the server cannot be reached

    /// The one screen where an unreachable server is fatal rather than
    /// degraded — there is nothing to choose from — so it offers the server
    /// address inline instead of sending the user to a Settings screen they
    /// cannot reach yet.
    private var unreachable: some View {
        @Bindable var settings = settings

        return ScrollView {
            VStack(spacing: 16) {
                Image(systemName: "antenna.radiowaves.left.and.right.slash")
                    .font(.largeTitle)
                    .foregroundStyle(.secondary)
                Text("Can't reach the routing server")
                    .font(.headline)
                if let error {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }

                TextField("Server URL", text: $settings.serverURLString)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)

                Button("Try again") {
                    Task { await load() }
                }
                .buttonStyle(.borderedProminent)

                Text(
                    "On a device, localhost is the phone itself — use your "
                        + "Mac's IP address on the same Wi-Fi."
                )
                .font(.caption2)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            }
            .padding(28)
        }
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }

        do {
            cities = try await client.cities().cities
            error = nil
        } catch {
            cities = []
            self.error = (error as? RoutingError)?.errorDescription
                ?? error.localizedDescription
        }
    }
}
