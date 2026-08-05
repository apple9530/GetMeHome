import CoreLocation
import Foundation

/// Tracks where along a route the user currently is.
///
/// The non-obvious part is that this searches a *window* around the last known
/// position rather than the whole polyline. A route that doubles back — around
/// a circle, or up and down the same street — has two points that are
/// genuinely closest to the same location, and a global nearest-point search
/// will teleport progress backwards or forwards between them. Constraining the
/// search to the neighbourhood of where the user already was removes the
/// ambiguity.
struct RouteTracker {
    let coordinates: [CLLocationCoordinate2D]
    let steps: [NavStep]
    let totalDistance: Double
    let totalDuration: Double

    /// Cumulative distance along the route at each vertex.
    private let cumulative: [Double]
    /// Distance along the route at which each step's maneuver occurs.
    private let stepDistance: [Double]
    /// Local planar projection of each vertex, in metres.
    private let projected: [(x: Double, y: Double)]
    private let originLat: Double
    private let originLon: Double

    /// How far ahead of the last known position to search, in metres. Has to
    /// cover the distance a walker covers between fixes plus GPS jitter.
    private static let forwardWindow: Double = 180

    /// How far *behind* to search. Deliberately small: this only exists to
    /// recover from a fix that pushed progress too far ahead, and a large
    /// value is what lets a doubled-back route snap to the wrong leg.
    ///
    /// The window is in metres rather than vertices because OSM vertex
    /// spacing ranges from a few metres on a curve to a couple of hundred on
    /// a straight arterial — a fixed vertex count is a different distance on
    /// every street.
    private static let backwardWindow: Double = 35

    init(coordinates: [CLLocationCoordinate2D], steps: [NavStep], duration: Double) {
        self.coordinates = coordinates
        self.steps = steps
        self.totalDuration = duration

        let anchor = coordinates.first
            ?? CLLocationCoordinate2D(latitude: 38.9047, longitude: -77.0164)
        originLat = anchor.latitude
        originLon = anchor.longitude

        let latScale = 111_320.0
        let lonScale = 111_320.0 * cos(anchor.latitude * .pi / 180)
        projected = coordinates.map {
            (x: ($0.longitude - anchor.longitude) * lonScale,
             y: ($0.latitude - anchor.latitude) * latScale)
        }

        var running = [0.0]
        running.reserveCapacity(projected.count)
        for i in 1..<max(projected.count, 1) {
            let dx = projected[i].x - projected[i - 1].x
            let dy = projected[i].y - projected[i - 1].y
            running.append(running[i - 1] + (dx * dx + dy * dy).squareRoot())
        }
        cumulative = running
        totalDistance = running.last ?? 0

        // Steps carry an index into the polyline, but that index is generated
        // per-leg on the server while this polyline is the whole itinerary
        // stitched together. Deriving each maneuver's position from its
        // coordinate instead keeps the two in sync regardless.
        var distances: [Double] = []
        var cursor = 0
        for step in steps {
            let target = step.location.clLocation
            var best = cursor
            var bestDistance = Double.greatestFiniteMagnitude
            for i in cursor..<coordinates.count {
                let d = Self.squaredDistance(coordinates[i], target, lonScale: lonScale)
                if d < bestDistance {
                    bestDistance = d
                    best = i
                }
                // Stop once we are clearly moving away again.
                if bestDistance < 25, d > bestDistance * 4 { break }
            }
            distances.append(running.indices.contains(best) ? running[best] : 0)
            cursor = best
        }
        stepDistance = distances
    }

    /// Last vertex at or before a given distance along the route.
    private func vertexIndex(atOrBefore distance: Double) -> Int {
        guard distance > 0 else { return 0 }
        guard distance < totalDistance else { return max(0, cumulative.count - 2) }

        var low = 0
        var high = cumulative.count - 1
        while low < high {
            let mid = (low + high + 1) / 2
            if cumulative[mid] <= distance {
                low = mid
            } else {
                high = mid - 1
            }
        }
        return min(low, max(0, cumulative.count - 2))
    }

