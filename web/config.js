/* Data endpoint for the static deployment.
 *
 * Local development leaves this null: app.js then talks to the Python server
 * at /api/state as before.
 *
 * The GitHub Pages workflow overwrites this file at build time with the raw
 * URL of the published snapshot, e.g.
 *   window.ESCAPE_DATA_URL = "https://raw.githubusercontent.com/<owner>/escape-zagreb/data/data.json";
 * so the deployed page never depends on a backend.
 */
window.ESCAPE_DATA_URL = window.ESCAPE_DATA_URL || null;
