import Testing

@testable import Longhouse

/// M3: the transcript state taxonomy is now explicit and unit-testable, instead
/// of being implied by stacked conditionals in the view.
struct TranscriptDisplayStateTests {
    @Test
    func loadingTakesPrecedenceOverEverything() {
        let state = TranscriptDisplayState.derive(
            isInitialLoading: true,
            hasContent: false,
            errorMessage: "boom",
            refreshErrorMessage: "also boom"
        )
        #expect(state == .loading)
        #expect(state.showsTranscript == false)
    }

    @Test
    func contentWithHealthyRefreshIsContent() {
        let state = TranscriptDisplayState.derive(
            isInitialLoading: false,
            hasContent: true,
            errorMessage: nil,
            refreshErrorMessage: nil
        )
        #expect(state == .content)
        #expect(state.showsTranscript == true)
    }

    @Test
    func contentWithFailedRefreshDegradesToBannerNotErase() {
        let state = TranscriptDisplayState.derive(
            isInitialLoading: false,
            hasContent: true,
            errorMessage: nil,
            refreshErrorMessage: "Couldn't refresh"
        )
        #expect(state == .contentWithRefreshError("Couldn't refresh"))
        // Critical: content stays on screen — this is the lock/unlock fix.
        #expect(state.showsTranscript == true)
    }

    @Test
    func contentWinsEvenIfBlockingErrorAlsoSet() {
        // Defensive: if both errorMessage and content somehow coexist, content
        // must win so we never cover a populated transcript with a hard error.
        let state = TranscriptDisplayState.derive(
            isInitialLoading: false,
            hasContent: true,
            errorMessage: "stale blocking error",
            refreshErrorMessage: nil
        )
        #expect(state == .content)
    }

    @Test
    func emptyWhenNoContentAndNoError() {
        let state = TranscriptDisplayState.derive(
            isInitialLoading: false,
            hasContent: false,
            errorMessage: nil,
            refreshErrorMessage: nil
        )
        #expect(state == .empty)
        #expect(state.showsTranscript == true)
    }

    @Test
    func hardErrorOnlyWhenNothingCached() {
        let state = TranscriptDisplayState.derive(
            isInitialLoading: false,
            hasContent: false,
            errorMessage: "Couldn't load session",
            refreshErrorMessage: nil
        )
        #expect(state == .hardError("Couldn't load session"))
        #expect(state.showsTranscript == false)
    }
}
