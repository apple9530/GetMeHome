import SwiftUI

enum Theme {
    /// Safety colours.
    ///
    /// Not a red/amber/green traffic light: red-green is the most common form
    /// of colour blindness, and this is exactly the kind of at-a-glance
    /// decision where that matters. Blue-to-orange stays distinguishable for
    /// every common type, and every use of colour here is paired with a text
    /// label anyway.
    static func safetyColor(_ score: Int) -> Color {
        switch score {
        case 75...: Color(red: 0.13, green: 0.55, blue: 0.85)
        case 50..<75: Color(red: 0.95, green: 0.62, blue: 0.16)
        default: Color(red: 0.85, green: 0.31, blue: 0.16)
        }
    }

    static func riskColor(_ risk: Double) -> Color {
        safetyColor(Int((1 - risk) * 100))
    }

    static let cameraTint = Color(red: 0.55, green: 0.28, blue: 0.75)
    static let routeLine = Color.accentColor
    static let alternateRouteLine = Color.secondary.opacity(0.55)

    static let cardCorner: CGFloat = 16
}

enum Format {
    static func distance(_ metres: Double) -> String {
        if metres < 1000 {
            return "\(Int(metres.rounded())) m"
        }
        return String(format: "%.1f km", metres / 1000)
    }

    static func duration(_ seconds: Double) -> String {
        let minutes = Int((seconds / 60).rounded())
        if minutes < 60 { return "\(minutes) min" }
        let hours = minutes / 60
        let remainder = minutes % 60
        return remainder == 0 ? "\(hours) hr" : "\(hours) hr \(remainder) min"
    }

    static func clock(_ date: Date) -> String {
        date.formatted(date: .omitted, time: .shortened)
    }

    /// Distance phrasing for the maneuver card, which updates continuously and
    /// so needs coarser steps than a static label to avoid visual noise.
    static func liveDistance(_ metres: Double) -> String {
        switch metres {
        case ..<15: "Now"
        case ..<100: "\(Int(metres / 10) * 10) m"
        case ..<1000: "\(Int(metres / 50) * 50) m"
        default: String(format: "%.1f km", metres / 1000)
        }
    }
}
