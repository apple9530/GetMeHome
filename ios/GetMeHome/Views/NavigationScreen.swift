import CoreLocation
import MapKit
import SwiftUI
import UIKit

/// Active turn-by-turn guidance.
struct NavigationScreen: View {
    @Environment(LocationService.self) private var location
    @Environment(SpeechService.self) private var speech
    @Environment(AppSettings.self) private var settings

    let model: NavigationViewModel
    var onEnd: () -> Void

    @State private var cameraPosition: MapCameraPosition = .userLocation(
        followsHeading: true, fallback: .automatic
    )
    @State private var shareSheetURL: SharePayload?
    @State private var confirmStopSharing = false

    var body: some View {
        ZStack(alignment: .top) {
            map
                .ignoresSafeArea()

            VStack(spacing: 0) {
                maneuverCard
                if let note = model.activeSafetyNote {
                    safetyBanner(note)
                }
                if model.isRerouting {
                    statusBanner("Rerouting…", systemImage: "arrow.triangle.2.circlepath")
                }
                if let error = model.lastError {
                    statusBanner(error, systemImage: "wifi.exclamationmark", tint: .orange)
                }
                if model.share.state.isActive {
                    sharingBanner
                }
                if case let .failed(reason) = model.share.state {
                    statusBanner(
                        "Couldn't start sharing. \(reason)",
                        systemImage: "exclamationmark.triangle",
                        tint: .orange
                    )
                    .onTapGesture { model.share.dismissError() }
                }
                Spacer()
                bottomBar
            }
        }
        .onAppear {
            location.beginNavigation()
            speech.isEnabled = settings.voiceGuidance
            model.start()
        }
        .onDisappear {
            location.endNavigation()
            model.stop()
        }
        .onChange(of: location.location) { _, newValue in
            guard let newValue else { return }
            model.update(with: newValue)
        }
        .onChange(of: settings.voiceGuidance) { _, newValue in
            speech.isEnabled = newValue
            if !newValue { speech.stop() }
        }
        .sheet(item: $shareSheetURL) { payload in
            ActivityView(items: [payload.text])
        }
        .confirmationDialog(
            "Stop sharing your ETA?",
            isPresented: $confirmStopSharing,
            titleVisibility: .visible
        ) {
            Button("Stop sharing", role: .destructive) {
                Task { await model.stopSharing() }
            }
            Button("Keep sharing", role: .cancel) {}
        } message: {
            Text("The link will stop working straight away.")
        }
        .sheet(isPresented: .constant(model.hasArrived)) {
            ArrivalSheet(itinerary: model.itinerary, onDone: onEnd)
                .presentationDetents([.height(320)])
                .interactiveDismissDisabled()
        }
    }

    // MARK: - Map

    private var map: some View {
        Map(position: $cameraPosition) {
            UserAnnotation()

            MapPolyline(coordinates: model.itinerary.allCoordinates)
                .stroke(
                    Theme.routeLine,
                    style: StrokeStyle(lineWidth: 8, lineCap: .round, lineJoin: .round)
                )

            if let step = model.currentStep {
                Annotation("", coordinate: step.location.clLocation) {
                    Image(systemName: step.symbolName)
                        .font(.caption.bold())
                        .padding(6)
                        .background(.white, in: Circle())
                        .foregroundStyle(Color.accentColor)
                        .shadow(radius: 2)
                }
            }
        }
        .mapStyle(.standard(elevation: .flat, pointsOfInterest: .excludingAll))
    }

    // MARK: - Guidance

    private var maneuverCard: some View {
        HStack(spacing: 14) {
            Image(systemName: model.currentStep?.symbolName ?? "arrow.up")
                .font(.system(size: 34, weight: .semibold))
                .frame(width: 52)

            VStack(alignment: .leading, spacing: 2) {
                Text(Format.liveDistance(model.distanceToManeuver))
                    .font(.title2.bold().monospacedDigit())
                Text(model.currentStep?.instruction ?? "Continue")
                    .font(.subheadline)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
        .foregroundStyle(.white)
        .padding(16)
        .background(Color.accentColor.gradient)
        .clipShape(RoundedRectangle(cornerRadius: Theme.cardCorner))
        .padding(.horizontal, 12)
        .padding(.top, 4)
        .shadow(radius: 6, y: 2)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "In \(Format.liveDistance(model.distanceToManeuver)), "
                + (model.currentStep?.voice ?? "continue")
        )
    }

    private func safetyBanner(_ note: String) -> some View {
        Label(note, systemImage: "flashlight.off.fill")
            .font(.footnote.weight(.medium))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .background(Color(red: 0.85, green: 0.45, blue: 0.15))
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .padding(.horizontal, 12)
            .padding(.top, 6)
    }

    private func statusBanner(
        _ text: String, systemImage: String, tint: Color = .secondary
    ) -> some View {
        Label(text, systemImage: systemImage)
            .font(.footnote)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(.regularMaterial)
            .foregroundStyle(tint)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .padding(.horizontal, 12)
            .padding(.top, 6)
    }

    // MARK: - Sharing

    /// Share, then stop sharing. Deliberately the same button position both
    /// ways, so the way out is exactly where the way in was.
    private var shareButton: some View {
        Button {
            if let url = model.share.state.url {
                shareSheetURL = SharePayload(text: url)
            } else if case .starting = model.share.state {
                // Already in flight; a second tap would create a second share.
            } else {
                Task { await model.startSharing() }
            }
        } label: {
            Group {
                if case .starting = model.share.state {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: model.share.state.isActive
                        ? "person.2.wave.2.fill" : "square.and.arrow.up")
                        .font(.body)
                }
            }
            .frame(width: 42, height: 42)
            .background(
                model.share.state.isActive
                    ? Color.accentColor.opacity(0.22)
                    : Color(.tertiarySystemFill),
                in: Circle()
            )
        }
        .accessibilityLabel(
            model.share.state.isActive ? "Share the link again" : "Share ETA"
        )
    }

