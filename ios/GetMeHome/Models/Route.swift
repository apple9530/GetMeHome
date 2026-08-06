import CoreLocation
import Foundation
import MapKit

// Mirrors the backend's response schema. The API emits camelCase precisely so
// these decode without a custom key strategy.

struct Coordinate: Codable, Hashable {
    let lat: Double
    let lon: Double

    var clLocation: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }
}

struct SafetyScore: Codable, Hashable {
    let overall: Int
    let lighting: Int
    let crime: Int
    let isolation: Int
    let pctWellLit: Int
    let pctHighCrime: Int
    let worstStretchRisk: Int
    let worstStretchName: String

    /// Coarse band used for colour and wording. Deliberately three bands: the
    /// underlying model is not precise enough to justify finer distinctions,
    /// and a five-point scale would imply accuracy we do not have.
    enum Band {
        case good, moderate, poor
    }

    var band: Band {
        switch overall {
        case 75...: .good
        case 50..<75: .moderate
        default: .poor
        }
    }

    var bandLabel: String {
        switch band {
        case .good: "Good"
        case .moderate: "Moderate"
        case .poor: "Use caution"
        }
    }
}

struct CameraExposure: Codable, Hashable {
    let camerasPassed: Int
    let fractionCovered: Double
}

struct NavStep: Codable, Hashable, Identifiable {
    let maneuver: String
    let instruction: String
    let voice: String
    let street: String
    let distance: Double
    let duration: Double
    let startIndex: Int
    let location: Coordinate
    let safetyNote: String
    let voiceTriggers: [Double]

    var id: String { "\(startIndex)-\(maneuver)-\(street)" }

    /// SF Symbol for the maneuver arrow.
    var symbolName: String {
        switch maneuver {
        case "depart": "figure.walk"
        case "left": "arrow.turn.up.left"
        case "right": "arrow.turn.up.right"
        case "slight_left": "arrow.up.left"
        case "slight_right": "arrow.up.right"
        case "sharp_left": "arrow.uturn.left"
        case "sharp_right": "arrow.uturn.right"
        case "uturn": "arrow.uturn.down"
        case "arrive": "mappin.and.ellipse"
        default: "arrow.up"
        }
    }
}

struct RouteLeg: Codable, Hashable, Identifiable {
    let mode: String
    let distance: Double
    let duration: Double
    /// Flat [lat, lon, lat, lon, ...]. Halves the JSON payload versus objects.
    let polyline: [Double]
    let steps: [NavStep]
    let safety: SafetyScore?

    let routeName: String
    let headsign: String
    let fromStopName: String
    let toStopName: String
    let departureTime: String
    let arrivalTime: String
    let numStops: Int

    var id: String { "\(mode)-\(fromStopName)-\(departureTime)-\(distance)" }

    var coordinates: [CLLocationCoordinate2D] {
        stride(from: 0, to: polyline.count - 1, by: 2).map {
            CLLocationCoordinate2D(latitude: polyline[$0], longitude: polyline[$0 + 1])
        }
    }

    var isWalking: Bool { mode == "walk" }

    var transitSymbol: String {
        switch mode {
        case "metro", "rail", "tram", "monorail": "tram.fill"
        case "bus", "trolleybus": "bus.fill"
        case "ferry": "ferry.fill"
        default: "figure.walk"
        }
    }
}

struct Itinerary: Codable, Hashable, Identifiable {
    let id: String
    let kind: String
    let label: String
    let summary: String
    let duration: Double
    let walkDistance: Double
    let departureTime: String
    let arrivalTime: String
    let numTransfers: Int
    let safety: SafetyScore
    let cameras: CameraExposure
    let legs: [RouteLeg]

    var isTransit: Bool { kind == "transit" }

    var allCoordinates: [CLLocationCoordinate2D] {
        legs.flatMap(\.coordinates)
    }

    /// Every walking step across all legs, in order — what the turn-by-turn
    /// screen actually walks the user through.
    var walkingSteps: [NavStep] {
        legs.filter(\.isWalking).flatMap(\.steps)
    }

    /// The transit modes used, in order, for the summary row icons.
    var transitLegs: [RouteLeg] {
        legs.filter { !$0.isWalking }
    }
}

struct RouteResponse: Codable {
    let itineraries: [Itinerary]
    let isNight: Bool
    /// The crime lookback the server actually used, after snapping the request
    /// to one it was built with. Optional so an older server still decodes.
    let crimeWindowDays: Int?
    let city: String?
    let generatedAt: Date
    let notices: [String]
}

// MARK: - Requests

struct RouteRequest: Codable {
    let origin: Coordinate
    let destination: Coordinate
    let destinationName: String
    let departAt: Date?
    let modes: [String]
    let avoidCameras: Bool
    let forceNight: Bool?
    let crimeWindowDays: Int?
    let city: String?
}

// MARK: - Overlays

