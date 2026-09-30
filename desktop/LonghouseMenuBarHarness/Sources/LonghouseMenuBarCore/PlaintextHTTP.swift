import Foundation

/// The plaintext-http rule for Runtime Host addresses.
///
/// Native clients send the device token and every transcript to the address
/// they are pointed at. https is always fine. Plaintext http is fine to
/// loopback and to Tailscale addresses (WireGuard encrypts that transport),
/// refused to a LAN or private address unless the user opted in, and refused
/// everywhere else.
///
/// One rule, four clients: this file (it is byte-identical in the iOS app and
/// the macOS Desktop app; a backend test enforces that),
/// `engine/src/plaintext_http.rs` and `server/zerg/services/plaintext_http.py`.
/// `schemas/plaintext-http-vectors.json` is the shared case list all four read
/// in their tests; change the rule there first.
public enum PlaintextHTTP {
    public static let optInFlag = "--allow-insecure-http"
    public static let optInEnvironment = "LONGHOUSE_ALLOW_INSECURE_HTTP"

    public enum HostClass: String, Sendable {
        case loopback
        case tailscale
        case lan
        case `public`
    }

    public enum Outcome: String, Sendable {
        case allowed
        case allowedWarn = "allowed_warn"
        case refusedLAN = "refused_lan"
        case refusedPublic = "refused_public"
        case invalid

        public var isUsable: Bool { self == .allowed || self == .allowedWarn }
    }

    /// Judge a Runtime Host address under the plaintext-http rule.
    public static func check(_ url: String, allowInsecureHTTP: Bool) -> Outcome {
        let text = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty,
              let components = URLComponents(string: text),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              var host = components.host,
              !host.isEmpty
        else { return .invalid }
        if scheme == "https" { return .allowed }
        // A backslash is a path separator to some URL parsers and userinfo to
        // others, so two components could disagree about which host this is.
        if text.contains("\\") { return .invalid }
        if host.hasPrefix("["), host.hasSuffix("]") {
            host = String(host.dropFirst().dropLast())
        }
        switch classify(host: host) {
        case .loopback, .tailscale:
            return .allowed
        case .lan:
            return allowInsecureHTTP ? .allowedWarn : .refusedLAN
        case .public:
            return .refusedPublic
        }
    }

    /// Classify a URL host: case-folded, one trailing dot removed.
    public static func classify(host raw: String) -> HostClass {
        var host = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if host.hasSuffix(".") { host.removeLast() }
        if host == "localhost" { return .loopback }
        if host.contains(":") {
            guard !host.contains("%"), let bytes = ipv6Bytes(host) else { return .public }
            return classify(ipv6: bytes)
        }
        if let octets = ipv4Octets(host) { return classify(ipv4: octets) }
        if host.isEmpty || host.split(separator: ".", omittingEmptySubsequences: false).contains(where: \.isEmpty) {
            return .public
        }
        if host.hasSuffix(".ts.net"), host.count > ".ts.net".count { return .tailscale }
        if host.hasSuffix(".local"), host.count > ".local".count { return .lan }
        return .public
    }

    /// The error a refused address gets; a LAN refusal names the opt-in.
    public static func refusalMessage(_ url: String, outcome: Outcome) -> String {
        let text = url.trimmingCharacters(in: .whitespacesAndNewlines)
        switch outcome {
        case .refusedLAN:
            return "Refusing plaintext \(text): http:// to a LAN address sends the device token and every transcript "
                + "unencrypted. If you trust this network, opt in with \(optInFlag) (or \(optInEnvironment)=1). "
                + "Otherwise use https://, or reach the box over Tailscale, where http:// is allowed "
                + "(a 100.x address or a .ts.net name)."
        case .refusedPublic:
            return "Refusing plaintext \(text): http:// is allowed only to loopback and Tailscale addresses "
                + "(100.64.0.0/10, fd7a:115c:a1e0::/48, *.ts.net). Use https:// (Caddy, nginx, or `tailscale serve`)."
        case .allowed, .allowedWarn, .invalid:
            return "\"\(text)\" is not an http(s) Longhouse address."
        }
    }

    /// The one-line warning shown each time an opted-in LAN address is used.
    public static func insecureWarning(_ url: String) -> String {
        "WARNING: \(url.trimmingCharacters(in: .whitespacesAndNewlines)) is plain http, allowed by \(optInFlag): "
            + "the device token and every transcript cross this network unencrypted."
    }

    // MARK: - Address parsing

    /// Only canonical dotted-quad IPv4 (no leading zeros) is an address here;
    /// a URL parser may read other spellings differently, so they stay names.
    private static func ipv4Octets(_ host: String) -> [UInt8]? {
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return nil }
        var octets: [UInt8] = []
        for part in parts {
            guard !part.isEmpty, part.count <= 3,
                  part.allSatisfy({ $0 >= "0" && $0 <= "9" }),
                  part.count == 1 || part.first != "0",
                  let value = UInt8(part)
            else { return nil }
            octets.append(value)
        }
        return octets
    }

    private static func ipv6Bytes(_ host: String) -> [UInt8]? {
        var address = in6_addr()
        guard inet_pton(AF_INET6, host, &address) == 1 else { return nil }
        return withUnsafeBytes(of: &address) { Array($0) }
    }

    private static func classify(ipv4 octets: [UInt8]) -> HostClass {
        let (a, b) = (octets[0], octets[1])
        if a == 127 { return .loopback }
        if a == 100, (b & 0xc0) == 64 { return .tailscale }
        if a == 10 || (a == 172 && (b & 0xf0) == 16) || (a == 192 && b == 168) || (a == 169 && b == 254) {
            return .lan
        }
        return .public
    }

    private static func classify(ipv6 bytes: [UInt8]) -> HostClass {
        if bytes.dropLast().allSatisfy({ $0 == 0 }), bytes.last == 1 { return .loopback }
        if bytes[0] == 0xfd, bytes[1] == 0x7a, bytes[2] == 0x11, bytes[3] == 0x5c, bytes[4] == 0xa1, bytes[5] == 0xe0 {
            return .tailscale
        }
        if (bytes[0] == 0xfe && (bytes[1] & 0xc0) == 0x80) || (bytes[0] & 0xfe) == 0xfc { return .lan }
        // IPv4-mapped addresses land here on purpose: they are not judged by
        // the IPv4 they wrap.
        return .public
    }
}
