import CoreLocation
import Observation
import SwiftUI

/// Sharing a walk's live position and ETA with someone.
///
/// The design constraint that shapes everything here: a share must never
/// outlive the walk. Someone shares their location because they are worried,
/// and a link that is still broadcasting an hour after they got home is worse
/// than not offering the feature. So it ends four ways, and only one of them
/// depends on the user remembering:
///
/// * **Arrival** ends it automatically, which is the normal case.
/// * **Stopping navigation** ends it, including by backing out of the screen.
/// * **The server's own ceiling** ends it after a few hours regardless.
/// * **Silence** ends it — if this device stops reporting, the server marks the
///   share stale and then drops it, which covers a dead battery or a killed app.
///
/// The link that gets shared is watch-only. The write credential comes back
/// once at creation, is held in memory here, and never leaves the device.
@Observable
@MainActor
final class ShareETAService {
    enum State: Equatable {
        case off
        case starting
        case sharing(url: String)
        case failed(String)

        var url: String? {
            if case let .sharing(url) = self { return url }
            return nil
        }

        var isActive: Bool {
            if case .sharing = self { return true }
            return false
        }
    }

    private(set) var state: State = .off
    /// Set when the last position push failed, so the UI can say the friend
    /// may be seeing a stale dot rather than silently pretending it is live.
    private(set) var lastPushFailed = false

    /// How often the position is pushed. Matched to what a recipient can
    /// actually perceive — a walker covers about twenty metres in fifteen
    /// seconds — rather than to how often fixes arrive.
    private static let pushInterval: TimeInterval = 15

    private let client: RoutingClient
    private var token: String?
    /// Never included in the shared URL, never persisted.
    private var ownerKey: String?
    private var lastPushAt: Date = .distantPast

    init(client: RoutingClient) {
        self.client = client
    }

    // MARK: - Lifecycle

    func start(destinationName: String) async {
        guard case .off = state else { return }
        state = .starting
        do {
            let created = try await client.startShare(destinationName: destinationName)
            token = created.token
            ownerKey = created.ownerKey
            lastPushAt = .distantPast
            lastPushFailed = false
            state = .sharing(url: created.url)
        } catch {
            state = .failed(
                (error as? RoutingError)?.errorDescription ?? error.localizedDescription
            )
        }
    }

    /// Push a position, at most once per `pushInterval`.
    ///
    /// `force` bypasses the interval for the first fix and for arrival, where
    /// the point is to be current rather than to be economical.
    func push(
        coordinate: CLLocationCoordinate2D,
        etaSeconds: Double?,
        remainingMetres: Double?,
        force: Bool = false
    ) async {
        guard state.isActive, let token, let ownerKey else { return }
        guard force || Date().timeIntervalSince(lastPushAt) >= Self.pushInterval else {
            return
        }
        lastPushAt = Date()

        do {
            _ = try await client.updateShare(
                token: token,
                ownerKey: ownerKey,
                coordinate: coordinate,
                etaSeconds: etaSeconds,
                remainingMetres: remainingMetres
            )
            lastPushFailed = false
        } catch {
            // Not fatal and not surfaced as an error: the walk continues, and
            // the server marks the share stale on its own if this keeps
            // failing. Recorded so the banner can say so.
            lastPushFailed = true
        }
    }

    /// End the share. `arrived` distinguishes "they got there" from "they
    /// stopped sharing", which is the difference the recipient cares about.
    func stop(arrived: Bool) async {
        guard let token, let ownerKey else {
            state = .off
            return
        }
        // Cleared first, so a failed request cannot leave the UI claiming to
        // still be sharing — and the server ends it on silence regardless.
        self.token = nil
        self.ownerKey = nil
        state = .off
        lastPushFailed = false

        try? await client.endShare(token: token, ownerKey: ownerKey, arrived: arrived)
    }

    func dismissError() {
        if case .failed = state { state = .off }
    }
}