struct ALPRCamera: Codable, Hashable, Identifiable {
    let id: String
    let lat: Double
    let lon: Double
    /// Nil means the bearing is not mapped. Drawn as a ring rather than a cone
    /// so the map never implies a direction the data does not have.
    let direction: Double?
    let operatorName: String

    private enum CodingKeys: String, CodingKey {
        case id, lat, lon, direction
        case operatorName = "operator"
    }

    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }
}

struct CameraResponse: Codable {
    let cameras: [ALPRCamera]
    let total: Int
    let truncated: Bool
}

struct OffenseCount: Codable, Hashable {
    /// Raw MPD code, e.g. "THEFT F/AUTO".
    let offense: String
    /// Readable form, supplied by the server so the wording lives in one
    /// place. MPD's codes are database values and no amount of automatic
    /// title-casing turns "THEFT F/AUTO" into English.
    let displayName: String
    let count: Int
    /// "violent" | "sexual" | "property" | "other"
    let category: String
    /// Share of the cell's weighted risk, 0-1.
    let share: Double

    var isSerious: Bool { category == "violent" || category == "sexual" }

    /// Falls back to the raw code if an older server omits the readable form.
    var label: String { displayName.isEmpty ? offense.capitalized : displayName }
}

/// One hexagon of aggregated incidents.
struct CrimeCell: Codable, Hashable, Identifiable {
    let id: String
    let centerLat: Double
    let centerLon: Double
    /// The six corners as flat [lat, lon, ...] pairs, computed server-side so
    /// the drawn cell is exactly the one incidents were binned into.
    let vertices: [Double]
    let total: Int
    /// Severity-weighted intensity in [0, 1], relative to the busiest cell
    /// currently in view.
    let intensity: Double
    let nightShare: Double
    /// Violent and sexual offences only.
    let seriousCount: Int
    let byOffense: [OffenseCount]
    let latest: String

    var center: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: centerLat, longitude: centerLon)
    }

    var polygon: [CLLocationCoordinate2D] {
        stride(from: 0, to: vertices.count - 1, by: 2).map {
            CLLocationCoordinate2D(latitude: vertices[$0], longitude: vertices[$0 + 1])
        }
    }
}

struct CrimeGridResponse: Codable {
    let cells: [CrimeCell]
    let radius: Double
    let totalIncidents: Int
    let nightOnly: Bool
    /// Lookback applied, in days. 0 or nil means everything the server holds.
    let windowDays: Int?
}

struct GeocodeResult: Codable, Hashable, Identifiable {
    let name: String
    let address: String
    let lat: Double
    let lon: Double

    var id: String { "\(lat),\(lon),\(name)" }
    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }
}

struct GeocodeResponse: Codable {
    let results: [GeocodeResult]
}

/// `/health`. Every field past `status` is optional so an older server,
/// which reported fewer of them, still decodes rather than failing in a way
/// that looks like the server being unreachable.
struct ServerHealth: Codable {
    let status: String
    let segments: Int?
    let streetlights: Int?
    let crimeIncidents: Int?
    let searchablePlaces: Int?
    let transit: Bool?
    let cameras: Int?

    var isReady: Bool { status == "ok" }
}

struct ServerMeta: Codable {
    let builtAt: String
    let nodes: Int
    let segments: Int
    let streetlights: Int
    let crimeIncidents: Int
    let cameras: Int
    let transitStops: Int
    let transitPatterns: Int
    let places: Int
    let crimeHistoryYears: Int
    /// Windows the graph was built with. Optional: an older server omits it,
    /// and the picker falls back to the full set rather than showing nothing.
    let crimeWindows: [Int]?
    let defaultCrimeWindow: Int?
    let city: String?
    let cityName: String?
    let bbox: [Double]
}

// MARK: - Transit stops, timetables and live vehicles

struct TransitStop: Codable, Hashable, Identifiable {
    let id: String
    let name: String
    let lat: Double
    let lon: Double
    /// "metro" | "bus" | "rail" | ...
    let mode: String
    let routes: [String]

    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }

    var isRail: Bool {
        ["metro", "rail", "tram", "monorail", "funicular"].contains(mode)
    }

    var symbolName: String { isRail ? "tram.fill" : "bus.fill" }
}

struct TransitStopsResponse: Codable {
    let stops: [TransitStop]
    let total: Int
    let truncated: Bool
}

struct Departure: Codable, Hashable, Identifiable {
    let routeName: String
    let headsign: String
    let mode: String
    let scheduledTime: String
    let scheduledMinutes: Int
    /// The operator's own prediction, where there is one. Nil means the row is
    /// showing a timetable rather than a live arrival, which the UI says.
    let liveMinutes: Int?
    let patternId: Int
    let tripId: String
    let stopsRemaining: Int
    let vehicleId: String

    var id: String { "\(patternId)-\(tripId)-\(scheduledTime)" }

    var isLive: Bool { liveMinutes != nil }

    /// What to show as "when". Live wins when it exists — that is the whole
    /// point of it — but the scheduled time stays visible alongside so a large
    /// gap between the two is legible rather than mysterious.
    var minutes: Int { liveMinutes ?? scheduledMinutes }

