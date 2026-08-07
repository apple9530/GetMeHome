import CoreLocation
import Foundation
import MapKit
import Observation

/// The crime grid, downloaded once and drawn with no server.
///
/// The pack holds finished hexagons rather than incidents — see the server's
/// `crime_pack.py` for why — so this does no scoring, no binning and no
/// severity weighting. It picks the level matching the viewport, filters to
/// what is on screen, and derives each hexagon's corners from its centre.
///
/// Only consulted while the server is unreachable. Downloaded data is a
/// fallback, not a cache: it goes stale as incidents are reported, and quietly
/// preferring it over a live server would mean showing month-old data to
/// someone with a perfectly good connection.
@Observable
@MainActor
final class OfflineCrimeStore {
    enum Status: Equatable {
        case absent
        case downloading(progress: Double)
        case ready(cells: Int, downloaded: Date, finestRadius: Double)
        case failed(String)
    }

    private(set) var status: [String: Status] = [:]

    private var packs: [String: CrimePack] = [:]

    func status(for city: String) -> Status {
        status[city] ?? .absent
    }

    func isReady(for city: String) -> Bool {
        if case .ready = status(for: city) { return true }
        return false
    }

    // MARK: - Where it lives

    /// Application Support, not Caches.
    ///
    /// iOS purges Caches under storage pressure without asking, and the whole
    /// point of this file is to still be there when there is no way to fetch
    /// it again. It is excluded from iCloud backup because it is large and
    /// entirely reproducible.
    private static func directory() throws -> URL {
        let base = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let dir = base.appendingPathComponent("CrimePacks", isDirectory: true)
        if !FileManager.default.fileExists(atPath: dir.path) {
            try FileManager.default.createDirectory(
                at: dir, withIntermediateDirectories: true
            )
            var mutable = dir
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try? mutable.setResourceValues(values)
        }
        return dir
    }

    private static func fileURL(for city: String) throws -> URL {
        try directory().appendingPathComponent("\(city).json")
    }

    // MARK: - Loading what is already here

    /// Called at launch and after a city switch. Cheap when nothing is stored.
    func loadIfPresent(city: String) {
        guard packs[city] == nil else { return }
        guard let url = try? Self.fileURL(for: city),
              FileManager.default.fileExists(atPath: url.path) else {
            status[city] = .absent
            return
        }

        do {
            let data = try Data(contentsOf: url)
            let pack = try JSONDecoder().decode(CrimePack.self, from: data)
            let modified = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate ?? Date()
            packs[city] = pack
            status[city] = .ready(
                cells: pack.totalCells,
                downloaded: modified,
                finestRadius: pack.finestRadius
            )
        } catch {
            // A truncated or superseded file is not worth keeping around; the
            // next download replaces it.
            try? FileManager.default.removeItem(at: url)
            status[city] = .absent
        }
    }

    // MARK: - Downloading

