# The `#hf-usage` Slack app

`hf-usage-bot.manifest.json` is the **HF Usage Bot** app, made from the shared template (`deploy/slack/`). It posts the usage digest thread to `#hf-usage` and the scan job's failure alerts to `#gcs-usage-alerts`, which every deployment's job shares. Interactivity is off: hf has no staged deletions.

Create it at api.slack.com/apps → *Create New App* → *From a manifest*, then install it to the OA workspace. Store the Bot User OAuth Token (`xoxb-…`) in Secret Manager without printing it:

    pbpaste | tr -d '\n' | gcloud secrets versions add hf-usage-slack-bot-token --project oa-internal-450019 --data-file=-
