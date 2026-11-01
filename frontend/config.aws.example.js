/* Public deployment example. Replace placeholders in a deployment-only copy;
   keep tracked config.js on local defaults. Never add credentials here. */
window.APP_CONFIG = Object.freeze({
  apiBaseUrl: "https://YOUR-API.execute-api.YOUR-REGION.amazonaws.com",
  authMode: "cognito",
  cognitoDomain: "https://YOUR-DOMAIN.auth.YOUR-REGION.amazoncognito.com",
  cognitoClientId: "YOUR-PUBLIC-APP-CLIENT-ID",
  redirectUri: window.location.origin + "/",
  logoutUri: window.location.origin + "/",
  oauthScopes: ["openid", "email", "ledger-api/access"]
});
