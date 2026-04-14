# iOS Hosted Auth Unification

Status: Active
Owner: iOS hosted auth
Updated: 2026-04-14

## Goal

Make hosted iOS login follow the same control-plane SSO contract as hosted web,
while keeping the browser cookie jar as the only runtime auth truth inside the
app.

## Problem

- Hosted web disables tenant-local Google and password login when a control
  plane URL is configured.
- iOS still has a direct Google-to-tenant path.
- That split created the current bug class:
  - stale or missing browser cookies can dump the user into the tenant web login
  - the app started adding WebView interception to paper over the mismatch
  - hosted auth behavior now depends on which surface happened to start the flow

## Decision

- Hosted iOS auth starts in `ASWebAuthenticationSession` against the control
  plane, not against the tenant runtime.
- The control plane owns the login method choice and pre-auth redirects.
- On success, the control plane redirects back to the app with a short-lived
  tenant SSO token.
- The app exchanges that token with the tenant `POST /api/auth/accept-token`
  endpoint.
- The tenant sets the normal browser session cookies.
- The app syncs those cookies into `WKWebsiteDataStore.default()` and treats
  that store as canonical.
- The iOS shell must not sniff `/login`, bounce between web and native login,
  or inject tenant access tokens into WebKit manually.

## Hosted Flow

1. The app loads `/api/auth/methods` from the tenant.
2. If `sso=true`, the login button launches `ASWebAuthenticationSession` to
   `https://control.<root>/auth/native/open-instance?tenant=<subdomain>&callback_scheme=<app scheme>`.
3. If the user is not already authenticated on the control plane, that route
   redirects through the normal control-plane login flow.
4. Once authenticated, the control plane mints a short-lived tenant SSO token
   and redirects to `<app scheme>://auth-callback?...`.
5. The app posts that token to the tenant `/api/auth/accept-token` route.
6. The app syncs the resulting cookies into WebKit and restores the session
   before showing the main webview.

## Scope For This Cut

- Build the hosted iOS SSO path above.
- Split hosted SSO from self-hosted local auth in the iOS UI instead of
  guessing from navigation behavior.
- Keep the current self-hosted local auth paths for now.
- Leave widget auth out of scope for this cut.

## Non-Goals

- Reworking self-hosted auth contracts.
- App-group or keychain-sharing widget auth.
- Generic browser login inside the tenant WebView.

## Acceptance Criteria

- Hosted iOS no longer posts Google ID tokens directly to the tenant.
- Hosted iOS no longer falls through to tenant web login by default.
- The main WebView is only shown after browser session restore completes.
- Control-plane tests cover the native hosted callback route.
- The iOS app builds cleanly.
