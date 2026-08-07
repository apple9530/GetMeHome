import Foundation
import Observation
import SwiftUI

/// Whether the routing server is reachable, and getting back to it when it is
/// not.
///
/// Deliberately about *the server* rather than about the network. A phone can
/// have four bars of LTE and still not reach a backend on someone's laptop, and
/// `NWPathMonitor` would call that online. What the app needs to know is
/// whether the next request will work, and the only honest way to know that is
/// to have made one.
///
/// So the state is driven by real requests: anything that fails as unreachable
/// marks it down, and a poll every fifteen seconds brings it back up. While it
/// is down the app shows a banner and the crime overlay falls back to
/// downloaded data.
@Observable
@MainActor
final class ConnectivityMonitor {
    enum State: Equatable {
        case unknown
        case online
        /// Carries the reason from the request that failed, so the banner can
        /// say *why* rather than only that.
        case offline(String)

        var isOffline: Bool {
            if case .offline = self { return true }
            return false
        }
    }

    private(set) var state: State = .unknown
    /// When the next retry fires, for the banner's countdown.
    private(set) var nextRetry: Date?
    private(set) var isRetrying = false

    /// Fifteen seconds, as asked for. Frequent enough that walking back into
    /// coverage is noticed quickly, sparse enough that it is not a meaningful
    /// battery cost while genuinely out of range.
    static let retryInterval: TimeInterval = 15

    var isOffline: Bool { state.isOffline }

    var reason: String? {
        if case let .offline(why) = state { return why }
        return nil
    }

    private var client: RoutingClient
    private var pollTask: Task<Void, Never>?

    init(client: RoutingClient) {
        self.client = client
    }

    func updateClient(_ client: RoutingClient) {
        self.client = client
    }

    // MARK: - Reporting from real requests

    /// Called by anything that talks to the server.
    ///
    /// Only `.unreachable` counts as offline. A 404 or a 503 means the server
    /// answered — the app is connected and something else is wrong, and
    /// telling someone they have no connection when they do would send them
    /// to check their Wi-Fi for nothing.
    func record(_ error: Error) {
        guard case let .unreachable(why)? = error as? RoutingError else { return }
        goOffline(why)
    }

    /// Called after any successful request.
    func recordSuccess() {
        guard state != .online else { return }
        state = .online
        nextRetry = nil
        isRetrying = false
        pollTask?.cancel()
        pollTask = nil
    }

    private func goOffline(_ why: String) {
        guard !state.isOffline else { return }
        state = .offline(why)
        startPolling()
    }

    // MARK: - Getting back

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                nextRetry = Date().addingTimeInterval(Self.retryInterval)
                try? await Task.sleep(for: .seconds(Self.retryInterval))
                guard !Task.isCancelled else { return }
                await probe()
                if !state.isOffline { return }
            }
        }
    }

    /// Ask the server whether it is there. Used by the poll and by the
    /// banner's manual retry.
    func probe() async {
        guard !isRetrying else { return }
        isRetrying = true
        defer { isRetrying = false }

        do {
            _ = try await client.health()
            recordSuccess()
        } catch let error as RoutingError {
            if case let .unreachable(why) = error {
                // Still down. Update the reason: it can change as the failure
                // does — a timeout is a different problem from a refused
                // connection, and the fix is different too.
                state = .offline(why)
            } else {
                // The server answered something, even if it was an error. It
                // is up.
                recordSuccess()
            }
        } catch {
            state = .offline(error.localizedDescription)
        }
    }

    /// Check once at launch so the first thing the app does is not a failed
    /// route request.
    func start() {
        Task { await probe() }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }
}
