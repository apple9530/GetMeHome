import CoreLocation
import Foundation

/// One end of a route.
///
/// Modelled as an enum rather than a resolved coordinate so that "current
/// location" stays *live*. If it were resolved to a fixed coordinate when the
/// user picked it, then walking a block before hitting Start would silently
/// route from where they used to be.
enum RoutePoint: Equatable, Hashable {
    case currentLocation
    case place(GeocodeResult)

    var displayName: String {
        switch self {
        case .currentLocation: "Current location"
        case let .place(result): result.name
        }
    }

    var subtitle: String {
        switch self {
        case .currentLocation: ""
        case let .place(result): result.address
        }
    }

    var isCurrentLocation: Bool {
        self == .currentLocation
    }

    var symbolName: String {
        switch self {
        case .currentLocation: "location.fill"
        case .place: "mappin.circle.fill"
        }
    }

    /// The fixed coordinate, if this end has one. Nil for current location,
    /// which has to be resolved against a live fix at request time.
    var fixedCoordinate: CLLocationCoordinate2D? {
        switch self {
        case .currentLocation: nil
        case let .place(result): result.coordinate
        }
    }
}

/// Which end of the route the search field is currently editing.
enum RouteField: Equatable {
    case origin
    case destination
}
