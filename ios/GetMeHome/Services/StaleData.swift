import Foundation

/// Removes local state left behind by features that no longer exist.
///
/// Deleting a feature's code does not delete what it wrote to the device. The
/// downloaded crime packs were tens of megabytes in Application Support, kept
/// deliberately outside Caches so iOS would never reclaim them — which means
/// that without this they would sit there for the life of the install, taking
/// space for a feature that cannot read them.
///
/// Runs once per launch, costs a directory check when there is nothing to do,
/// and is safe to delete outright once no installed build predates the
/// removal.
enum StaleData {
    /// Paths under Application Support that belonged to removed features.
    private static let removedDirectories = ["CrimePacks"]

    /// UserDefaults keys written by removed features.
    private static let removedKeys = ["offlineCrimePacks", "crimePackDownloadedAt"]

    static func purge(defaults: UserDefaults = .standard) {
        let manager = FileManager.default
        guard let base = try? manager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: false
        ) else { return }

        for name in removedDirectories {
            let directory = base.appendingPathComponent(name, isDirectory: true)
            guard manager.fileExists(atPath: directory.path) else { continue }
            try? manager.removeItem(at: directory)
        }

        for key in removedKeys where defaults.object(forKey: key) != nil {
            defaults.removeObject(forKey: key)
        }
    }
}
