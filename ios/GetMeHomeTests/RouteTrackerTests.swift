import CoreLocation
import XCTest

@testable import GetMeHome

/// Tests for the progress-along-route maths.
///
/// The fixture is a straight line running due north at 0.0009° latitude
/// spacing, which is 100.2 m per step at this latitude — chosen so every
/// expected value below can be worked out by hand.
final class RouteTrackerTests: XCTestCase {
    private let baseLat = 38.9000
    private let baseLon = -77.0300
    private let step = 0.0009
    private let metresPerStep = 0.0009 * 111_320.0  // ≈ 100.2 m

    private func makeCoordinates(_ count: Int = 11) -> [CLLocationCoordinate2D] {
        (0..<count).map {
            CLLocationCoordinate2D(latitude: baseLat + Double($0) * step, longitude: baseLon)
        }
    }

    private func makeStep(
        maneuver: String, at index: Int, distance: Double, triggers: [Double] = []
    ) -> NavStep {
        NavStep(
            maneuver: maneuver,
            instruction: "\(maneuver) test",
            voice: "\(maneuver) test",
            street: "Test St NW",
            distance: distance,
            duration: distance / 1.35,
            startIndex: index,
            location: Coordinate(lat: baseLat + Double(index) * step, lon: baseLon),
            safetyNote: "",
            voiceTriggers: triggers
        )
    }

    private func makeTracker() -> RouteTracker {
        let coordinates = makeCoordinates()
        let steps = [
            makeStep(maneuver: "depart", at: 0, distance: metresPerStep * 5,
                     triggers: [200, 60, 15]),
            makeStep(maneuver: "right", at: 5, distance: metresPerStep * 5,
                     triggers: [200, 60, 15]),
            makeStep(maneuver: "arrive", at: 10, distance: 0),
        ]
        return RouteTracker(
            coordinates: coordinates,
            steps: steps,
            duration: metresPerStep * 10 / 1.35
        )
    }

    func testTotalDistanceMatchesGeometry() {
        let tracker = makeTracker()
        XCTAssertEqual(tracker.totalDistance, metresPerStep * 10, accuracy: 5)
    }

    func testProgressAtStart() {
        let tracker = makeTracker()
        let progress = tracker.progress(for: makeCoordinates()[0])

        XCTAssertEqual(progress.distanceAlong, 0, accuracy: 2)
        XCTAssertEqual(progress.deviation, 0, accuracy: 2)
        XCTAssertEqual(progress.remainingDistance, tracker.totalDistance, accuracy: 5)
    }

    func testProgressPartwayAlong() {
        let tracker = makeTracker()
        let atThird = CLLocationCoordinate2D(latitude: baseLat + 3 * step, longitude: baseLon)
        let progress = tracker.progress(for: atThird)

        XCTAssertEqual(progress.distanceAlong, metresPerStep * 3, accuracy: 5)
        XCTAssertEqual(progress.remainingDistance, metresPerStep * 7, accuracy: 5)
        XCTAssertEqual(progress.fractionComplete, 0.3, accuracy: 0.02)
    }

    func testDeviationFromRouteIsMeasured() {
        let tracker = makeTracker()
        // 0.0005° of longitude at 38.9°N is about 43 m east.
        let offset = CLLocationCoordinate2D(
            latitude: baseLat + 3 * step, longitude: baseLon + 0.0005
        )
        let progress = tracker.progress(for: offset)

        XCTAssertEqual(progress.deviation, 43, accuracy: 6)
        // Being off to one side must not move progress along the route.
        XCTAssertEqual(progress.distanceAlong, metresPerStep * 3, accuracy: 6)
    }

    func testManeuverSelectionAdvances() {
        let tracker = makeTracker()

        // Before the turn at vertex 5, the turn is what's being approached.
        let early = tracker.progress(
            for: CLLocationCoordinate2D(latitude: baseLat + 2 * step, longitude: baseLon)
        )
        XCTAssertEqual(early.stepIndex, 1)
        XCTAssertEqual(early.distanceToManeuver, metresPerStep * 3, accuracy: 6)

        // Past it, the arrival becomes the target.
        let late = tracker.progress(
            for: CLLocationCoordinate2D(latitude: baseLat + 7 * step, longitude: baseLon),
            previousDistance: metresPerStep * 6
        )
        XCTAssertEqual(late.stepIndex, 2)
        XCTAssertEqual(late.distanceToManeuver, metresPerStep * 3, accuracy: 6)
    }

    func testProgressDoesNotJumpBackwardsOnADoubledBackRoute() {
        // Out and back along the same line: vertex 2 and vertex 8 are the same
        // place. A global nearest-point search would flip between them; the
        // windowed search must stay where the walker actually is.
        var coordinates = makeCoordinates(6)
        coordinates += makeCoordinates(6).reversed()

        let tracker = RouteTracker(
            coordinates: coordinates,
            steps: [makeStep(maneuver: "depart", at: 0, distance: 100)],
            duration: 600
        )

        let ambiguous = CLLocationCoordinate2D(latitude: baseLat + 2 * step, longitude: baseLon)

        let outbound = tracker.progress(
            for: ambiguous, previousDistance: metresPerStep * 1.5
        )
        let returning = tracker.progress(
            for: ambiguous, previousDistance: metresPerStep * 8
        )

        XCTAssertEqual(outbound.distanceAlong, metresPerStep * 2, accuracy: 8)
        // On the way back the same coordinate is much further along the route:
        // the outbound match is 6 steps behind, well outside the backward
        // window, so it must not be selected.
        XCTAssertGreaterThan(returning.distanceAlong, metresPerStep * 6)
    }

    func testFirstFixSearchesWholeRouteWithoutAPreviousPosition() {
        let tracker = makeTracker()
        // No previous distance and a position two thirds along: a windowed
        // search anchored at zero would fail to find it.
        let progress = tracker.progress(
            for: CLLocationCoordinate2D(latitude: baseLat + 7 * step, longitude: baseLon)
        )
        XCTAssertEqual(progress.distanceAlong, metresPerStep * 7, accuracy: 6)
    }

    func testCourseAlongRouteIsNorth() {
        let tracker = makeTracker()
        let progress = tracker.progress(
            for: CLLocationCoordinate2D(latitude: baseLat + 2 * step, longitude: baseLon)
        )
        XCTAssertEqual(progress.courseAlongRoute, 0, accuracy: 2)
    }

    func testEmptyRouteDoesNotCrash() {
        let tracker = RouteTracker(coordinates: [], steps: [], duration: 0)
        let progress = tracker.progress(
            for: CLLocationCoordinate2D(latitude: baseLat, longitude: baseLon)
        )
        XCTAssertEqual(progress.distanceAlong, 0)
        XCTAssertEqual(progress.remainingDistance, 0)
    }

    func testSingleVertexRouteDoesNotCrash() {
        let tracker = RouteTracker(
            coordinates: [CLLocationCoordinate2D(latitude: baseLat, longitude: baseLon)],
            steps: [],
            duration: 0
        )
        let progress = tracker.progress(
            for: CLLocationCoordinate2D(latitude: baseLat, longitude: baseLon)
        )
        XCTAssertEqual(progress.distanceAlong, 0)
    }
}
