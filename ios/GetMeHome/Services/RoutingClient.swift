import CoreLocation
import Foundation

enum RoutingError: LocalizedError {
    case badURL
    case server(status: Int, detail: String)
    case unreachable(String)
    case blockedByPolicy
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .badURL:
            "The server address isn't valid. Check it in Settings."
        case let .server(status, detail):
            // The backend puts a human-readable reason in `detail` for the
            // cases a user can act on — off-graph endpoints especially — so
            // surface it rather than a status code.
            detail.isEmpty ? "The server returned an error (\(status))." : detail
        case let .unreachable(reason):
            // Naming the reason matters: "can't connect" is the same message
            // whether the server is down, the address is wrong, or the phone
            // is on a different network, and those need different fixes.
            "Can't reach the routing server. \(reason)"
        case .blockedByPolicy:
            "iOS blocked the connection. Allow local network access for "
                + "GetMeHome in iOS Settings > Privacy & Security > Local Network."
        case let .decoding(message):
            "Unexpected response from the server. \(message)"
        }
    }
}

/// Talks to the GetMeHome backend.
actor RoutingClient {
    private let session: URLSession
    private var baseURL: URL

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()

    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        return e
    }()

    init(baseURL: URL) {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 15
        // Ceiling on the whole transfer. Without it the default is seven days.
        config.timeoutIntervalForResource = 30

        // Must stay false. `waitsForConnectivity = true` makes URLSession park
        // a request indefinitely when the host is unreachable instead of
        // failing — which, for a search box, means a spinner that never stops
        // and never explains itself. Failing fast lets the UI say the server
        // is unreachable.
        config.waitsForConnectivity = false
        self.session = URLSession(configuration: config)
    }

    func updateBaseURL(_ url: URL) {
        baseURL = url
    }

    // MARK: - Routing

    func route(
        from origin: CLLocationCoordinate2D,
        to destination: CLLocationCoordinate2D,
        destinationName: String,
        modes: [String],
        avoidCameras: Bool,
        departAt: Date? = nil,
        forceNight: Bool? = nil,
        crimeWindowDays: Int? = nil
    ) async throws -> RouteResponse {
        let body = RouteRequest(
            origin: Coordinate(lat: origin.latitude, lon: origin.longitude),
            destination: Coordinate(lat: destination.latitude, lon: destination.longitude),
            destinationName: destinationName,
            departAt: departAt,
            modes: modes,
            avoidCameras: avoidCameras,
            forceNight: forceNight,
            crimeWindowDays: crimeWindowDays
        )
        return try await post("/route", body: body)
    }

    // MARK: - Overlays

    func cameras(in region: MapBounds) async throws -> CameraResponse {
        try await get("/cameras", query: region.queryItems)
    }

    func crimeGrid(
        in region: MapBounds, nightOnly: Bool, windowDays: Int? = nil
    ) async throws -> CrimeGridResponse {
        var items = region.queryItems
        if nightOnly {
            items.append(URLQueryItem(name: "nightOnly", value: "true"))
        }
        if let windowDays {
            items.append(URLQueryItem(name: "windowDays", value: String(windowDays)))
        }
        return try await get("/crime/grid", query: items)
    }

    // MARK: - Live ETA sharing

    func startShare(destinationName: String) async throws -> ShareCreated {
        try await post(
            "/share", body: ShareCreateRequest(destinationName: destinationName)
        )
    }

    func updateShare(
        token: String,
        ownerKey: String,
        coordinate: CLLocationCoordinate2D,
        etaSeconds: Double?,
        remainingMetres: Double?
    ) async throws -> ShareStatus {
        try await post(
            "/share/\(encoded(token))/update",
            body: ShareUpdateRequest(
                ownerKey: ownerKey,
                lat: coordinate.latitude,
                lon: coordinate.longitude,
                etaSeconds: etaSeconds,
                remainingMetres: remainingMetres
            )
        )
    }

    @discardableResult
    func endShare(
        token: String, ownerKey: String, arrived: Bool
    ) async throws -> ShareStatus {
        try await post(
            "/share/\(encoded(token))/end",
            body: ShareFinishRequest(ownerKey: ownerKey, arrived: arrived)
        )
    }

    // MARK: - Transit

    func transitStops(
        in region: MapBounds, railOnly: Bool = false
    ) async throws -> TransitStopsResponse {
        var items = region.queryItems
        if railOnly {
            items.append(URLQueryItem(name: "railOnly", value: "true"))
        }
        return try await get("/transit/stops", query: items)
    }

    func stopBoard(_ stopId: String) async throws -> StopBoard {
        try await get("/transit/stop/\(encoded(stopId))/board", query: [])
    }

    func tripDetail(
        patternId: Int, tripId: String, fromStop: String = "", vehicleId: String = ""
    ) async throws -> TripDetail {
        var items: [URLQueryItem] = []
        if !fromStop.isEmpty {
            items.append(URLQueryItem(name: "fromStop", value: fromStop))
        }
        if !vehicleId.isEmpty {
            // Lets the server identify *which* train the user is waiting for,
            // which is the only way it can place one on the map.
            items.append(URLQueryItem(name: "vehicleId", value: vehicleId))
        }
        return try await get(
            "/transit/trip/\(patternId)/\(encoded(tripId))", query: items
        )
    }

    /// Escape an opaque id for use as a single path component.
    ///
    /// GTFS ids are arbitrary strings — WMATA's rail stations look like
    /// `STN_B01_F01` and bus trip ids carry colons and slashes — so anything
    /// outside the URL unreserved set has to be escaped or it becomes extra
    /// path segments. Unreserved characters are left alone deliberately:
    /// escaping the underscore in `STN_B01_F01` is legal but makes the logs
    /// unreadable, which is how the double-encoding bug hid for as long as it
    /// did.
    private func encoded(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: Self.unreserved) ?? value
    }

    /// RFC 3986 unreserved: A-Z a-z 0-9 - . _ ~
    private static let unreserved: CharacterSet = {
        var set = CharacterSet.alphanumerics
        set.insert(charactersIn: "-._~")
        return set
    }()

    // MARK: - Places

    func geocode(
        _ query: String, near: CLLocationCoordinate2D? = nil
    ) async throws -> [GeocodeResult] {
        var items = [URLQueryItem(name: "q", value: query)]
        if let near {
            // Lets the server break ties between same-named streets in
            // different quadrants, which DC has plenty of.
            items.append(URLQueryItem(name: "lat", value: String(near.latitude)))
            items.append(URLQueryItem(name: "lon", value: String(near.longitude)))
        }
        let response: GeocodeResponse = try await get("/geocode", query: items)
        return response.results
    }

    func reverseGeocode(_ coordinate: CLLocationCoordinate2D) async throws -> GeocodeResult? {
        let response: GeocodeResponse = try await get(
            "/reverse",
            query: [
                URLQueryItem(name: "lat", value: String(coordinate.latitude)),
                URLQueryItem(name: "lon", value: String(coordinate.longitude)),
            ]
        )
        return response.results.first
    }

    func meta() async throws -> ServerMeta {
        try await get("/meta", query: [])
    }

    func health() async throws -> ServerHealth {
        try await get("/health", query: [])
    }

    // MARK: - Transport

    /// Build a URL from an *already-escaped* path.
    ///
    /// This used to go through `URL.appendingPathComponent`, which escapes
    /// again — so an id containing an underscore went out as `%255F`: the
    /// underscore escaped to `%5F`, then the percent escaped to `%25`. The
    /// server decodes once, sees `%5F`, and finds no such stop. Parsing an
    /// escaped string with `URLComponents(string:)` preserves the escaping
    /// exactly as written instead of applying a second round of it.
    private func makeURL(_ path: String, query: [URLQueryItem]) throws -> URL {
        var base = baseURL.absoluteString
        while base.hasSuffix("/") { base.removeLast() }

        guard var components = URLComponents(string: base + path) else {
            throw RoutingError.badURL
        }
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw RoutingError.badURL }
        return url
    }

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem]) async throws -> T {
        try await perform(URLRequest(url: try makeURL(path, query: query)))
    }

    private func post<Body: Encodable, T: Decodable>(_ path: String, body: Body) async throws -> T {
        var request = URLRequest(url: try makeURL(path, query: []))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try encoder.encode(body)
        return try await perform(request)
    }

    private func perform<T: Decodable>(_ request: URLRequest) async throws -> T {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError {
            throw Self.mapped(error, url: request.url)
        }

        guard let http = response as? HTTPURLResponse else {
            throw RoutingError.decoding("no HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw RoutingError.server(
                status: http.statusCode, detail: Self.detail(from: data)
            )
        }

        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw RoutingError.decoding(error.localizedDescription)
        }
    }

    /// Turn a URLError into something that says what to do about it.
    private static func mapped(_ error: URLError, url: URL?) -> RoutingError {
        let host = url?.host ?? "the server"
        switch error.code {
        case .appTransportSecurityRequiresSecureConnection:
            return .blockedByPolicy
        case .cannotConnectToHost:
            // The commonest one by far, and on a phone it almost always means
            // the address points at the phone itself rather than the Mac.
            return .unreachable(
                "Nothing is listening at \(host). Is the backend running, and "
                    + "is the address right? On a device, localhost means the "
                    + "phone — use your Mac's IP address."
            )
        case .cannotFindHost, .dnsLookupFailed:
            return .unreachable("Can't find \(host).")
        case .notConnectedToInternet:
            return .unreachable("This device is offline.")
        case .timedOut:
            return .unreachable(
                "\(host) didn't respond. If it's on your local network, check "
                    + "both devices are on the same Wi-Fi."
            )
        case .networkConnectionLost:
            return .unreachable("The connection dropped.")
        case .cancelled:
            return .unreachable("Cancelled.")
        default:
            return .unreachable(error.localizedDescription)
        }
    }

    /// FastAPI puts the message in `detail`, which is either a string or, for
    /// validation failures, an array of per-field objects.
    private static func detail(from data: Data) -> String {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let detail = object["detail"] else { return "" }
        if let text = detail as? String { return text }
        if let items = detail as? [[String: Any]] {
            return items.compactMap { $0["msg"] as? String }.joined(separator: "; ")
        }
        return ""
    }
}

/// A map viewport, for the overlay endpoints.
struct MapBounds: Equatable {
    let minLat: Double
    let minLon: Double
    let maxLat: Double
    let maxLon: Double

    var queryItems: [URLQueryItem] {
        [
            URLQueryItem(name: "minLat", value: String(minLat)),
            URLQueryItem(name: "minLon", value: String(minLon)),
            URLQueryItem(name: "maxLat", value: String(maxLat)),
            URLQueryItem(name: "maxLon", value: String(maxLon)),
        ]
    }

    /// Whether `other` is materially different — used to avoid refetching
    /// overlays on every pixel of a pan.
    func differs(from other: MapBounds, threshold: Double = 0.004) -> Bool {
        abs(minLat - other.minLat) > threshold
            || abs(minLon - other.minLon) > threshold
            || abs(maxLat - other.maxLat) > threshold
            || abs(maxLon - other.maxLon) > threshold
    }
}