    /// Always visible while sharing.
    ///
    /// Someone broadcasting their live location should never have to remember
    /// that they are — the state has to be on screen, and stopping has to be
    /// one tap from wherever they are looking.
    private var sharingBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "dot.radiowaves.left.and.right")
                .font(.caption)
                .foregroundStyle(model.share.lastPushFailed ? .orange : .green)
            VStack(alignment: .leading, spacing: 1) {
                Text("Sharing your ETA")
                    .font(.caption.weight(.medium))
                Text(
                    model.share.lastPushFailed
                        ? "Can't reach the server — they may see an old position."
                        : "Stops automatically when you arrive."
                )
                .font(.caption2)
                .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Stop") { confirmStopSharing = true }
                .font(.caption.weight(.semibold))
                .buttonStyle(.borderless)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .padding(.horizontal, 12)
        .padding(.top, 6)
    }

    // MARK: - Bottom bar

    private var bottomBar: some View {
        VStack(spacing: 10) {
            if let next = model.nextStep, !model.hasArrived {
                HStack(spacing: 8) {
                    Image(systemName: "arrow.turn.up.right")
                        .font(.caption)
                    Text("Then \(next.instruction.lowercasedFirstCharacter())")
                        .font(.caption)
                        .lineLimit(1)
                    Spacer()
                }
                .foregroundStyle(.secondary)
                .padding(.horizontal, 4)
            }

            HStack {
                VStack(alignment: .leading, spacing: 1) {
                    Text(Format.duration(model.remainingDuration))
                        .font(.title3.bold().monospacedDigit())
                    Text(
                        "\(Format.distance(model.remainingDistance)) · arrive \(Format.clock(model.estimatedArrival))"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                Spacer()

                Button {
                    settings.voiceGuidance.toggle()
                } label: {
                    Image(
                        systemName: settings.voiceGuidance
                            ? "speaker.wave.2.fill" : "speaker.slash.fill"
                    )
                    .font(.body)
                    .frame(width: 42, height: 42)
                    .background(Color(.tertiarySystemFill), in: Circle())
                }
                .accessibilityLabel(settings.voiceGuidance ? "Mute guidance" : "Unmute guidance")

                shareButton

                Button(role: .destructive) {
                    onEnd()
                } label: {
                    Text("End")
                        .font(.subheadline.weight(.semibold))
                        .padding(.horizontal, 18)
                        .padding(.vertical, 11)
                        .background(Color(.tertiarySystemFill), in: Capsule())
                }
            }
        }
        .padding(14)
        .background(.regularMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 20))
        .padding(.horizontal, 12)
        .padding(.bottom, 8)
    }
}

struct ArrivalSheet: View {
    let itinerary: Itinerary
    var onDone: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 48))
                .foregroundStyle(.green)
                .padding(.top, 20)

            Text("You've arrived")
                .font(.title2.bold())

            HStack(spacing: 26) {
                stat(Format.duration(itinerary.duration), "Time")
                stat(Format.distance(itinerary.walkDistance), "Walked")
                stat("\(itinerary.safety.overall)", "Safety")
            }

            Spacer()

            Button("Done", action: onDone)
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .frame(maxWidth: .infinity)
        }
        .padding(20)
    }

    private func stat(_ value: String, _ label: String) -> some View {
        VStack(spacing: 2) {
            Text(value).font(.headline.monospacedDigit())
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
    }
}

private extension String {
    func lowercasedFirstCharacter() -> String {
        guard let first else { return self }
        return first.lowercased() + dropFirst()
    }
}

/// UIKit's share sheet.
///
/// `ShareLink` would be simpler, but it needs the URL to exist when the view
/// is built and here it does not exist until the share has been created on the
/// server. Presenting the system sheet on demand is the honest shape for that.
struct ActivityView: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}

/// A shareable string, wrapped so it can drive an `item:` sheet.
struct SharePayload: Identifiable {
    let text: String
    var id: String { text }
}
