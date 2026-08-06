import CoreLocation
import Foundation

enum RoutingError: LocalizedError {
    case badURL
    case server(status: Int, detail: String)
    case offline
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .badURL:
            "The server address is not valid. Check it in Settings."
        case let .server(status, detail):
            // The backend puts a human-readable reason in `detail` for the
            // cases a user can act on — off-graph endpoints especially — so
            // surface it rather than a status code.
            detail.isEmpty ? "The server returned an error (\(status))." : detail
        case .offline:
            "Can't reach the routing server. Check your connection."
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
        forceNight: Bool? = nil
    ) async throws -> RouteResponse {
        let body = RouteRequest(
            origin: Coordinate(lat: origin.latitude, lon: origin.longitude),
            destination: Coordinate(lat: destination.latitude, lon: destination.longitude),
            destinationName: destinationName,
            departAt: departAt,
            modes: modes,
            avoidCameras: avoidCameras,
            forceNight: forceNight
        )
        return try await post("/route", body: body)
    }

    // MARK: - Overlays

    func cameras(in region: MapBounds) async throws -> CameraResponse {
        try await get("/cameras", query: region.queryItems)
    }

    func crimeGrid(in region: MapBounds, nightOnly: Bool) async throws -> CrimeGridResponse {
        var items = region.queryItems
        if nightOnly {
            items.append(URLQueryItem(name: "nightOnly", value: "true"))
        }
        return try await get("/crime/grid", query: items)
    }

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

    // MARK: - Transport

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem]) async throws -> T {
        guard var components = URLComponents(
            url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false
        ) else { throw RoutingError.badURL }
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw RoutingError.badURL }

        return try await perform(URLRequest(url: url))
    }

    private func post<Body: Encodable, T: Decodable>(_ path: String, body: Body) async throws -> T {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
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
        } catch let error as URLError where error.code == .notConnectedToInternet
            || error.code == .cannotConnectToHost || error.code == .timedOut {
            throw RoutingError.offline
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
