/* ==============================================================
   AUTH — Cognito sign-in (v4.1). Talks to the Cognito IDP endpoint
   with raw fetch (no SDK; the app is bundler-free ES modules).
   Tokens live in localStorage; api.js attaches the ID token as a
   Bearer header on every call. The API GW JWT authorizer validates
   it server-side, so this module is UX only — the enforcement
   boundary is API Gateway.
   ============================================================== */
import { CONFIG } from './config.js';

const IDP_ENDPOINT = `https://cognito-idp.${CONFIG.COGNITO_REGION}.amazonaws.com/`;
const STORE_KEY = 'pest-monitor-auth';

function loadTokens() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY)) || null; } catch { return null; }
}
function saveTokens(t) { localStorage.setItem(STORE_KEY, JSON.stringify(t)); }
function clearTokens() { localStorage.removeItem(STORE_KEY); }

async function idpCall(target, body) {
    const res = await fetch(IDP_ENDPOINT, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-amz-json-1.1',
            'X-Amz-Target': `AWSCognitoIdentityProviderService.${target}`,
        },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
        const err = new Error(data.message || data.__type || 'Authentication failed');
        err.code = data.__type;
        throw err;
    }
    return data;
}

function storeAuthResult(r, email) {
    const prev = loadTokens() || {};
    saveTokens({
        idToken: r.IdToken,
        // Cognito omits RefreshToken on REFRESH_TOKEN_AUTH responses — keep the old one
        refreshToken: r.RefreshToken || prev.refreshToken,
        expiresAt: Date.now() + r.ExpiresIn * 1000,
        email: email || prev.email,
    });
}

export async function login(email, password) {
    const data = await idpCall('InitiateAuth', {
        AuthFlow: 'USER_PASSWORD_AUTH',
        ClientId: CONFIG.COGNITO_CLIENT_ID,
        AuthParameters: { USERNAME: email, PASSWORD: password },
    });
    // Accounts are created admin-only with permanent passwords
    // (admin-set-user-password --permanent), so challenges should not occur.
    if (data.ChallengeName) {
        throw new Error(`Account needs a password reset (${data.ChallengeName}) — ask the admin.`);
    }
    storeAuthResult(data.AuthenticationResult, email);
}

async function refreshSession() {
    const t = loadTokens();
    if (!t || !t.refreshToken) return false;
    try {
        const data = await idpCall('InitiateAuth', {
            AuthFlow: 'REFRESH_TOKEN_AUTH',
            ClientId: CONFIG.COGNITO_CLIENT_ID,
            AuthParameters: { REFRESH_TOKEN: t.refreshToken },
        });
        storeAuthResult(data.AuthenticationResult);
        return true;
    } catch {
        return false;
    }
}

/* A session exists (a stale ID token is fine — getIdToken refreshes it). */
export function isLoggedIn() {
    const t = loadTokens();
    return !!(t && (t.refreshToken || (t.idToken && Date.now() < t.expiresAt)));
}

/* Valid ID token or null. Refreshes when within 5 min of expiry. */
export async function getIdToken() {
    const t = loadTokens();
    if (!t) return null;
    if (t.idToken && Date.now() < t.expiresAt - 5 * 60 * 1000) return t.idToken;
    if (await refreshSession()) return loadTokens().idToken;
    return null;
}

export function logout() {
    clearTokens();
    requireLogin();
}

/* Show the login overlay — used at boot and by api.js on any 401. */
export function requireLogin() {
    document.getElementById('login-screen').classList.add('open');
    const email = document.getElementById('login-email');
    if (email) email.focus();
}
function hideLogin() {
    document.getElementById('login-screen').classList.remove('open');
}

/* Wire the overlay form. onLogin runs after each successful sign-in. */
export function initAuthUi(onLogin) {
    document.getElementById('login-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = document.getElementById('login-submit');
        const errBox = document.getElementById('login-error');
        errBox.textContent = '';
        btn.disabled = true;
        btn.textContent = 'Signing in…';
        try {
            await login(
                document.getElementById('login-email').value.trim(),
                document.getElementById('login-password').value,
            );
            document.getElementById('login-password').value = '';
            hideLogin();
            await onLogin();
        } catch (err) {
            errBox.textContent = err.message;
        } finally {
            btn.disabled = false;
            btn.textContent = 'Sign in';
        }
    });
}
