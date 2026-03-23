type GoogleCodeResponse = {
  code?: string;
  error?: string;
  error_description?: string;
};

type GoogleCodeError = {
  type?: string;
  message?: string;
};

type GoogleCodeClient = {
  requestCode: () => void;
};

let googleIdentityScriptPromise: Promise<void> | null = null;

function ensureGoogleIdentityScript(): Promise<void> {
  if (window.google?.accounts?.oauth2) {
    return Promise.resolve();
  }

  if (googleIdentityScriptPromise) {
    return googleIdentityScriptPromise;
  }

  googleIdentityScriptPromise = new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>('script[data-google-identity="true"]');
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error("Failed to load Google Identity Services.")), {
        once: true,
      });
      return;
    }

    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.defer = true;
    script.dataset.googleIdentity = "true";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Failed to load Google Identity Services."));
    document.head.appendChild(script);
  });

  return googleIdentityScriptPromise;
}

export async function requestGoogleAuthorizationCode(options: {
  clientId: string;
  scope: string;
}): Promise<string> {
  if (!options.clientId) {
    throw new Error("Google OAuth is not configured on this instance.");
  }

  await ensureGoogleIdentityScript();

  return new Promise<string>((resolve, reject) => {
    const oauth2 = window.google?.accounts?.oauth2;
    if (!oauth2?.initCodeClient) {
      reject(new Error("Google Identity Services is unavailable."));
      return;
    }

    const client = oauth2.initCodeClient({
      client_id: options.clientId,
      scope: options.scope,
      ux_mode: "popup",
      select_account: true,
      callback: (response: GoogleCodeResponse) => {
        if (response.error) {
          reject(new Error(response.error_description || response.error));
          return;
        }
        if (!response.code) {
          reject(new Error("Google did not return an authorization code."));
          return;
        }
        resolve(response.code);
      },
      error_callback: (error: GoogleCodeError) => {
        reject(new Error(error.message || error.type || "Google sign-in was cancelled."));
      },
    });

    client.requestCode();
  });
}