    private static func squaredDistance(
        _ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D, lonScale: Double
    ) -> Double {
        let dx = (a.longitude - b.longitude) * lonScale
        let dy = (a.latitude - b.latitude) * 111_320.0
        return dx * dx + dy * dy
    }

    struct Progress {
        /// Metres travelled along the route.
        let distanceAlong: Double
        /// Perpendicular distance from the route line.
        let deviation: Double
        /// Index into `steps` of the maneuver being approached.
        let stepIndex: Int
        /// Metres until that maneuver.
        let distanceToManeuver: Double
        let remainingDistance: Double
        let remainingDuration: TimeInterval
        /// Index of the polyline vertex matched.
        let vertexIndex: Int
        /// Bearing of the route at the current position, for map rotation.
        let courseAlongRoute: CLLocationDirection

        var fractionComplete: Double {
            let total = distanceAlong + remainingDistance
            return total > 0 ? distanceAlong / total : 0
        }
    }

    /// Locate `location` on the route.
    ///
    /// Pass the previous call's `distanceAlong` to constrain the search to the
    /// neighbourhood of where the walker already was. Pass `nil` — the default
    /// — to search the whole route, which is what the first fix of a session
    /// and a fix taken after a reroute both want.
    func progress(
        for location: CLLocationCoordinate2D, previousDistance: Double? = nil
    ) -> Progress {
        guard projected.count >= 2 else {
            return Progress(
                distanceAlong: 0, deviation: 0, stepIndex: 0,
                distanceToManeuver: 0, remainingDistance: 0,
                remainingDuration: 0, vertexIndex: 0, courseAlongRoute: 0
            )
        }

        let lonScale = 111_320.0 * cos(originLat * .pi / 180)
        let px = (location.longitude - originLon) * lonScale
        let py = (location.latitude - originLat) * 111_320.0

        let lower: Int
        let upper: Int
        if let previousDistance {
            lower = vertexIndex(atOrBefore: previousDistance - Self.backwardWindow)
            upper = min(
                projected.count - 2,
                vertexIndex(atOrBefore: previousDistance + Self.forwardWindow) + 1
            )
        } else {
            lower = 0
            upper = projected.count - 2
        }

        var bestSegment = lower
        var bestT = 0.0
        var bestDistanceSquared = Double.greatestFiniteMagnitude

        for i in lower...max(lower, upper) {
            let a = projected[i]
            let b = projected[i + 1]
            let dx = b.x - a.x
            let dy = b.y - a.y
            let lengthSquared = dx * dx + dy * dy

            var t = 0.0
            if lengthSquared > 1e-9 {
                t = ((px - a.x) * dx + (py - a.y) * dy) / lengthSquared
                t = min(1, max(0, t))
            }
            let qx = a.x + t * dx
            let qy = a.y + t * dy
            let d = (px - qx) * (px - qx) + (py - qy) * (py - qy)
            if d < bestDistanceSquared {
                bestDistanceSquared = d
                bestSegment = i
                bestT = t
            }
        }

        let segmentLength = cumulative[bestSegment + 1] - cumulative[bestSegment]
        let along = cumulative[bestSegment] + bestT * segmentLength
        let deviation = bestDistanceSquared.squareRoot()

        // The maneuver being approached is the first one still ahead. A small
        // tolerance stops the card flickering back to a turn just completed.
        var stepIndex = steps.count - 1
        for (i, distance) in stepDistance.enumerated() where distance > along + 2 {
            stepIndex = i
            break
        }

        let toManeuver = max(0, (stepDistance.indices.contains(stepIndex)
            ? stepDistance[stepIndex] : totalDistance) - along)
        let remaining = max(0, totalDistance - along)

        let a = projected[bestSegment]
        let b = projected[bestSegment + 1]
        let course = (atan2(b.x - a.x, b.y - a.y) * 180 / .pi + 360)
            .truncatingRemainder(dividingBy: 360)

        return Progress(
            distanceAlong: along,
            deviation: deviation,
            stepIndex: stepIndex,
            distanceToManeuver: toManeuver,
            remainingDistance: remaining,
            remainingDuration: totalDistance > 0
                ? totalDuration * (remaining / totalDistance)
                : 0,
            vertexIndex: bestSegment,
            courseAlongRoute: course
        )
    }
}
