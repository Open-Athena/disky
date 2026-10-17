# The `#hf-usage` Slack app

`hf-usage-bot.manifest.json` is the **HF Usage Bot** app, made from the shared template (`deploy/slack/`). It posts the usage digest thread to `#hf-usage` and every alert (pipeline errors, session logging) to `#hf-admin`. Interactivity is off: hf has no staged deletions.

Create it at api.slack.com/apps → *Create New App* → *From a manifest*, then install it to the OA workspace. Store the Bot User OAuth Token (`xoxb-…`) in Secret Manager without printing it:

    pbpaste | tr -d '\n' | gcloud secrets versions add hf-usage-slack-bot-token --project oa-internal-450019 --data-file=-

App icon (*Basic Information* → *Display Information*; the API can't set it): `deploy/slack/icons/hf.png`, the site's Hugging Face mark full-bleed (`scripts/marks/`).

The digest (`job/digest.yml`, the scan's `post` step, `job/profiles/hf.yml`) also needs the icons Pages project `oa-hf-usage-icons` (the CF stack) to serve the reply avatars: deploy `job/icons-hf/` to its production branch once, `wrangler pages deploy job/icons-hf --project-name oa-hf-usage-icons --branch main`. The job uploads each month's plot to its `plot` branch with `CF_PAGES_TOKEN` (`hf-usage-cf-pages-token`).
