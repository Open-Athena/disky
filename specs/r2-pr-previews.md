# r2's PR previews

A PR into `r2` gets a disposable preview through `front-door`'s `pr-preview.yml` (`full` mode, specs/cf-pr-previews.md):

- **Target:** `.github/cf-preview.json` `r2`: Pages project `oa-r2-usage`, previews at `pr-N.oa-r2-usage.pages.dev`. Its dev Pages branch `r2` is protected beside `dev`, so a preview never replaces `dev.r2.oa.dev`'s deployment.
- **Token:** `CF_PAGES_DEPLOY_TOKEN` (Pages Write) in the `r2-preview` GitHub environment, which only `front-door` may use.
- **Sign-in:** OA through the broker's preview client `oa-r2-usage-preview` (`OA_LOGIN_PREVIEW_CLIENT_ID` / `_SECRET`, the Pages project's preview environment).
