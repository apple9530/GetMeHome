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

/// How far back the crime data should look.
///
/// The tradeoff is real and belongs to the user, not to us: a year is stable
/// but slow to notice a neighbourhood changing, while 30 days reacts fast and
/// is noisy — a month of DC data is a few thousand incidents spread over 177
/// square kilometres. Both are defensible; which one you want depends on
/// whether you are asking "what is this area like" or "what has been happening
/// lately".
enum CrimeWindow: Int, CaseIterable, Identifiable {
    case month = 30
    case twoMonths = 60
    case sixMonths = 180
    case year = 365

    var id: Int { rawValue }

    var label: String {
        switch self {
        case .month: "30 days"
        case .twoMonths: "2 months"
        case .sixMonths: "6 months"
        case .year: "1 year"
        }
    }
}

/// User preferences, persisted in `UserDefaults`.
@Observable
@MainActor
final class AppSettings {
    /// Where the app talks to when the user has not overridden it.
    ///
    /// Baked in at build time from `GETMEHOME_SERVER_URL`, which `project.yml`
    /// writes into Info.plist. That is what makes a shipped build connect to
    /// the deployed backend on first launch with nobody typing anything — the
    /// Settings field stays as an override for development and for anyone
    /// running their own server.
    ///
    /// Falls back to the simulator's host. On a physical device localhost is
    /// the phone itself, so a device build with no configured URL will fail to
    /// connect, which is the correct and visible outcome rather than a silent
    /// one.
    static let defaultServer: String = {
        let configured = Bundle.main.object(forInfoDictionaryKey: "GetMeHomeServerURL")
        if let value = configured as? String,
           !value.isEmpty,
           // Guard against the placeholder surviving into a build.
           !value.contains("$("),
           URL(string: value) != nil {
            return value
        }
        return "http://localhost:8000"
    }()

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

    /// Every Metro and bus stop, drawn on the map and tappable for its
    /// timetable.
    var showTransitStops: Bool {
        didSet { defaults.set(showTransitStops, forKey: Keys.showTransitStops) }
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

    /// Applies to the route scores and the map grid together, so what you are
    /// looking at is always the data your route was scored against.
    var crimeWindow: CrimeWindow {
        didSet { defaults.set(crimeWindow.rawValue, forKey: Keys.crimeWindow) }
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
        static let showTransitStops = "showTransitStops"
        static let crimeGridNight = "crimeGridNightOnly"
        static let includeTransit = "includeTransit"
        static let voice = "voiceGuidance"
        static let timeOfDay = "timeOfDay"
        static let crimeWindow = "crimeWindowDays"
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        serverURLString = defaults.string(forKey: Keys.server) ?? Self.defaultServer
        avoidCameras = defaults.bool(forKey: Keys.avoidCameras)
        showCameraOverlay = defaults.bool(forKey: Keys.showCameras)
        showCrimeGrid = defaults.bool(forKey: Keys.showCrimeGrid)
        showTransitStops = defaults.bool(forKey: Keys.showTransitStops)
        crimeGridNightOnly = defaults.bool(forKey: Keys.crimeGridNight)
        // These two default to on, so read them only if previously written.
        includeTransit = defaults.object(forKey: Keys.includeTransit) as? Bool ?? true
        voiceGuidance = defaults.object(forKey: Keys.voice) as? Bool ?? true
        timeOfDay = TimeOfDay(
            rawValue: defaults.string(forKey: Keys.timeOfDay) ?? ""
        ) ?? .auto
        crimeWindow = CrimeWindow(
            rawValue: defaults.integer(forKey: Keys.crimeWindow)
        ) ?? .year
    }
}
