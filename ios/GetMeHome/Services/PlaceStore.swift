import Foundation
import Observation

/// A place the user has been to before, or has starred.
struct SavedPlace: Codable, Hashable, Identifiable {
    let place: GeocodeResult
    var isStarred: Bool
    var lastUsed: Date
    var useCount: Int

    var id: String { place.id }
}

/// Remembers where the user has searched, and which places they starred.
///
/// Kept entirely on the device. Somewhere you go regularly is among the more
/// sensitive things an app can know about you, and there is no reason for
/// this list to leave the phone — the server never needs it to plan a route.
@Observable
@MainActor
final class PlaceStore {
    /// Beyond this, older entries are dropped. Starred places are never
    /// dropped regardless of age.
    private static let historyLimit = 60

    private(set) var saved: [SavedPlace] = []

    private let defaults: UserDefaults
    private let key = "savedPlaces"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        load()
    }

    /// Starred first, then most recently used. This is the order the picker
    /// shows, and it is the whole point of starring: the handful of places
    /// someone actually goes should never scroll away behind a week of
    /// one-off searches.
    var suggestions: [SavedPlace] {
        let starred = saved.filter(\.isStarred).sorted {
            $0.place.name.localizedCaseInsensitiveCompare($1.place.name) == .orderedAscending
        }
        let recents = saved.filter { !$0.isStarred }.sorted { $0.lastUsed > $1.lastUsed }
        return starred + recents
    }

    var starred: [SavedPlace] { saved.filter(\.isStarred) }

    func isStarred(_ place: GeocodeResult) -> Bool {
        entry(for: place)?.isStarred ?? false
    }

    /// Note that a place was used as an endpoint.
    func record(_ place: GeocodeResult) {
        if let i = index(of: place) {
            saved[i].lastUsed = Date()
            saved[i].useCount += 1
        } else {
            saved.append(
                SavedPlace(place: place, isStarred: false, lastUsed: Date(), useCount: 1)
            )
        }
        prune()
        persist()
    }

    func toggleStar(_ place: GeocodeResult) {
        if let i = index(of: place) {
            saved[i].isStarred.toggle()
        } else {
            // Starring something never visited is legitimate — it arrives
            // from a search result the user has not routed to yet.
            saved.append(
                SavedPlace(place: place, isStarred: true, lastUsed: Date(), useCount: 0)
            )
        }
        persist()
    }

    func remove(_ place: GeocodeResult) {
        saved.removeAll { $0.place.id == place.id }
        persist()
    }

    func clearHistory() {
        saved.removeAll { !$0.isStarred }
        persist()
    }

    // MARK: - Internals

    private func entry(for place: GeocodeResult) -> SavedPlace? {
        index(of: place).map { saved[$0] }
    }

    /// Matched on rounded coordinates as well as identity, because the same
    /// place can come back from the local index and the external geocoder
    /// with slightly different names.
    private func index(of place: GeocodeResult) -> Int? {
        if let exact = saved.firstIndex(where: { $0.place.id == place.id }) {
            return exact
        }
        return saved.firstIndex {
            abs($0.place.lat - place.lat) < 0.00015
                && abs($0.place.lon - place.lon) < 0.00015
        }
    }

    private func prune() {
        let unstarred = saved.filter { !$0.isStarred }.sorted { $0.lastUsed > $1.lastUsed }
        guard unstarred.count > Self.historyLimit else { return }
        let keep = Set(unstarred.prefix(Self.historyLimit).map(\.id))
        saved.removeAll { !$0.isStarred && !keep.contains($0.id) }
    }

    private func persist() {
        guard let data = try? JSONEncoder().encode(saved) else { return }
        defaults.set(data, forKey: key)
    }

    private func load() {
        guard let data = defaults.data(forKey: key),
              let decoded = try? JSONDecoder().decode([SavedPlace].self, from: data)
        else { return }
        saved = decoded
    }
}