    var minutesLabel: String {
        let value = minutes
        if value <= 0 { return "Now" }
        if value == 1 { return "1 min" }
        return "\(value) min"
    }

    var isRail: Bool {
        ["metro", "rail", "tram", "monorail", "funicular"].contains(mode)
    }
}

struct StopBoard: Codable {
    let stopId: String
    let stopName: String
    let mode: String
    let departures: [Departure]
    let live: Bool
    let liveNote: String
}

struct TripStop: Codable, Hashable, Identifiable {
    let stopId: String
    let name: String
    let lat: Double
    let lon: Double
    let arrivalTime: String
    /// Minutes from now; negative once the call is in the past.
    let minutes: Int
    let passed: Bool

    var id: String { "\(stopId)-\(arrivalTime)" }

    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }
}

struct VehiclePosition: Codable, Hashable {
    let lat: Double
    let lon: Double
    /// True when interpolated rather than reported. Trains are always
    /// estimated — WMATA publishes track circuits, not coordinates — and the
    /// UI labels them so nobody reads a dot as a measurement.
    let estimated: Bool

    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }
}

struct TripDetail: Codable {
    let patternId: Int
    let tripId: String
    let routeName: String
    let headsign: String
    let mode: String
    let stops: [TripStop]
    let polyline: [Double]
    let deviationSeconds: Double
    let vehicle: VehiclePosition?
    let liveNote: String

    var coordinates: [CLLocationCoordinate2D] {
        stride(from: 0, to: polyline.count - 1, by: 2).map {
            CLLocationCoordinate2D(latitude: polyline[$0], longitude: polyline[$0 + 1])
        }
    }

    /// Late (positive) or early (negative), phrased for a person.
    var punctuality: String? {
        let minutes = Int((deviationSeconds / 60).rounded())
        if minutes == 0 { return nil }
        return minutes > 0
            ? "\(minutes) min late"
            : "\(-minutes) min early"
    }
}

// MARK: - Live ETA sharing

struct ShareCreateRequest: Codable {
    let destinationName: String
}

struct ShareCreated: Codable {
    let token: String
    /// The link to hand to a friend. Watching only.
    let url: String
    /// This device's write credential. Never put it in the shared link.
    let ownerKey: String
    let expiresInSeconds: Double
}

struct ShareUpdateRequest: Codable {
    let ownerKey: String
    let lat: Double
    let lon: Double
    let etaSeconds: Double?
    let remainingMetres: Double?
}

struct ShareFinishRequest: Codable {
    let ownerKey: String
    let arrived: Bool
}

struct ShareStatus: Codable {
    /// active | arrived | stale | ended
    let status: String
    let destinationName: String
    let lat: Double?
    let lon: Double?
    let etaSeconds: Double?
    let remainingMetres: Double?
    let updatedAgoSeconds: Double
    let expiresInSeconds: Double
    let pollAfterSeconds: Int

    var isActive: Bool { status == "active" }
}

// MARK: - Cities

/// One city the server can route in.
struct CityInfo: Codable, Hashable, Identifiable {
    let slug: String
    let name: String
    let region: String
    let centerLat: Double
    let centerLon: Double
    /// [minLat, minLon, maxLat, maxLon]
    let bbox: [Double]
    let timezone: String
    /// Whether the server has actually built it. A configured-but-unbuilt city
    /// is still listed so the app can say what is missing rather than
    /// pretending it does not exist.
    let available: Bool
    /// Whether it is resident in memory server-side. The first request for a
    /// city that is not loaded takes several seconds while its graph comes off
    /// disk, which is worth warning about rather than looking like a hang.
    let loaded: Bool

    var id: String { slug }

    var center: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: centerLat, longitude: centerLon)
    }

    /// A span that frames the whole city.
    var span: MKCoordinateSpan {
        guard bbox.count == 4 else {
            return MKCoordinateSpan(latitudeDelta: 0.12, longitudeDelta: 0.12)
        }
        return MKCoordinateSpan(
            latitudeDelta: max(0.05, (bbox[2] - bbox[0]) * 1.05),
            longitudeDelta: max(0.05, (bbox[3] - bbox[1]) * 1.05)
        )
    }

    var mapRegion: MKCoordinateRegion {
        MKCoordinateRegion(center: center, span: span)
    }

    func contains(_ coordinate: CLLocationCoordinate2D) -> Bool {
        guard bbox.count == 4 else { return false }
        return coordinate.latitude >= bbox[0] && coordinate.latitude <= bbox[2]
            && coordinate.longitude >= bbox[1] && coordinate.longitude <= bbox[3]
    }

    var symbolName: String {
        switch slug {
        case "dc": "building.columns.fill"
        case "nyc": "building.2.fill"
        default: "mappin.and.ellipse"
        }
    }
}

struct CitiesResponse: Codable {
    let cities: [CityInfo]
    let defaultCity: String
}