    func download(city: String, from baseURL: URL) async {
        if case .downloading = status(for: city) { return }
        status[city] = .downloading(progress: 0)

        var components = URLComponents(
            url: baseURL.appendingPathComponent("/crime/pack"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [URLQueryItem(name: "city", value: city)]
        guard let url = components?.url else {
            status[city] = .failed("The server address isn't valid.")
            return
        }

        do {
            // Straight to a file rather than into memory: a pack for a large
            // city is megabytes, and holding it as Data while also decoding it
            // doubles that for no reason.
            let (temporary, response) = try await URLSession.shared.download(from: url)
            if let http = response as? HTTPURLResponse,
               !(200..<300).contains(http.statusCode) {
                status[city] = .failed(
                    http.statusCode == 503
                        ? "That city hasn't been built on the server yet."
                        : "The server returned an error (\(http.statusCode))."
                )
                return
            }

            let data = try Data(contentsOf: temporary)
            let pack = try JSONDecoder().decode(CrimePack.self, from: data)

            let destination = try Self.fileURL(for: city)
            // Replace atomically, so an interrupted write cannot leave a
            // half-file where a working one used to be.
            try data.write(to: destination, options: .atomic)

            packs[city] = pack
            status[city] = .ready(
                cells: pack.totalCells,
                downloaded: Date(),
                finestRadius: pack.finestRadius
            )
        } catch {
            status[city] = .failed(
                (error as? URLError)?.localizedDescription
                    ?? error.localizedDescription
            )
        }
    }

    func remove(city: String) {
        packs[city] = nil
        status[city] = .absent
        if let url = try? Self.fileURL(for: city) {
            try? FileManager.default.removeItem(at: url)
        }
    }

    // MARK: - Serving the overlay

    /// Cells for a viewport, matching the online response as closely as the
    /// baked levels allow.
    ///
    /// Returns nil when there is nothing stored for this city, so the caller
    /// can tell "no offline data" from "offline data with nothing in view".
    func cells(
        city: String,
        in bounds: MapBounds,
        windowDays: Int
    ) -> (cells: [CrimeCell], radius: Double)? {
        guard let pack = packs[city] else { return nil }
        guard let level = pack.level(for: bounds, windowDays: windowDays) else {
            return ([], 0)
        }

        // Pad by a cell so hexagons straddling the edge are still drawn, the
        // same as the server does.
        let padLat = level.radius / 111_320.0
        let padLon = padLat / max(0.1, cos((bounds.minLat + bounds.maxLat) / 2 * .pi / 180))

        let visible = level.cells.filter {
            $0.lat >= bounds.minLat - padLat && $0.lat <= bounds.maxLat + padLat
                && $0.lon >= bounds.minLon - padLon && $0.lon <= bounds.maxLon + padLon
        }

        let projection = pack.projection
        return (
            visible.map { $0.asCrimeCell(radius: level.radius, projection: projection) },
            level.radius
        )
    }
}

// MARK: - The pack on disk

/// Mirrors the server's `/crime/pack`. Short keys, because at tens of
/// thousands of cells the key names are a meaningful share of the file.
struct CrimePack: Codable {
    struct Projection: Codable, Equatable {
        let originLat: Double
        let originLon: Double

        /// Metres per degree of latitude, and of longitude at this origin.
        /// The same equirectangular frame the server binned in — the corners
        /// would not tile if this disagreed.
        var metresPerDegreeLat: Double { 6_371_008.8 * .pi / 180 }
        var metresPerDegreeLon: Double {
            metresPerDegreeLat * cos(originLat * .pi / 180)
        }
    }

    struct PackedCell: Codable {
        let a: Double  // latitude
        let o: Double  // longitude
        let t: Int     // total incidents
        let i: Double  // intensity
        let n: Double  // night share
        let s: Int     // serious count
        let d: String  // latest date
        let b: [PackedOffense]

        var lat: Double { a }
        var lon: Double { o }

        func asCrimeCell(radius: Double, projection: Projection) -> CrimeCell {
            CrimeCell(
                id: "offline:\(radius):\(String(format: "%.5f,%.5f", a, o))",
                centerLat: a,
                centerLon: o,
                vertices: Self.vertices(lat: a, lon: o, radius: radius, projection: projection),
                total: t,
                intensity: i,
                nightShare: n,
                seriousCount: s,
                byOffense: b.map {
                    OffenseCount(
                        offense: $0.n,
                        displayName: $0.n,
                        count: $0.c,
                        category: $0.g,
                        share: $0.s
                    )
                },
                latest: d
            )
        }

        /// The six corners of a pointy-top hexagon, flat [lat, lon, ...].
        ///
        /// Derived here rather than shipped: twelve floats per cell is several
        /// times the rest of the record, and this is the same arithmetic the
        /// server would have done, moved to where it costs nothing.
        static func vertices(
            lat: Double, lon: Double, radius: Double, projection: Projection
        ) -> [Double] {
            var out: [Double] = []
            out.reserveCapacity(12)
            for i in 0..<6 {
                let angle = Double.pi / 180 * (60 * Double(i) - 30)
                let dx = radius * cos(angle)
                let dy = radius * sin(angle)
                out.append(lat + dy / projection.metresPerDegreeLat)
                out.append(lon + dx / projection.metresPerDegreeLon)
            }
            return out
        }
    }

    struct PackedOffense: Codable {
        let n: String  // display name
        let c: Int     // count
        let g: String  // category
        let s: Double  // share
    }

    struct Level: Codable {
        let windowDays: Int
        let radius: Double
        let cells: [PackedCell]
    }

    let city: String
    let cityName: String
    let generatedAt: String
    let projection: Projection
    let windows: [Int]
    let radii: [Double]
    let finestRadius: Double
    let nightOnlySupported: Bool
    let totalCells: Int
    let levels: [Level]

    /// The baked level closest to what the server would have chosen.
    ///
    /// Rounds *up* when the ideal radius is finer than anything baked — a
    /// coarser grid is a truthful lower-resolution answer, where the finest
    /// baked level stretched over a small viewport would be a handful of
    /// enormous cells.
    func level(for bounds: MapBounds, windowDays: Int) -> Level? {
        let wanted = Self.idealRadius(for: bounds)
        let window = nearestWindow(to: windowDays)

        let candidates = levels.filter { $0.windowDays == window }
        guard !candidates.isEmpty else { return nil }

        return candidates.first { $0.radius >= wanted }
            ?? candidates.max { $0.radius < $1.radius }
    }

    func nearestWindow(to days: Int) -> Int {
        windows.min { abs($0 - days) < abs($1 - days) } ?? days
    }

    /// The same rule the server uses: the smallest radius keeping the viewport
    /// under the cell cap.
    private static func idealRadius(for bounds: MapBounds) -> Double {
        let metresPerDegLat = 111_320.0
        let midLat = (bounds.minLat + bounds.maxLat) / 2
        let height = (bounds.maxLat - bounds.minLat) * metresPerDegLat
        let width = (bounds.maxLon - bounds.minLon) * metresPerDegLat
            * cos(midLat * .pi / 180)
        let area = max(1, width * height)

        // MAX_CELLS in the server's hexgrid.
        let maxCells = 180.0
        for radius in [165.0, 250.0, 375.0, 560.0, 850.0, 1300.0, 2000.0]
        where area / (2.598 * radius * radius) <= maxCells {
            return radius
        }
        return 2000
    }
}
