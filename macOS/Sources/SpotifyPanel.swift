import SwiftUI

struct SpotifyPanel: View {
    private enum Page: String, CaseIterable {
        case tracks = "Top tracks"
        case playlists = "Playlists"
        case account = "Account"
    }

    private struct LoadRequest: Equatable {
        let page: Page
        let connected: Bool
        let topTracksAuthorized: Bool
        let authorizationPending: Bool
        let refresh: Int
    }

    private enum Content {
        case loading
        case tracks([SpotifyTrack])
        case playlists([SpotifyPlaylist])
        case account(SpotifyAccount)
        case permission
        case unavailable(String)
    }

    let voice: VoiceSession
    @State private var page = Page.tracks
    @State private var content = Content.loading
    @State private var refresh = 0
    @State private var isWorking = false
    @State private var message: String?
    @State private var failed = false

    var body: some View {
        let request = LoadRequest(
            page: page, connected: voice.spotifyConnected,
            topTracksAuthorized: voice.spotifyTopTracksAuthorized,
            authorizationPending: voice.spotifyAuthorizationPending, refresh: refresh
        )
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Picker("Spotify library", selection: $page) {
                    ForEach(Page.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                Button { refresh += 1 } label: {
                    Label("Refresh Spotify", systemImage: "arrow.clockwise")
                        .labelStyle(.iconOnly)
                }
                .help("Refresh Spotify")
                .disabled(!request.connected || isWorking)
            }

            if request.connected {
                libraryContent
                if page == .account {
                    HStack {
                        Button("Reauthorize", action: reauthorize)
                        Button("Sign Out", role: .destructive) {
                            perform { try await voice.disconnectSpotify() }
                        }
                        .help("Remove Keni’s Spotify sign-in")
                    }
                    .disabled(isWorking || request.authorizationPending)
                }
            } else {
                emptyState("Spotify is disconnected", icon: "music.note", detail: "Connect Spotify to view your library.")
            }

            if isWorking {
                ProgressView("Working with Spotify…")
                    .controlSize(.small)
            } else if let message {
                Text(verbatim: message)
                    .font(.subheadline)
                    .foregroundStyle(failed ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task(id: request) { await load(request) }
    }

    @ViewBuilder
    private var libraryContent: some View {
        switch content {
        case .loading:
            ProgressView("Loading Spotify…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .permission:
            VStack(spacing: 12) {
                Text("Allow Keni to view your top tracks.")
                    .foregroundStyle(.secondary)
                Button("Reauthorize Spotify", action: reauthorize)
                    .disabled(isWorking || voice.spotifyAuthorizationPending)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .unavailable(let error):
            emptyState("Spotify data unavailable", icon: "exclamationmark.circle", detail: error)
        case .tracks(let tracks):
            if tracks.isEmpty {
                emptyState("No top tracks yet", icon: "music.note", detail: "Tracks will appear here as you listen on Spotify.")
            } else {
                List(tracks) { track in
                    HStack {
                        itemLabel(track.name, detail: track.artist)
                        Button("Play") { play(track) }
                            .accessibilityLabel("Play \(track.name)")
                            .disabled(isWorking)
                    }
                    .buttonStyle(.borderless)
                    .padding(.vertical, 4)
                }
                .listStyle(.inset)
            }
        case .playlists(let playlists):
            if playlists.isEmpty {
                emptyState("No playlists in your library", icon: "music.note.list")
            } else {
                Text("Link a playlist to play it by saying its name.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                List(playlists) { playlist in
                    HStack {
                        itemLabel(playlist.name, detail: playlist.linkedName.map { "Say “Play \($0)”" } ?? "")
                        Button(playlist.linkedName == nil ? "Link" : "Unlink") { toggleLink(playlist) }
                            .accessibilityLabel(playlist.linkedName == nil ? "Link \(playlist.name)" : "Unlink \(playlist.name)")
                            .disabled(isWorking)
                    }
                    .buttonStyle(.borderless)
                    .padding(.vertical, 4)
                }
                .listStyle(.inset)
            }
        case .account(let account):
            List {
                Section("Spotify account") {
                    Text(verbatim: account.name)
                }
                Section("Spotify Connect devices") {
                    if account.devices.isEmpty {
                        Text("Open Spotify to make a device available.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(account.devices.indices, id: \.self) { index in
                        let device = account.devices[index]
                        HStack {
                            Text(verbatim: device.name)
                            Spacer()
                            if device.active {
                                Label("Active", systemImage: "speaker.wave.2")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .listStyle(.inset)
        }
    }

    private func itemLabel(_ title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(verbatim: title).font(.subheadline.weight(.semibold))
            if !detail.isEmpty {
                Text(verbatim: detail).font(.caption).foregroundStyle(.secondary)
            }
        }
        .lineLimit(1)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private func emptyState(_ title: String, icon: String, detail: String? = nil) -> some View {
        ContentUnavailableView(title, systemImage: icon, description: detail.map { Text(verbatim: $0) })
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func load(_ request: LoadRequest) async {
        guard request.connected else { return }
        content = .loading
        do {
            let result: Content
            switch request.page {
            case .tracks where !request.topTracksAuthorized:
                result = .permission
            case .tracks:
                let response: SpotifyTracks = try await Backend.request("spotify/top-tracks", method: .get, authenticated: true)
                result = .tracks(response.tracks)
            case .playlists:
                let response: SpotifyPlaylists = try await Backend.request(
                    "spotify/playlists", method: .get, authenticated: true, timeout: 15
                )
                result = .playlists(response.playlists)
            case .account:
                let response: SpotifyAccount = try await Backend.request(
                    "spotify/account", method: .get, authenticated: true, timeout: 10
                )
                result = .account(response)
            }
            if !Task.isCancelled { content = result }
        } catch {
            if !Task.isCancelled { content = .unavailable(error.localizedDescription) }
        }
    }

    private func perform(reload: Bool = false, _ operation: @escaping () async throws -> String?) {
        guard !isWorking else { return }
        isWorking = true
        message = nil
        Task {
            defer { isWorking = false }
            do {
                message = try await operation()
                failed = false
                if reload { refresh += 1 }
            } catch {
                message = error.localizedDescription
                failed = true
            }
        }
    }

    private func reauthorize() {
        perform {
            try await voice.connectSpotify()
            return nil
        }
    }

    private func play(_ track: SpotifyTrack) {
        perform {
            let result: Backend.Message = try await Backend.request(
                "spotify/play-track", method: .post, authenticated: true, timeout: 30,
                body: JSONEncoder().encode(SpotifyURI(uri: track.uri))
            )
            return result.message
        }
    }

    private func toggleLink(_ playlist: SpotifyPlaylist) {
        perform(reload: true) {
            let result: Backend.Message = try await Backend.request(
                "spotify/playlist-link", method: .post, authenticated: true, timeout: 15,
                body: JSONEncoder().encode(PlaylistLink(uri: playlist.uri, linked: playlist.linkedName == nil))
            )
            return result.message
        }
    }
}

private struct SpotifyTrack: Decodable, Identifiable {
    let uri: String
    let name: String
    let artist: String
    var id: String { uri }
}

private struct SpotifyTracks: Decodable {
    let tracks: [SpotifyTrack]
}

private struct SpotifyPlaylist: Decodable, Identifiable {
    let uri: String
    let name: String
    let linkedName: String?
    var id: String { uri }
}

private struct SpotifyPlaylists: Decodable {
    let playlists: [SpotifyPlaylist]
}

private struct SpotifyAccount: Decodable {
    struct Device: Decodable {
        let name: String
        let active: Bool
    }
    let name: String
    let devices: [Device]
}

private struct SpotifyURI: Encodable {
    let uri: String
}

private struct PlaylistLink: Encodable {
    let uri: String
    let linked: Bool
}
