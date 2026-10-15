# The `#hf-usage` Slack app

`hf-usage-bot.manifest.json` is the **HF Usage Bot** app, made from the shared template (`deploy/slack/`). It posts the usage digest thread to `#hf-usage` and the scan job's failure alerts to `#gcs-usage-alerts`, which every deployment's job shares. Interactivity is off: hf has no staged deletions.

Create it at api.slack.com/apps → *Create New App* → *From a manifest*, then install it to the OA workspace. Store the Bot User OAuth Token (`xoxb-…`) in Secret Manager without printing it:

    pbpaste | tr -d '\n' | gcloud secrets versions add hf-usage-slack-bot-token --project oa-internal-450019 --data-file=-

App icon (*Basic Information* → *App icon*; the API can't set it): `hf-app.png`, the usage bots' treemap mark in Hugging Face's yellow and orange (cw-s3's `job/gen-bot-icons.py`, with an `hf` palette, style `plain`).

The digest (`job/digest.yml`, step 12 of `job/hf-run.sh`) also needs the icons Pages project `oa-hf-usage-icons` (the CF stack) to serve the reply avatars: deploy `job/icons-hf/` to its production branch once, `wrangler pages deploy job/icons-hf --project-name oa-hf-usage-icons --branch main`. The job uploads each month's plot to its `plot` branch with `CF_PAGES_TOKEN` (`hf-usage-cf-pages-token`).
