import Foundation
import Observation
import SwiftUI

/// Whether a journey is scored as a day or night trip.
///
/// Matters more than it might sound: lighting carries zero weight in daylight,
/// so a route planned at noon shows no streetlight information at all. Being
/// able to force night is how you plan tonight's walk home this afternoon.
enum TimeOfDay: String, CaseIterable, Identifiable {
    /// Follow the sun at the origin — the sensible default.
    case auto
    case day
    case night

    var id: String { rawValue }

    var label: String {
        switch self {
        case .auto: "Auto"
        case .day: "Day"
        case .night: "Night"
        }
    }

    var symbolName: String {
        switch self {
        case .auto: "clock"
        case .day: "sun.max.fill"
        case .night: "moon.stars.fill"
        }
    }

    /// What to send as the request's `forceNight`. Nil lets the server decide
    /// from real solar elevation at the origin.
    var forceNight: Bool? {
        switch self {
        case .auto: nil
        case .day: false
        case .night: true
        }
    }
}

/// User preferences, persisted in `UserDefaults`.
@Observable
@MainActor
final class AppSettings {
    /// Default points at the simulator's host. On a physical device this has
    /// to be the Mac's LAN address or a deployed URL — localhost on an iPhone
    /// means the iPhone.
    static let defaultServer = "http://localhost:8000"

    var serverURLString: String {
        didSet { defaults.set(serverURLString, forKey: Keys.server) }
    }

    var avoidCameras: Bool {
        didSet { defaults.set(avoidCameras, forKey: Keys.avoidCameras) }
    }

    var showCameraOverlay: Bool {
        didSet { defaults.set(showCameraOverlay, forKey: Keys.showCameras) }
    }

    var showCrimeGrid: Bool {
        didSet { defaults.set(showCrimeGrid, forKey: Keys.showCrimeGrid) }
    }

    /// Restricts the crime grid to evening and midnight shift incidents.
    var crimeGridNightOnly: Bool {
        didSet { defaults.set(crimeGridNightOnly, forKey: Keys.crimeGridNight) }
    }

    var includeTransit: Bool {
        didSet { defaults.set(includeTransit, forKey: Keys.includeTransit) }
    }

    var voiceGuidance: Bool {
        didSet { defaults.set(voiceGuidance, forKey: Keys.voice) }
    }

    var timeOfDay: TimeOfDay {
        didSet { defaults.set(timeOfDay.rawValue, forKey: Keys.timeOfDay) }
    }

    var serverURL: URL {
        URL(string: serverURLString) ?? URL(string: Self.defaultServer)!
    }

    var modes: [String] {
        includeTransit ? ["walk", "transit"] : ["walk"]
    }

    private let defaults: UserDefaults

    private enum Keys {
        static let server = "serverURL"
        static let avoidCameras = "avoidCameras"
        static let showCameras = "showCameraOverlay"
        static let showCrimeGrid = "showCrimeGrid"
        static let crimeGridNight = "crimeGridNightOnly"
        static let includeTransit = "includeTransit"
        static let voice = "voiceGuidance"
        static let timeOfDay = "timeOfDay"
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        serverURLString = defaults.string(forKey: Keys.server) ?? Self.defaultServer
        avoidCameras = defaults.bool(forKey: Keys.avoidCameras)
        showCameraOverlay = defaults.bool(forKey: Keys.showCameras)
        showCrimeGrid = defaults.bool(forKey: Keys.showCrimeGrid)
        crimeGridNightOnly = defaults.bool(forKey: Keys.crimeGridNight)
        // These two default to on, so read them only if previously written.
        includeTransit = defaults.object(forKey: Keys.includeTransit) as? Bool ?? true
        voiceGuidance = defaults.object(forKey: Keys.voice) as? Bool ?? true
        timeOfDay = TimeOfDay(
            rawValue: defaults.string(forKey: Keys.timeOfDay) ?? ""
        ) ?? .auto
    }
}
