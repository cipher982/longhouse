import PhotosUI
import SwiftUI
import UIKit

@MainActor
struct BugReportSheet: View {
    @EnvironmentObject private var appState: AppState
    @Environment(\.dismiss) private var dismiss

    let sourceSessionID: String
    let initialContextJSON: Data
    let initialScreenshot: Data?

    @State private var description = ""
    @State private var screenshotData: Data?
    @State private var additionalFiles: [BugReportUploadFile] = []
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var reportID: String?
    @State private var targetSessionID: String?
    @State private var clientRequestID: String?
    @State private var showingLaunchPicker = false
    @State private var isUploading = false
    @State private var isSending = false
    @State private var draftSaveTask: Task<Void, Never>?
    @State private var errorMessage: String?
    @State private var statusMessage: String?

    init(sourceSessionID: String, contextJSON: Data, screenshotData: Data?) {
        self.sourceSessionID = sourceSessionID
        self.initialContextJSON = contextJSON
        self.initialScreenshot = screenshotData
        _screenshotData = State(initialValue: screenshotData)
    }

    private var canUpload: Bool {
        !description.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !isUploading && reportID == nil
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextEditor(text: $description)
                        .frame(minHeight: 130)
                        .overlay(alignment: .topLeading) {
                            if description.isEmpty {
                                Text("What went wrong? Include what you expected and what happened.")
                                    .foregroundStyle(.secondary)
                                    .padding(.top, 8)
                                    .allowsHitTesting(false)
                            }
                        }
                } header: {
                    Text("Describe the bug")
                }

