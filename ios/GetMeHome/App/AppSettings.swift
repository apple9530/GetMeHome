import Foundation
import Observation
import SwiftUI

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
    }
}
