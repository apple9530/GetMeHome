import SwiftUI

/// Everything that can be drawn on the map, in one place.
///
/// These used to be a row of chips under the search fields. That row grew a
/// chip per feature, mixed map overlays in with routing preferences, and
/// scrolled sideways off the screen once there were more than three. A single
/// layers button — which is where every maps app puts this — keeps the search
/// panel to one job and gives each toggle room for a line explaining what it
/// actually shows.
struct MapLayersView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        @Bindable var settings = settings

        NavigationStack {
            Form {
                Section {
                    layerToggle(
                        "Transit stops",
                        systemImage: "tram.circle.fill",
                        tint: Theme.transitTint,
                        detail: "Every Metro and bus stop. Tap one for its "
                            + "timetable and to follow a vehicle live.",
                        isOn: $settings.showTransitStops
                    )
                    layerToggle(
                        "Crime grid",
                        systemImage: "hexagon.fill",
                        tint: .orange,
                        detail: "Reported incidents binned into hexagons. Tap "
                            + "one to see what was reported there.",
                        isOn: $settings.showCrimeGrid
                    )
                    if settings.showCrimeGrid {
                        Toggle("Night incidents only", isOn: $settings.crimeGridNightOnly)
                            .font(.subheadline)
                    }
                    layerToggle(
                        "Flock cameras",
                        systemImage: "camera.fill",
                        tint: Theme.cameraTint,
                        detail: "Automated licence-plate readers, with the "
                            + "direction they face where it is mapped.",
                        isOn: $settings.showCameraOverlay
                    )
                } header: {
                    Text("Map layers")
                } footer: {
                    Text(
                        "Camera locations are crowdsourced and incomplete — an "
                            + "empty stretch of map is not evidence there is "
                            + "nothing there."
                    )
                }

                Section {
                    Picker("Crime data from the last", selection: $settings.crimeWindow) {
                        ForEach(CrimeWindow.allCases) { window in
                            Text(window.label).tag(window)
                        }
                    }
                } header: {
                    Text("Crime data")
                } footer: {
                    Text(
                        "Applies to the grid above and to route safety scores "
                            + "together, so what you are looking at is always "
                            + "the data your route was scored against."
                    )
                }

                Section {
                    Toggle("Avoid Flock cameras", isOn: $settings.avoidCameras)
                } header: {
                    Text("Routing")
                } footer: {
                    Text(
                        "Routes around mapped plate readers where it can. This "
                            + "is a privacy preference, not a safety one — it "
                            + "does not affect the safety score."
                    )
                }
            }
            .navigationTitle("Layers")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    private func layerToggle(
        _ title: String,
        systemImage: String,
        tint: Color,
        detail: String,
        isOn: Binding<Bool>
    ) -> some View {
        Toggle(isOn: isOn) {
            HStack(spacing: 11) {
                Image(systemName: systemImage)
                    .font(.system(size: 15))
                    .foregroundStyle(tint)
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}