                Section {
                    if let screenshotData, let image = BugReportScreenCapture.previewImage(from: screenshotData) {
                        Image(uiImage: image)
                            .resizable()
                            .scaledToFit()
                            .frame(maxHeight: 230)
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                            .accessibilityLabel("Captured Longhouse screen")
                        Button("Remove captured screenshot", role: .destructive) {
                            self.screenshotData = nil
                            saveDraft()
                        }
                    } else {
                        Label("No screen capture", systemImage: "rectangle.slash")
                            .foregroundStyle(.secondary)
                    }
                    PhotosPicker(
                        selection: $photoItems,
                        maxSelectionCount: max(0, 4 - (screenshotData == nil ? 0 : 1)),
                        matching: .images
                    ) {
                        Label("Add screenshots from Photos", systemImage: "photo.on.rectangle")
                    }
                    if !additionalFiles.isEmpty {
                        Text("Added screenshots: \(additionalFiles.count)")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Evidence")
                } footer: {
                    Text("The report includes recent iOS diagnostics and the visible session state. Review the image before sending.")
                }

                if let errorMessage {
                    Section {
                        Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                    }
                }
                if let statusMessage {
                    Section {
                        Label(statusMessage, systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                    }
                }
            }
            .navigationTitle("Report a bug")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if isUploading || isSending {
                        ProgressView()
                    } else if reportID == nil {
                        Button("Choose agent") { Task { await uploadAndChooseTarget() } }
                            .disabled(!canUpload)
                    } else if targetSessionID == nil {
                        Button("Choose agent") { showingLaunchPicker = true }
                    } else {
                        Button("Retry") { Task { await sendReport() } }
                            .disabled(isSending)
                    }
                }
            }
            .task {
                restoreDraft()
            }
            .onChange(of: description) { _, _ in scheduleDraftSave() }
            .onChange(of: photoItems) { _, items in
                Task { await loadPhotos(items) }
            }
            .sheet(isPresented: $showingLaunchPicker) {
                LaunchSessionSheet(
                    onLaunchSelection: { selection in
                        targetSessionID = selection.sessionId
                        let handoff = BugReportHandoff(
                            serverURL: appState.serverURL,
                            reportID: reportID ?? "",
                            sessionID: selection.sessionId,
                            deviceID: selection.deviceId,
                            provider: selection.provider,
                            cwd: selection.cwd,
                            clientRequestID: clientRequestID ?? "ios-report-\(UUID().uuidString)"
                        )
                        clientRequestID = handoff.clientRequestID
                        BugReportLocalStore.saveHandoff(handoff)
                    },
                    onLaunched: { sessionID in
                        showingLaunchPicker = false
                        targetSessionID = sessionID
                        Task { await sendReport() }
                    }
                )
            }
        }
    }

    private func uploadAndChooseTarget() async {
        guard canUpload, let api = LonghouseAPI(host: appState.serverURL) else { return }
        isUploading = true
        errorMessage = nil
        statusMessage = nil
        saveDraft()
        defer { isUploading = false }
        do {
            let response = try await api.uploadBugReport(
                description: description,
                contextJSON: initialContextJSON,
                sourceSessionID: sourceSessionID,
                files: reportFiles()
            )
            reportID = response.reportId
            statusMessage = "Report saved. Choose the machine and workspace to repair it."
            showingLaunchPicker = true
        } catch {
            errorMessage = (error as? LocalizedError)?.errorDescription ?? "Could not save the bug report."
        }
    }

    private func sendReport() async {
        guard let reportID, let sessionID = targetSessionID, let api = LonghouseAPI(host: appState.serverURL) else { return }
        isSending = true
        errorMessage = nil
        let requestID = clientRequestID ?? "ios-report-\(UUID().uuidString)"
        clientRequestID = requestID
        if let handoff = BugReportLocalStore.loadHandoff(), handoff.reportID == reportID, handoff.sessionID == sessionID {
            BugReportLocalStore.saveHandoff(
                BugReportHandoff(
                    serverURL: handoff.serverURL,
                    reportID: handoff.reportID,
                    sessionID: handoff.sessionID,
                    deviceID: handoff.deviceID,
                    provider: handoff.provider,
                    cwd: handoff.cwd,
                    clientRequestID: requestID
                )
            )
        }
        defer { isSending = false }
        do {
            _ = try await api.sendInput(
                id: sessionID,
                text: "Investigate this iOS bug report. Read the staged report evidence before changing code.",
                intent: "auto",
                clientRequestId: requestID,
                reportID: reportID
            )
            BugReportLocalStore.clearDraft()
            BugReportLocalStore.clearHandoff()
            statusMessage = "Sent to Console. The agent has the screenshot and diagnostics."
        } catch {
            if case let LonghouseAPIError.structured(_, errorCode, _) = error,
               errorCode != "turn_start_outcome_unknown" {
                clientRequestID = "ios-report-\(UUID().uuidString)"
                saveHandoffForRetry()
            }
            errorMessage = (error as? LocalizedError)?.errorDescription ?? "The report was saved, but the agent could not be started. Retry without choosing a new target."
        }
    }

    private func saveHandoffForRetry() {
        guard let reportID, let targetSessionID,
              let handoff = BugReportLocalStore.loadHandoff(),
              handoff.reportID == reportID,
              handoff.sessionID == targetSessionID,
              let clientRequestID
        else { return }
        BugReportLocalStore.saveHandoff(
            BugReportHandoff(
                serverURL: handoff.serverURL,
                reportID: handoff.reportID,
                sessionID: handoff.sessionID,
                deviceID: handoff.deviceID,
                provider: handoff.provider,
                cwd: handoff.cwd,
                clientRequestID: clientRequestID
            )
        )
    }

    private func reportFiles() -> [BugReportUploadFile] {
        var files = additionalFiles
        if let screenshotData, let jpeg = preparedJPEG(from: screenshotData) {
            files.insert(BugReportUploadFile(filename: "captured-screen.jpg", mimeType: "image/jpeg", data: jpeg), at: 0)
        }
        return Array(files.prefix(4))
    }

    private func preparedJPEG(from data: Data) -> Data? {
        guard let image = UIImage(data: data) else { return nil }
        let maxDimension: CGFloat = 1600
        let scale = min(1, maxDimension / max(image.size.width, image.size.height))
        let size = CGSize(width: max(1, image.size.width * scale), height: max(1, image.size.height * scale))
        let renderer = UIGraphicsImageRenderer(size: size)
        let resized = renderer.image { _ in
            image.draw(in: CGRect(origin: .zero, size: size))
        }
        var quality: CGFloat = 0.78
        var best: Data?
        while quality >= 0.38 {
            guard let jpeg = resized.jpegData(compressionQuality: quality) else { break }
            best = jpeg
            if jpeg.count <= 1_800_000 { return jpeg }
            quality -= 0.1
        }
        return best
    }

    private func loadPhotos(_ items: [PhotosPickerItem]) async {
        guard !items.isEmpty else { return }
        var loaded: [BugReportUploadFile] = []
        for (index, item) in items.prefix(4).enumerated() {
            guard let data = try? await item.loadTransferable(type: Data.self),
                  let jpeg = preparedJPEG(from: data)
            else { continue }
            loaded.append(BugReportUploadFile(filename: "photo-\(index).jpg", mimeType: "image/jpeg", data: jpeg))
        }
        additionalFiles = Array(loaded.prefix(max(0, 4 - (screenshotData == nil ? 0 : 1))))
        photoItems = []
        saveDraft()
    }

    private func restoreDraft() {
        guard let draft = BugReportLocalStore.loadDraft(),
              draft.serverURL == appState.serverURL,
              draft.sourceSessionID == sourceSessionID
        else { return }
        if description.isEmpty { description = draft.description }
        if screenshotData == nil { screenshotData = draft.screenshotData }
        if additionalFiles.isEmpty {
            additionalFiles = draft.additionalImages.enumerated().compactMap {
                guard let jpeg = preparedJPEG(from: $0.element) else { return nil }
                return BugReportUploadFile(filename: "saved-photo-\($0.offset).jpg", mimeType: "image/jpeg", data: jpeg)
            }
        }
        if let handoff = BugReportLocalStore.loadHandoff(), handoff.serverURL == appState.serverURL, handoff.sessionID.isEmpty == false {
            reportID = handoff.reportID
            targetSessionID = handoff.sessionID
            clientRequestID = handoff.clientRequestID
        }
    }

    private func makeDraft() -> BugReportDraft? {
        guard !description.isEmpty || screenshotData != nil || !additionalFiles.isEmpty else { return nil }
        return BugReportDraft(
            serverURL: appState.serverURL,
            sourceSessionID: sourceSessionID,
            description: description,
            screenshotData: screenshotData,
            additionalImages: additionalFiles.map(\.data)
        )
    }

    private func saveDraft() {
        guard let draft = makeDraft() else { return }
        draftSaveTask?.cancel()
        draftSaveTask = nil
        BugReportLocalStore.saveDraft(draft)
    }

    private func scheduleDraftSave() {
        guard let draft = makeDraft() else { return }
        draftSaveTask?.cancel()
        draftSaveTask = Task {
            try? await Task.sleep(nanoseconds: 350_000_000)
            guard !Task.isCancelled else { return }
            await Task.detached(priority: .utility) {
                BugReportLocalStore.saveDraft(draft)
            }.value
        }
    }
}
