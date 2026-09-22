# NexVUE portfolio audit vs NexAPP

Read-only review. Inferred from this repository (`VERSION` **2.30.0**) and from NexAPP **0.10.1** as the UI and auth standard (`README.md`, `docs/nexapp-architecture-spec.md`, `docs/nexapp-theme-kit.md` in [davidmcferrin-spec/NexAPP](https://github.com/davidmcferrin-spec/NexAPP)). No runtime was exercised. Dates and hostnames below are the ones already written in `README.md` and `CLAUDE.md`.

NexAPP’s contract, in short: one identity at `nexapp.nexstar.tv` (local account or Entra SAML), cookie `NexAPP_AUTH` (RS256, host-only, Secure, HttpOnly, SameSite=Lax), then a **live** grant check (`NexApp\Auth\AccessService` or `GET /api/access.php?service_id=`). Catalog `apps` is only a user|admin ceiling. Same-host apps are Apache Aliases. WAN apps that cannot see the cookie use `/launch.php` plus a one-time redeem, not a shared cookie. Alias UI loads `/assets/nexapp-theme.css` and `/assets/theme.js` (`--nx-*`, `nexapp-theme` = light|dark|system).

## Scorecard

| Area | vs NexAPP | Notes |
|---|---|---|
| Portal as Alias `/nexvue` | Strong | Manifest, widget, RS256 verify, live grant check, catalog role ceiling |
| Edge identity | Partial | Local bcrypt remains; SSO is a custom portal JWT, not NexAPP launch redeem |
| Stream authorization | Mixed | WHEP mint is session- and channel-scoped; portal tokens are not station-scoped; side channels are not |
| Edge UI | Divergent | Broadcast tool aesthetic, not the theme kit |
| Portal UI | Partial | Kit is loaded; layout is still a custom top nav |
| DMZ / TLS posture | Partial | HTTPS redirect and WHEP TLS exist; real certs and portal→edge CORS are still called out as remaining |

---

## 1. Status / maturity

NexVUE is a deployed edge product with a catalog portal, not a prototype. `VERSION` is **2.30.0**. `README.md` (Phase roadmap) and `CLAUDE.md` (Phase status) describe:

- **Phase 1** hardware-validated on DeckLink Duo 2 then Quad 2 with Core Ultra 5 235. Encode → MediaMTX → WHEP is working. Glass-to-glass photos are deferred; the written estimate is ~200 ms from player RTT.
- **Phase 1.5** slate/selector rolled back. Production encode is `nexvue-encode.sh` → `nexvue-encode.py`. `nexvue-supervisor.py` is present and unused (`setup.sh` treats it as deferred).
- **Phase 2** edge auth is in tree: bcrypt roles, channel ACL, expiring share links, MediaMTX RS256 JWTs, local JWKS.
- **Phase 3** DMZ controls are largely in tree: loopback MediaMTX API (`mediamtx.yml` `apiAddress: 127.0.0.1:9997`) and status daemon, TLS on Apache and WHEP `:8889`, Settings for public ICE hosts, optional Cloudflare TURN and Stream.
- **Phase 4** catalog portal is in tree under `web-portal/`, meant to mount as NexAPP Alias `/nexvue`.

The opening of `README.md` still says Phase 1 is “LAN only, no TLS, no auth.” That sentence is stale. Later sections of the same file describe TLS, auth, and the portal. Treat the phase table near the end of `README.md`, plus `CLAUDE.md`, as the maturity record.

What “production” means here: station software can be installed with `setup.sh`, operators have Services / Settings / Users, and at least one datacenter host is named in the docs (`dcwasof2nexvue01`). Formal Phase 1 closeout (72h soak, latency photo) is still open in `nexvue-phase1-closeout.sh` and `CLAUDE.md`. NexAPP itself is **0.10.1**, pre-1.0, and is the identity standard rather than a more mature app than NexVUE.

Two deployables:

| Piece | Path | Role |
|---|---|---|
| Edge | `web-node/`, `nexvue-encode.py`, `mediamtx.yml`, `setup.sh` | Capture, encode, local UI, WHEP |
| Portal | `web-portal/`, `setup.sh --portal` | Catalog, group ACL, heartbeat, viewer JWT, edge SSO |

Video is not supposed to transit the portal. Browsers WHEP the edge (`:8889`) or, when Stream is on, a same-origin proxy (`sfu_whep`).

## 2. Incomplete work / gaps

Stated in docs or visible as unwired code:

- **Phase 1 soak and latency.** `CLAUDE.md` and `nexvue-phase1-closeout.sh`: re-deploy plus a 72h window on the datacenter host; burnt-in-clock measurement still deferred (`README.md` latency table).
- **SRT ingest.** `README.md` marks SRT deferred with the Phase 1.5 rollback. `mediamtx.yml` has `srt: no`. Channel `.env` can still say `INPUT_TYPE=srt`; the Settings UI is DeckLink-oriented (`CLAUDE.md`).
- **Slate / input-selector.** `nexvue-supervisor.py` is not `ExecStart`.
- **Portal→edge CORS.** `README.md` Phase 3 row: “Remaining: CORS validation portal-origin → edge.”
- **Real certificates.** Self-signed bootstrap is in `setup.sh` / `nexvue-tls.py`. `CLAUDE.md` still recommends a real cert on Apache and on WHEP `:8889` before wider use (trust on `:443` does not extend to `:8889`).
- **Portal watch is a thin player.** `web-portal/watch.html` plus `web-portal/nexvue-portal-whep.js`. `CLAUDE.md`: VU, CC, and stats on `/watch` are deferred. No cross-site multiview. Fleet health is heartbeat age (stale after 700s), not a DeckLink probe.
- **Auto-switch thresholds** in `web-node/index.html` are documented as first guesses.
- **iGPU Render %** is collected and not charted (`CLAUDE.md`).
- **Channel id ceiling is split.** Auth and portal JWTs only accept `ch0`–`ch7` (`web-node/nexvue-auth-lib.php` `auth_expand_channel_paths`, `web-portal/nexvue-portal-auth-lib.php` `portal_mint_viewer_jwt`). `mediamtx.yml` path regex allows `ch0`–`ch15`. `auth_max_channels()` clamps to 8. Extra MediaMTX paths are not part of the ACL model.
- **README front door vs body.** The intro still describes a no-auth LAN prototype.

Explicit non-goals already written in `CLAUDE.md` (not accidental holes): instant key revocation (bounded by the 300s heartbeat), portal viewers landing in the edge Player without the SSO hop, org billing, multi-org membership, edge `admin` synced from catalog admin.

## 3. Next step

Do this before treating multi-station NexAPP login as the access boundary for live video:

1. **Bind portal-minted tokens to the receiving station** and **apply the same channel ACL on caption, status, and MediaMTX path-list proxies.** Detail and severity are in section 8. Those gaps are the difference between “WHEP requires a JWT” and “a grant for station A’s `ch0` cannot be replayed against station B.”
2. **Finish the written Phase 1 closeout** (`nexvue-phase1-closeout.sh`, `README.md`): redeploy, 72h soak, latency photo when a source monitor exists.
3. **Then** the roadmap leftovers that are already named: portal-origin CORS to the edge (`README.md` Phase 3), and a real certificate on both `:443` and `:8889`.

Wider UI restyle and cross-site multiview should wait until stream tokens are station-scoped. Restyling does not fix who can open a WHEP session.

## 4. Improvements

- **One channel-allow function** used by `whep_jwt`, `sfu_whep`, `nexvue-captions.php`, `nexvue-status.php`, and `nexvue-mediamtx-api.php` (`web-node/`). Today only the JWT mint paths consult `auth_allowed_channels_for_session()`.
- **Redact `jwt` and `NEXVUE_PUBLISH_JWT`** in `nexvue-support-bundle.py` (`redact_text` / `_RE_KEY_VALUE_SECRET` match `token` and `api_key`, not `jwt`). `collect_config()` copies `/etc/nexvue/nexvue.env`.
- **Narrow the publish credential** in `auth_mint_publish_jwt()` (`web-node/nexvue-auth-lib.php`): `path: ""` plus `action: api`, TTL `NEXVUE_AUTH_PUBLISH_TTL_S` (~10 years). Prefer per-path publish, no API action, and a rotatable TTL. Same for `auth_mint_sfu_read_jwt()` written to `sfu-publish.json` by `auth_sfu_write_publish_file()`.
- **Stop trusting `X-Forwarded-Proto` and `HTTP_HOST`** unless a trusted proxy is actually configured. See `nexvue_web_is_https()` in `web-node/nexvue-web-router.php` and the same helper in `web-portal/nexvue-portal-web-router.php`. Password-reset links in `web-node/nexvue-auth.php` (`forgot`, `user_reset_link`) are built from `HTTP_HOST`.
- **Validate `next` on the edge login page** the way `web-portal/login.html` already does (path must start with `/` and not `//`). `web-node/login.html` assigns `location.href = next` from the query string.
- **Login throttling** on `action === 'login'` in `web-node/nexvue-auth.php`. None is present.
- **Share secrets:** `web-node/nexvue-auth-lib.php` stores the raw share token in `share_links.token` so the UI can re-copy the URL. A dump of `auth.db` is then a set of live links. Keep the hash for lookup; keep the raw token only in the create response, or encrypt it.
- **Docs:** rewrite the `README.md` intro so it matches the phase table. Point operators at `web-node/` vs `web-portal/` in the first screenful.
- **Theme:** portal pages already import the kit (`web-portal/catalog.html` and siblings). Edge pages do not. See section 6.
- **Tests:** `test/test_nexvue_auth.py` and `test/test_nexvue_portal_*.py` cover minting and NexAPP grant checks. They do not cover cross-station replay or caption ACL. Add those as negative tests when the fixes land.

## 5. Suggested features

These match gaps already named in `CLAUDE.md` / `README.md`, or the NexAPP surfaces this app does not have yet. They are not a new product direction.

- **NexAPP launch redeem for the edge** (section 7). The edge is a WAN host. The spec’s WAN path is `/launch.php?service_id=nexvue` and `POST /api/launch/redeem.php`, not a long-lived custom JWT in the URL hash.
- **Portal `/watch` parity** with the edge player: CC (`web-node/nexvue-captions.js`), VU (`web-node/nexvue-vu.js`), and the session drawer. `CLAUDE.md` lists this as deferred.
- **Cross-site multiview** on the portal (named non-goal / remaining item). Needs station-scoped tokens first.
- **Fleet panel** beyond heartbeat age: last encode state already collected in metrics (`nexvue-metrics-server.py`) could be summarized on `web-portal/stations.html` without a new inbound probe.
- **Signal alarm** is explicitly deferred to portal ops (`README.md`). Outbound heartbeat is the right direction; a “video unlocked for N minutes” bit on the existing heartbeat would match that, without a DMZ pull.
- **Account theme.** NexAPP `/account.php` stores theme preference. NexVUE uses `localStorage` only (`nexvue-theme` on the edge, `nexapp-theme` on the portal). A shared preference matters once both UIs sit on the hub.
- **SRT** when the encode path is ready (`README.md`). Do not turn `srt: yes` on in `mediamtx.yml` as a listener; ingest should stay a configured publisher the way RTSP is bound to loopback.

## 6. UI / style evaluation vs NexAPP

NexAPP theme kit (`docs/nexapp-theme-kit.md`):

- Stylesheet `/assets/nexapp-theme.css`, script `/assets/theme.js`.
- Tokens `--nx-*`. Classes such as `.nx-btn`, `.nx-app-tile`.
- Preference key `nexapp-theme`: `light` | `dark` | `system`.
- Type: IBM Plex Sans / IBM Plex Mono (the fallback copy in `web-portal/nexapp-tokens.css` loads those from Google Fonts).
- Portal chrome is the NexAPP apps dashboard (`/portal.php`), not a second product nav.

**Portal (`web-portal/`).** Partially aligned.

- `catalog.html`, `watch.html`, `stations.html`, `users.html`, `login.html`, `access.html` link `/nexvue/assets/nexapp-tokens.css`, `/assets/nexapp-theme.css`, and `/assets/theme.js`.
- An inline script reads `localStorage["nexapp-theme"]` and supports `system`.
- `web-portal/nexvue-portal.css` maps `--bg` / `--acc` onto `--nx-*` and sets `font` to `--nx-font`.
- Nav is still a custom `nav.topnav` (`.brand`, `.apps-link`, `.theme-toggle`), not NexAPP tiles or `.nx-btn`. `login.html` uses `class="ch-btn"` for the NexAPP link.
- There is an “Apps” link to `/portal.php` (`catalog.html`), which is the right escape back to the hub.
- Catalog and stations are simple stacked panels (`main` max-width 960px). They do not use the hub’s app-tile layout. That is acceptable for an Alias tool, and it is not yet the same skin.

**Edge (`web-node/`).** A different product UI, on purpose, and not on the standard.

- Every page inlines its own palette. `web-node/login.html` and `web-node/index.html`: background `#14181d`, accent `#56c4f5`, `font: 14px/1.45 ui-monospace`. No `nexapp-theme.css`, no `--nx-*`.
- `web-node/nexvue-ui.js` uses `localStorage` key `nexvue-theme`, default **dark**, values dark|light only. No `system`. NexAPP defaults the kit toward light/system.
- Player / Multiview (`index.html`, `multiview.html`) are dense broadcast tools: rendition, CC, VU, safe area, scope, RTA, theater, PiP. Those controls have no NexAPP equivalent and should stay. The chrome around them (login, nav, settings, users, metrics) is what a station user compares to the hub.
- Login is a local username/password card with a optional portal nudge (`login.html` `#portal-nudge`). NexAPP’s login is the hub welcome (local + SAML). The edge card does not say “Continue with NexAPP” as the primary action; the portal login page does (`web-portal/login.html`).

**Practical alignment, without a restyle in this audit:** edge auth pages and the top nav should consume the same kit and `nexapp-theme` key when the browser is on the hub or when the station is adopted; keep the monospace video overlays. Portal pages should prefer `.nx-btn` and the kit’s nav patterns over `.ch-btn` / duplicated topnav CSS. `web-portal/nexapp-tokens.css` should not be required once `/assets/nexapp-theme.css` is always present on the Alias host; it is a fallback and it pulls fonts from a third party.

## 7. NexAPP integration

What exists (portal on the hub):

| NexAPP requirement | Where |
|---|---|
| `service_id` `nexvue` | `web-portal/nexapp-manifest.json` |
| Alias base `/nexvue` | `nexvue-portal-web-router.php` `nexvue_portal_web_base()`, manifest `base_path` |
| Widget JSON | `web-portal/public/widget.php` (heartbeat health counts), declared in the manifest |
| Verify RS256, fail if PEM missing | `portal_nexapp_verify_jwt()` in `web-portal/nexvue-portal-nexapp.php` |
| Live grant, not JWT `apps` alone | `portal_nexapp_check_access()` → `AccessService` or `GET /api/access.php?service_id=nexvue` |
| Unknown catalog role → user, never admin | `portal_nexapp_normalize_catalog_role()` |
| Page gate redirects to NexAPP login | `nexvue_portal_web_nexapp_login_redirect()` |
| Group → station ACL, heartbeat push | `web-portal/users.html`, `nexvue-portal-heartbeat.php`, `auth_apply_portal_user_sync()` |
| Catalog admin is not edge admin | `portal_mint_sso_jwt()` maps org admin to edge `operator`; sync rejects role `admin` (`auth_apply_portal_user_sync()`) |
| No production portal passwords | `portal_test_auth_enabled()` is `NEXVUE_PORTAL_TEST_AUTH=1` only (`web-portal/nexvue-portal-auth-lib.php`) |

What is missing or different:

| NexAPP pattern | NexVUE today |
|---|---|
| WAN app uses `/launch.php` + `POST /api/launch/redeem.php` with `X-NexApp-Launch-Secret` (`docs/nexapp-architecture-spec.md` §3c, `examples/nexapp-launch-redeem.php`) | Edge SSO is `portal_mint_sso_jwt()` (`typ: nexvue-portal-sso`, 180s) placed in `https://<edge>/login#portal_sso=` (`portal_station_login_url()`). The edge never calls NexAPP redeem. |
| Cookie stays on `nexapp.nexstar.tv` | Correct that the edge does not read `NexAPP_AUTH`. The substitute token is not station-checked on arrival (section 8). |
| `return=` must be a same-host path; WAN hosts are rejected | Portal login builds `return=` under `/nexvue` (`web-portal/login.html`). Edge `next` is not constrained (`web-node/login.html`). |
| Entra only at the hub | Matches the docs. The edge has no SAML client. Good. |
| Break-glass local account | Edge bootstrap is `admin` / `password` with forced change (`setup.sh` comment, `must_change_password` in `web-node/nexvue-auth-lib.php`). That is a second identity store, which the spec allows only as break-glass. It is still a full user database (Users UI, share links). |
| Sibling apps do not take hub DB credentials as the primary design | Same-VM path `require`s `/var/www/nexapp/src/bootstrap.php` and calls `AccessService` and `ServiceGrantRepository` (`portal_nexapp_check_access`, `portal_nexapp_directory`). That matches the “PHP on this VM” option. The HTTP fallback exists. Directory failure omits `users_sync` so nodes are not wiped (`CLAUDE.md`) — and also leaves a just-in-time SSO user in place (section 8). |
| Theme kit on every Alias page | Portal yes, edge no (section 6). |

Adopt URL must include `/nexvue` so heartbeat hits `/nexvue/api/portal` (`CLAUDE.md`). Enrollment is edge-initiated (`portal_enroll` in `web-node/nexvue-auth.php`). There is no portal-to-edge inbound call. That matches the DMZ rule.

**Hosted service vs login redirect.** The catalog is a hosted Alias. Station login is a redirect onto the edge with a bearer token in the fragment, then a local session cookie `nexvue_session`. Viewers who stay on `/nexvue/watch` never need the edge login; they get a 90s MediaMTX JWT from `viewer_jwt` in `web-portal/nexvue-portal-api.php` and open WHEP on the edge. Both paths depend on every adopted edge trusting the **same** portal public key via `auth_merged_jwks()` (`web-node/nexvue-jwks.php`). That shared trust is what makes a token minted for one station acceptable to another (section 8).

## 8. Security issues, bugs, proposed fixes

Stream path as implemented:

1. Browser session or share session (`nexvue_session`) or portal session.
2. `POST /api/auth` `whep_jwt` or portal `viewer_jwt` checks channel ACL and mints RS256 with `mediamtx_permissions`.
3. Browser posts SDP to `https://<edge>:8889/<path>/whep` with `?jwt=` (`mediamtx.yml` `authJWTInHTTPQuery: yes`).
4. MediaMTX verifies against `http://127.0.0.1:9080/nexvue-jwks.php` (local key plus cached portal key).
5. Media is DTLS-SRTP on UDP/TCP `:8189`.

Controls that are in good shape:

- WHEP viewer TTL is 90s (`NEXVUE_AUTH_JWT_TTL_S`, `NEXVUE_PORTAL_VIEWER_JWT_TTL_S`).
- Edge `whep_jwt` / `sfu_whep` reject paths outside the session ACL (`web-node/nexvue-auth.php`).
- RTSP ingest is `127.0.0.1:8554` (`mediamtx.yml`). API and status are loopback; browsers use `nexvue-mediamtx-api.php` and `nexvue-status.php`.
- `authJWTExclude` is only `action: api`, and the comment says that is safe only because the API is loopback.
- Session cookies are HttpOnly, SameSite=Lax; `auth_login_user()` / `auth_login_share()` call `session_regenerate_id(true)`.
- `NEXVUE_API_KEY` comparison uses `hash_equals` (`auth_bearer_api_ok()`).
- Portal NexAPP verify refuses a missing PEM (`portal_nexapp_verify_jwt()`).
- DocumentRoot is `public/` so handlers are not directly enumerable (`web-node/nexvue-web-router.php` header comment).
- Share links require `expires_at` (`auth_share_create()`).
- Stream play URLs are proxied (`sfu_whep`); the browser is not given the Cloudflare publish URL (`CLAUDE.md`).
- Default password `password` is rejected in `change_password`.

### High — portal viewer JWT is not bound to a station

`portal_mint_viewer_jwt()` (`web-portal/nexvue-portal-auth-lib.php`) puts `sub` and `mediamtx_permissions` for `chN` / `chNlo` only. No `station_id`. Every adopted edge serves that portal key from `auth_merged_jwks()` (`web-node/nexvue-jwks.php`, fed by the heartbeat). MediaMTX only checks `authJWTClaimKey: mediamtx_permissions` (`mediamtx.yml`).

A viewer who is allowed `ch0` on station A receives a 90s token that station B will also accept for `ch0` / `ch0lo`, if B is adopted and `:8889` is reachable. Channel names are the same on every node (`ch0`–`ch7`).

**Fix:** put a station identifier in the permission path (or a claim MediaMTX will enforce — today it will not enforce an extra claim), or give each station its own portal signing key and stop merging one fleet-wide key. Re-check on the edge cannot see the portal ACL unless the token carries the station and the edge verifies it before the key is published in JWKS. Short TTL limits the window; it does not fix the binding.

### High — edge SSO JWT ignores `station_id` and creates a local user

`portal_mint_sso_jwt()` includes `station_id`, `role`, and `channels` (`web-portal/nexvue-portal-auth-lib.php`). `auth_portal_jwt_verify()` checks `iss=nexvue-portal`, `typ=nexvue-portal-sso`, `exp`, and RS256 (`web-node/nexvue-auth-lib.php`). `auth_login_portal_sso()` does not compare `station_id` to this node. It creates or updates a local user from `role` and `channels` and calls `auth_login_user()`.

A token minted for station A (TTL `NEXVUE_PORTAL_SSO_JWT_TTL_S` = 180s) presented to station B’s `portal_sso` action (`web-node/nexvue-auth.php`, consumed from the hash in `web-node/login.html`) opens a B session with A’s role and channel list. `channels: null` means all paths (`auth_me_payload()` / `auth_user_channel_bases()`). Org admin/operator SSO tokens are minted with `channels: null` and role `operator`.

The new row persists until a later `users_sync` disables it (`auth_apply_portal_user_sync()`). If the directory is down, the heartbeat omits `users_sync` so the node is not wiped (`nexvue-portal-heartbeat.php`, `CLAUDE.md`). The JIT user then remains.

**Fix:** reject SSO whose `station_id` is not this node’s enrolled id. Do not JIT-provision from the token; require the user to already exist from `users_sync`. Prefer NexAPP one-time launch redeem so the token cannot be replayed to a second host.

### High — long-lived publish JWT in support bundles

`auth_mint_publish_jwt()` grants `publish` on `path: ""` (MediaMTX “any path”) and `api`, for ~10 years, and `auth_ensure_publish_jwt_in_env()` writes `NEXVUE_PUBLISH_JWT` into `/etc/nexvue/nexvue.env`. `nexvue-encode.py` `append_jwt()` puts it on the loopback RTSP URL. `authJWTInHTTPQuery: yes` means the same parameter works on the public WebRTC port.

`nexvue-support-bundle.py` `collect_config()` copies `nexvue.env`. `redact_text()` strips keys whose names contain `password`, `token`, `secret`, or `api_key`. It does not strip `JWT` or `jwt=`. Journals collected by `collect_journals()` go through the same redactor, so a logged pipeline URL with `?jwt=` can survive.

`mediamtx.yml` turns on `webrtc: yes` at `:8889` and does not add a publish allowlist. A copy of that env file is enough to present a publish token to the public listener for any `chN` path the regex allows (`ch0`–`ch15`), not only the station’s `MAX_CHANNELS`.

`auth_mint_sfu_read_jwt()` is the read equivalent (`path: ""`, same TTL), stored as `rtsp_jwt` in `/var/lib/nexvue/auth/sfu-publish.json` (`auth_sfu_write_publish_file()`, mode 0640). It is not in the support-bundle file list. It is still a 10-year read-all credential on disk.

**Fix:** redact `(?i)jwt` values and the `NEXVUE_PUBLISH_JWT` line. Scope publish and SFU-read tokens to `ch0`–`ch{MAX}` only, drop the `api` permission, and rotate them on a short schedule. Keep RTSP on loopback (already true). Do not log query strings.

### Medium — captions, status, and path list ignore channel ACL

- `web-node/nexvue-captions.php` calls `auth_require_any()` then `captions_normalize_channel()`, which allows any `[A-Za-z0-9_-]{1,32}` name. It never calls `auth_allowed_channels_for_session()`. A share limited to `ch0` can read CC text for another channel (program dialogue).
- `web-node/nexvue-status.php` returns the daemon JSON unchanged after `auth_require_any()`. Share viewers learn lock/format for every input.
- `web-node/nexvue-mediamtx-api.php` returns full `/v3/paths/list` (viewer counts, bytes, readiness for every path) to any session, including shares.

WHEP itself is checked. These proxies are the side channel around that check.

**Fix:** filter each response to the session’s channel bases before `echo`.

### Medium — HTTP downgrade via `X-Forwarded-Proto`

`nexvue_web_is_https()` treats `X-Forwarded-Proto: https` as TLS (`web-node/nexvue-web-router.php`, `web-portal/nexvue-portal-web-router.php`). Port 80 is open on purpose as a redirect (`CLAUDE.md`, `setup.sh`). A client that sends that header on port 80 skips the 301. `auth_session_start()` sets `secure` only from `HTTPS` / port 443, not from the forwarded proto, so the session cookie can be set on cleartext.

There is no trusted-proxy allowlist in either router.

**Fix:** ignore `X-Forwarded-Proto` unless the peer is a configured proxy. Keep the port-80 response as a redirect only.

### Medium — open redirect after edge login

`web-node/login.html` uses `params.get("next")` as `location.href` after password login and after `portal_sso`. No check that it is a same-origin path. `web-portal/login.html` does check `/` and rejects `//`.

**Fix:** same rule as the portal page and as NexAPP `return=`: single leading slash, no scheme, no `//`.

### Medium — password-reset URL from `Host`

`forgot` and `user_reset_link` in `web-node/nexvue-auth.php` build `scheme://HTTP_HOST/reset?token=…`. If mail is sent (`auth_try_mail_reset()`), a forged Host makes the link point at another site while the token is real. Reset tokens are stored hashed (`auth_reset_create()`), TTL 3600s (`NEXVUE_AUTH_RESET_TTL_S`).

**Fix:** build links from the configured public hostname (`NEXVUE_PUBLIC_HOSTNAME`), not `HTTP_HOST`.

### Medium — no login rate limit; default break-glass password

`login` in `web-node/nexvue-auth.php` has no delay, counter, or lockout. Stations with `:443` on the internet can be guessed. Bootstrap is `admin` / `password` until the forced change runs (`setup.sh`). That window is the dangerous one.

**Fix:** throttle by username and source, and refuse remote login while `must_change_password` is set for the bootstrap account until it is changed from a local console, or generate the first password at setup and print it once.

### Medium — raw share tokens at rest

`share_links.token` holds the raw secret (`web-node/nexvue-auth-lib.php` schema comment and `auth_share_create()`). `token_hash` is what lookup uses. `auth.db` is www-data writable by design. Anyone who copies the DB can redeem live links until expiry or revoke.

**Fix:** stop persisting the raw token, or encrypt it with a key that is not in the same directory. `users_export` / `shares_export` (`web-node/nexvue-auth.php`, API key) already ship password hashes and token hashes to the sync caller; treat `NEXVUE_API_KEY` as a full backup credential.

### Low — other notes

- **Metrics viewer regex.** `parse_text_or_regex_filter()` in `web-node/nexvue-metrics.php` compiles operator-supplied PCRE. Callers are admin/operator only. A pathological pattern can stall PHP. Cap pattern length (there is `FILTER_MAX_LEN`) is not the same as rejecting nested quantifiers.
- **`apiAllowOrigin: '*'`** and **`authJWTExclude` for `api`** (`mediamtx.yml`). Safe only while `apiAddress` stays loopback. `setup.sh` is what patches the live file. A bad edit that binds `:9997` widely exposes kick/config with no JWT.
- **Logo** `web-node/nexvue-logo.php` is unauthenticated. It is a branding image, not a stream.
- **No CSP, `X-Frame-Options`, or `Referrer-Policy`** on edge or portal routers. NexAPP’s `public/.htaccess` does not set them either. `Referrer-Policy: no-referrer` matters because viewer JWTs travel in the WHEP query string (`authJWTInHTTPQuery`).
- **Self-signed `:8889`.** Operators must accept a second certificate. A user who clicks through a different cert on the media port will complete signaling to the wrong host. Real certs (already recommended in `CLAUDE.md`) close that.
- **Kick fail-open.** `web-node/nexvue-ops.php` comments: a missing WHEP session id does not ban the IP. That is intentional so a viewer is not locked out. It is not an authorization bypass of the JWT.
- **`NEXVUE_AUTH_BYPASS` / `NEXVUE_PORTAL_AUTH_BYPASS`.** If set in the environment, `auth_require_roles()` returns a synthetic admin (`web-node/nexvue-auth-lib.php`, `web-portal/nexvue-portal-auth-lib.php`). Tests use this. It must never be set on a station unit.

### Bugs that are not confidentiality issues

- **Stale README intro** (`README.md` lines describing “no TLS, no auth”) will cause an operator to under-configure a DMZ box.
- **SSO and viewer tokens share one portal key** by design so MediaMTX config never changes (`CLAUDE.md`). The availability win (portal outage does not break local publish) is also why station binding cannot be “whatever key signed it.”
- **`ch8`–`ch15` in `mediamtx.yml`** cannot be granted by the PHP ACL, but the empty-path publish token can still publish them.

---

## File index

| Topic | Paths |
|---|---|
| Edge UI | `web-node/index.html`, `multiview.html`, `login.html`, `channels.html`, `services.html`, `users.html`, `metrics.html`, `nexvue-ui.js` |
| Edge auth and WHEP mint | `web-node/nexvue-auth.php`, `web-node/nexvue-auth-lib.php`, `web-node/nexvue-auth-gate.js`, `web-node/nexvue-web-router.php` |
| MediaMTX | `mediamtx.yml`, `nexvue-mediamtx-jwt-patch.py`, `web-node/nexvue-jwks.php`, `nexvue-jwks-loopback.conf` |
| Side-channel proxies | `web-node/nexvue-captions.php`, `web-node/nexvue-status.php`, `web-node/nexvue-mediamtx-api.php` |
| Publish / SFU | `nexvue-encode.py`, `nexvue-sfu-publish.py`, `auth_sfu_write_publish_file()` in `web-node/nexvue-auth-lib.php` |
| Support bundle | `nexvue-support-bundle.py`, `nexvue-ops-support-bundle.sh` |
| Portal + NexAPP | `web-portal/nexvue-portal-nexapp.php`, `web-portal/nexvue-portal-auth-lib.php`, `web-portal/nexvue-portal-api.php`, `web-portal/nexvue-portal-web-router.php`, `web-portal/nexapp-manifest.json`, `web-portal/public/widget.php`, `nexvue-portal-heartbeat.php` |
| Portal UI | `web-portal/catalog.html`, `watch.html`, `login.html`, `stations.html`, `users.html`, `nexvue-portal.css`, `nexapp-tokens.css` |
| NexAPP standard | NexAPP `README.md`, `docs/nexapp-architecture-spec.md`, `docs/nexapp-theme-kit.md`, `examples/nexapp-access-client.php`, `examples/nexapp-launch-redeem.php` |
