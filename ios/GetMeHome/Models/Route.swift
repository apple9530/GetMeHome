import CoreLocation
import Foundation

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

struct ServerMeta: Codable {
    let builtAt: String
    let nodes: Int
    let segments: Int
    let streetlights: Int
    let crimeIncidents: Int
    let cameras: Int
    let transitStops: Int
    let transitPatterns: Int
    let crimeHistoryYears: Int
    let bbox: [Double]
}
