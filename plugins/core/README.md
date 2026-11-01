# Telebot core

An external bundle for official Harness 0.2.1-rc.5. Provides the single `telebotCore` service and public capability discovery. It does not create an Agent loop, alter default models, or access Python data.

`telebotCore.conversations()` returns a channel-owned adapter with `commands`: native command discovery/dispatch, model catalog/selection and native session preparation. Channels must authorize trusted actors before calling it. The registry compatibility list currently permits only `compact`; model/cwd controls use public Agent APIs. Adapters resolve optional host services through `ctx.get`, and persist through the official session flush boundary. Consumers must dispose their conversation adapter on unload.

The service also owns the trusted actor scope, optional feature registry and one-use approval/question broker. Host callbacks use that scope; tools cannot invent actor IDs through arguments. Native file/image blocks go through the official attachment service. Build and pack at the repository root; install core before the unified feishu, codex and adviser bundles. See [current status](../../docs/status.md) for current scope and live-environment limits.

Core also owns the native Settings → Telebot 插件 section and configuration-page registry. Official dsh.client/settings.section renders the UI; official Connection owner-only Fetch routes carry configuration reads and saves. Other bundles register their schemas and existing transactional save ports. No standalone configuration server, page, iframe or capability token is shipped.
