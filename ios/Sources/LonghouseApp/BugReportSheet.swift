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
               !["turn_start_outcome_unknown", "turn_start_ambiguous"].contains(errorCode) {
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
        var files = additionalFiles.compactMap { file -> BugReportUploadFile? in
            guard let compressed = try? ImageCompression.compress(file.data) else { return nil }
            return BugReportUploadFile(filename: file.filename, mimeType: compressed.mimeType, data: compressed.data)
        }
        if let screenshotData, let compressed = try? ImageCompression.compress(screenshotData) {
            files.insert(
                BugReportUploadFile(filename: "captured-screen.jpg", mimeType: compressed.mimeType, data: compressed.data),
                at: 0
            )
        }
        return Array(files.prefix(4))
    }

    private func loadPhotos(_ items: [PhotosPickerItem]) async {
        guard !items.isEmpty else { return }
        var loaded: [BugReportUploadFile] = []
        for (index, item) in items.prefix(4).enumerated() {
            guard let data = try? await item.loadTransferable(type: Data.self),
                  let compressed = try? ImageCompression.compress(data)
            else { continue }
            loaded.append(
                BugReportUploadFile(
                    filename: "photo-\(index).jpg",
                    mimeType: compressed.mimeType,
                    data: compressed.data
                )
            )
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
            additionalFiles = draft.additionalImages.enumerated().map {
                BugReportUploadFile(filename: "saved-photo-\($0.offset).jpg", mimeType: "image/jpeg", data: $0.element)
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
        draftSaveTask?.cancel()
        guard let draft = makeDraft() else {
            BugReportLocalStore.clearDraft()
            draftSaveTask = nil
            return
        }
        draftSaveTask = nil
        BugReportLocalStore.saveDraft(draft)
    }

    private func scheduleDraftSave() {
        draftSaveTask?.cancel()
        guard let draft = makeDraft() else {
            BugReportLocalStore.clearDraft()
            draftSaveTask = nil
            return
        }
        draftSaveTask = Task {
            try? await Task.sleep(nanoseconds: 350_000_000)
            guard !Task.isCancelled else { return }
            await Task.detached(priority: .utility) {
                BugReportLocalStore.saveDraft(draft)
            }.value
        }
    }
}
