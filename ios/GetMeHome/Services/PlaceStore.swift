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

    /// Which city's places are loaded.
    ///
    /// History and starred places are per city. They used to share one list,
    /// which meant tapping a search field in New York offered a screen of
    /// Washington addresses — every one of them unroutable there, and the
    /// first thing anyone would see.
    private(set) var city: String?

    /// The current city's bounds, `[minLat, minLon, maxLat, maxLon]`.
    ///
    /// A second, independent guard on top of the per-city storage key. The key
    /// is the mechanism; this is the check that the mechanism worked. It
    /// matters because the key alone trusts history: anything saved before
    /// cities existed, or saved while no city had been chosen yet, landed in a
    /// shared list, and a store that only keys by city will happily hand that
    /// mixture back. Filtering on position cannot be fooled by any of that —
    /// a Washington coordinate is not in New York no matter which file it was
    /// read from.
    private(set) var bounds: [Double] = []

    private var key: String {
        city.map { "savedPlaces.\($0)" } ?? "savedPlaces"
    }

    /// The pre-city key, migrated once into whichever city owned it.
    private static let legacyKey = "savedPlaces"

    init(defaults: UserDefaults = .standard, city: String? = nil, bounds: [Double] = []) {
        self.defaults = defaults
        self.city = city
        self.bounds = bounds
        load()
    }

    /// Point the store at a different city's list.
    ///
    /// The previous city's entries stay on disk under their own key, so
    /// switching back and forth does not lose anything.
    func switchTo(city newCity: String?, bounds newBounds: [Double] = []) {
        let cityChanged = newCity != city
        let boundsChanged = newBounds != bounds
        guard cityChanged || boundsChanged else { return }

        city = newCity
        bounds = newBounds

        if cityChanged {
            load()
        } else {
            // Bounds arriving for a city already loaded — the backfill on the
            // first launch after upgrading. The legacy list could not be split
            // without them, so that is retried now rather than left until the
            // next city switch.
            migrateLegacy()
        }
    }

    /// Whether a saved place belongs to the city currently loaded.
    ///
    /// With no bounds known — the first launch after upgrading, before the
    /// city picker has been reopened — everything passes. Hiding someone's
    /// starred places because a cache entry is missing would be a worse
    /// failure than showing one that is out of area.
    private func inCurrentCity(_ place: GeocodeResult) -> Bool {
        guard bounds.count == 4 else { return true }
        return place.lat >= bounds[0] && place.lat <= bounds[2]
            && place.lon >= bounds[1] && place.lon <= bounds[3]
    }

    /// Starred first, then most recently used. This is the order the picker
    /// shows, and it is the whole point of starring: the handful of places
    /// someone actually goes should never scroll away behind a week of
    /// one-off searches.
    /// Everything saved that is actually in the current city. This, not
    /// `saved`, is what the interface should count and show.
    var visible: [SavedPlace] { saved.filter { inCurrentCity($0.place) } }

    var suggestions: [SavedPlace] {
        let here = visible
        let starred = here.filter(\.isStarred).sorted {
            $0.place.name.localizedCaseInsensitiveCompare($1.place.name) == .orderedAscending
        }
        let recents = here.filter { !$0.isStarred }.sorted { $0.lastUsed > $1.lastUsed }
        return starred + recents
    }

    var starred: [SavedPlace] { visible.filter(\.isStarred) }

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
        saved = decode(forKey: key) ?? []
        migrateLegacy()
    }

    /// Fold the pre-city list into whichever city each of its entries is in.
    ///
    /// The first version of this assumed the legacy list was all Washington,
    /// because cities did not exist when it was written to. That was only true
    /// for someone who never used the app in the window where New York had
    /// been added but the per-city keys had not — and for anyone who had, it
    /// copied a mixed list wholesale under Washington's key, which is one of
    /// the ways the two cities' addresses ended up interleaved.
    ///
    /// So it keeps only the entries that fall inside the city being loaded,
    /// and leaves the legacy blob in place for the other cities to take their
    /// own share when they are next opened. It is deleted once nothing is left
    /// that any city would claim.
    private func migrateLegacy() {
        guard bounds.count == 4, city != nil,
              let legacy = decode(forKey: Self.legacyKey), !legacy.isEmpty
        else { return }

        let mine = legacy.filter { inCurrentCity($0.place) }
        let theirs = legacy.filter { !inCurrentCity($0.place) }

        if !mine.isEmpty {
            // Merged rather than assigned: this city may already have its own
            // list, and the migration must not overwrite it.
            let known = Set(saved.map(\.id))
            saved.append(contentsOf: mine.filter { !known.contains($0.id) })
            persist()
        }

        if theirs.isEmpty {
            defaults.removeObject(forKey: Self.legacyKey)
        } else if !mine.isEmpty, let data = try? JSONEncoder().encode(theirs) {
            defaults.set(data, forKey: Self.legacyKey)
        }
    }

    private func decode(forKey key: String) -> [SavedPlace]? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode([SavedPlace].self, from: data)
    }
}
