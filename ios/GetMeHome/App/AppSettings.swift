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
    /// The development default: a backend on the machine running the
    /// simulator. On a physical device localhost is the phone itself, so a
    /// device build needs either a configured URL or the Mac's LAN address
    /// typed into Settings.
    static let localServer = "http://localhost:8000"

    /// Where the app talks to when the user has not overridden it.
    ///
    /// Optionally baked in at build time from `GETMEHOME_SERVER_URL`, which
    /// `project.yml` writes into Info.plist. Setting it is what makes a
    /// shipped build reach a deployed backend on first launch with nobody
    /// typing anything. Leaving it unset — the normal case while developing —
    /// keeps the app pointed at localhost, and the Settings field remains an
    /// override either way.
    static let defaultServer: String = {
        guard let value = Bundle.main.object(
            forInfoDictionaryKey: "GetMeHomeServerURL"
        ) as? String else { return localServer }

        // An unset build variable can reach Info.plist either as an empty
        // string or as the literal placeholder, in either of two syntaxes
        // depending on whether XcodeGen or the build system got to it. Rather
        // than enumerate the ways it can be wrong, insist it is right: an
        // absolute http(s) URL with a host. Anything else is not a server
        // address and falling back is better than trying to reach it.
        guard let url = URL(string: value.trimmingCharacters(in: .whitespaces)),
              let scheme = url.scheme?.lowercased(),
              scheme == "https" || scheme == "http",
              let host = url.host,
              !host.isEmpty
        else { return localServer }

        return url.absoluteString
    }()

    var serverURLString: String {
        didSet { defaults.set(serverURLString, forKey: Keys.server) }
    }

    /// Which city the app is routing in, or nil until one has been chosen.
    ///
    /// Nil is a real state rather than a default: everything the app shows is
    /// specific to a city, and guessing one would mean opening on a map of
    /// somewhere the user is not. The picker on first launch is the price of
    /// not guessing.
    var citySlug: String? {
        didSet { defaults.set(citySlug, forKey: Keys.city) }
    }

    /// Cached so the map can be framed and labelled before `/cities` answers.
    var cityName: String {
        didSet { defaults.set(cityName, forKey: Keys.cityName) }
    }

    var hasChosenCity: Bool { citySlug != nil }

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
        URL(string: serverURLString) ?? URL(string: Self.localServer)!
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
        static let city = "citySlug"
        static let cityName = "cityName"
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
        citySlug = defaults.string(forKey: Keys.city)
        cityName = defaults.string(forKey: Keys.cityName) ?? ""
    }

    /// Adopt a city, remembering enough to render before the server answers.
    func select(_ city: CityInfo) {
        citySlug = city.slug
        cityName = city.name
    }
}
