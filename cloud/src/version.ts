// The server's release version (kept equal to package.json; a test checks it).
// `memex cloud` reads it from /health to tell the owner when their deployment
// is older than the CLI expects. Released as the git tag `cloud-v<version>`,
// which is what the template's update workflow pulls.
export const SERVER_VERSION = "0.2.0";

// The last migration this code expects to have been applied (a test checks it
// is the newest file in migrations/). /health compares it with d1_migrations.
export const EXPECTED_SCHEMA = "0002_inbox_observations.sql";
