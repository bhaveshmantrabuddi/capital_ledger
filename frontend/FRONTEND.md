# Capital Ledger browser client

Dependency-free HTML, CSS, and JavaScript. Serve this directory as the document root with `index.html` at `/`; the local Python application serves these files automatically. Do not open `index.html` directly with a `file:` URL.

## Local use

`config.js` defaults to same-origin requests. The client discovers local versus AWS behavior from the public `GET /api/health` endpoint. Local mode signs in as Alice, and the user selector switches between Alice and Bob. Use fictional personal details when creating accounts. Copy an account ID before switching users to test a transfer to the other user.

The balance card always uses the authoritative API balance, while the line beneath it shows the separate customer summary version (local SQLite summaries in free local mode, DynamoDB in AWS mode). The local recovery controls pause/resume the outbox worker or request a sync pass. In AWS mode, full reconciliation remains a private backend operation; the public UI reports actual customer-summary synchronization state. Refresh and eight-second polling update the visible state; they never submit financial operations.

## AWS configuration

The tracked `config.js` uses local defaults. Start an AWS configuration from [`config.aws.example.js`](config.aws.example.js), replace its placeholders in an ignored `config.deployed.js` copy, and upload that deployment copy as `config.js`. The optional SAM script generates configuration under `.build/` without overwriting local defaults. Example:

```javascript
window.APP_CONFIG = {
  apiBaseUrl: "https://YOUR-API.execute-api.REGION.amazonaws.com",
  authMode: "cognito",
  cognitoDomain: "https://YOUR-DOMAIN.auth.REGION.amazoncognito.com",
  cognitoClientId: "YOUR-PUBLIC-APP-CLIENT-ID",
  redirectUri: "https://YOUR-CLOUDFRONT-DOMAIN/",
  logoutUri: "https://YOUR-CLOUDFRONT-DOMAIN/",
  oauthScopes: ["openid", "email", "ledger-api/access"]
};
```

Register the exact redirect and logout URLs with a Cognito **public app client**, without a client secret. The browser uses Authorization Code with PKCE, validates OAuth state, and sends the access token as a Bearer token. The API must validate that token, the issuer, intended client, and required scope. Serve the frontend over HTTPS. AWS credentials and database passwords never belong in frontend files.

## Retry behavior

Before every financial write, the client persists its request payload and idempotency key in `sessionStorage`, scoped to the authenticated user. If a timeout, network error, server error, or unreadable response leaves an uncertain outcome, the original operation remains available as “Retry saved request.” New financial operations are disabled until it receives a definitive result. Reloading the same browser tab preserves the request. Closing the tab clears session storage; server-side idempotency remains necessary. Do not treat browser storage as an audit trail.

The client parses dollar input into integer cents without decimal floating-point multiplication. All user-provided content is rendered with DOM `textContent`, and all authoritative balances come from API responses.

## Accessibility

The interface includes explicit form labels, keyboard focus indicators, live status messages, a skip link, native modal-dialog focus management, responsive layouts, reduced-motion support, and semantic transaction tables. No remote fonts, scripts, icons, or build dependencies are required.
