# Jamf Radar

A two-tab dashboard tracking:

- **Jamf Radar** — latest Jamf Pro/Connect releases, security patches, DDM/Blueprints
  deep dives, and community troubleshooting threads.
- **Mac Fix-It KB** — common macOS/Mac hardware issues with practical, step-by-step
  fixes, curated for IT support engineers building toward Jamf administration.

Live site: enable GitHub Pages on this repo (Settings → Pages → Deploy from branch →
`main` → `/(root)`) and it'll be served at `https://<username>.github.io/<repo>/`.

## Auto-updates

`.github/workflows/update-jamf-radar.yml` runs daily (and on manual dispatch via the
Actions tab). It calls the Claude API with the built-in `web_search` tool to find new
Jamf news and Mac troubleshooting entries, merges them into `jamf_data.json`, syncs the
same JSON into the `<script id="jamf-data">` block in `index.html`, and commits the
result — so the site stays current without any manual intervention.

Requires a repository secret named `ANTHROPIC_API_KEY` (Settings → Secrets and
variables → Actions → New repository secret) with a valid Anthropic API key from
console.anthropic.com.
