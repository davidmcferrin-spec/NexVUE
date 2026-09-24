<?php
/**
 * nexvue-portal-api.php — JSON API for the NexVUE cloud portal (Phase 4).
 *
 * Public: login, logout, me
 * Authed (any portal role): change_password, catalog_list, viewer_jwt, sfu_whep
 * org_admin: users_list, user_create, user_update, stations_list,
 *   enroll_token_create, enroll_token_revoke, catalog_acl_put
 * Edge-initiated, no browser session:
 *   enroll_exchange  — one-time enrollment token in the body
 *   station_heartbeat — Authorization: Bearer <station_api_key>
 *
 * No portal-initiated call to any edge exists anywhere in this file —
 * enroll_exchange and station_heartbeat are both the EDGE calling IN, never
 * the reverse. viewer_jwt mints independently from local SQLite (catalog +
 * ACL already synced via heartbeat) — no live call to the target edge.
 *
 * CLI include: when PHP_SAPI is cli and NEXVUE_PORTAL_HTTP is unset, this
 * file only defines helpers (for unit tests) and returns without dispatching.
 */

declare(strict_types=1);

require_once __DIR__ . '/nexvue-portal-auth-lib.php';

function portal_api_fail(int $status, string $message): never {
    if (!headers_sent()) {
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
    }
    http_response_code($status);
    echo json_encode(['ok' => false, 'error' => $message], JSON_UNESCAPED_SLASHES);
    exit;
}

function portal_api_ok(array $extra = []): never {
    if (!headers_sent()) {
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
    }
    echo json_encode(array_merge(['ok' => true], $extra), JSON_UNESCAPED_SLASHES);
    exit;
}

function portal_api_body(): array {
    $raw = file_get_contents('php://input');
    if (!is_string($raw) || trim($raw) === '') {
        return [];
    }
    $j = json_decode($raw, true);
    return is_array($j) ? $j : [];
}

function portal_api_action(array $body): string {
    $a = $_GET['action'] ?? ($body['action'] ?? '');
    return is_string($a) ? trim($a) : '';
}

/** Bearer <station_api_key> — used only by station_heartbeat. */
function portal_api_station_from_bearer(): ?array {
    $hdr = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if (!is_string($hdr) || !preg_match('/^Bearer\s+(.+)$/i', $hdr, $m)) {
        return null;
    }
    return portal_station_find_by_api_key(trim($m[1]));
}

if (PHP_SAPI === 'cli' && getenv('NEXVUE_PORTAL_HTTP') === false) {
    return;
}

$body = portal_api_body();
$action = portal_api_action($body);
if ($action === '') {
    portal_api_fail(400, 'missing action');
}

try {
    portal_migrate();
} catch (Throwable $e) {
    portal_api_fail(500, 'portal store unavailable');
}

try {
    // ---- public --------------------------------------------------------

    if ($action === 'login') {
        if (!portal_test_auth_enabled()) {
            portal_api_fail(404, 'NexAPP SSO only');
        }
        $username = (string)($body['username'] ?? '');
        $password = (string)($body['password'] ?? '');
        $row = portal_user_verify($username, $password);
        if ($row === null) {
            portal_api_fail(401, 'invalid credentials');
        }
        portal_login_user($row);
        portal_api_ok(['user' => portal_me_payload()]);
    }

    if ($action === 'logout') {
        portal_session_clear();
        portal_nexapp_lib_load();
        $client = function_exists('portal_nexapp_logout_client')
            ? portal_nexapp_logout_client()
            : ['logout_url' => '/logout.php', 'csrf' => null, 'return_to' => '/login.php'];
        portal_api_ok($client);
    }

    if ($action === 'me') {
        $me = portal_me_payload();
        if ($me === null) {
            portal_api_fail(401, 'unauthorized');
        }
        portal_api_ok(['user' => $me]);
    }

    // ---- edge-initiated, no browser session -----------------------------

    if ($action === 'enroll_exchange') {
        $token = trim((string)($body['enrollment_token'] ?? ''));
        $edgeBaseUrl = rtrim(trim((string)($body['edge_base_url'] ?? '')), '/');
        $edgeVersion = trim((string)($body['edge_version'] ?? ''));
        if ($token === '') {
            portal_api_fail(400, 'enrollment_token required');
        }
        if ($edgeBaseUrl === '' || !preg_match('#^https://#i', $edgeBaseUrl)) {
            portal_api_fail(400, 'edge_base_url must be an https:// URL');
        }
        try {
            $result = portal_enroll_token_consume($token, $edgeBaseUrl, $edgeVersion);
        } catch (InvalidArgumentException $e) {
            portal_api_fail(400, $e->getMessage());
        }
        $keys = portal_ensure_keys();
        portal_api_ok([
            'station_id' => $result['station']['id'],
            'station_api_key' => $result['api_key'],
            'portal_jwks' => $keys['jwks'],
            'heartbeat_interval_s' => 300,
        ]);
    }

    if ($action === 'station_heartbeat') {
        $station = portal_api_station_from_bearer();
        if ($station === null) {
            portal_api_fail(401, 'unauthorized');
        }
        $edgeVersion = trim((string)($body['edge_version'] ?? ''));
        $channels = $body['channels'] ?? [];
        if (!is_array($channels)) {
            portal_api_fail(400, 'channels must be an array');
        }
        portal_station_touch_heartbeat($station['id'], $edgeVersion);
        portal_station_channels_upsert($station['id'], $channels);
        if (array_key_exists('ice_servers', $body)) {
            $ice = $body['ice_servers'];
            $iceList = is_array($ice) ? $ice : [];
            $iceExp = (string)($body['ice_servers_expires_at'] ?? '');
            portal_station_ice_servers_store($station['id'], $iceList, $iceExp);
        }
        if (array_key_exists('sfu', $body)) {
            $sfu = is_array($body['sfu']) ? $body['sfu'] : [];
            $play = is_array($sfu['play'] ?? null) ? $sfu['play'] : [];
            portal_station_sfu_store($station['id'], (string)($sfu['mode'] ?? 'off'), $play);
        }
        $keys = portal_ensure_keys();
        $users = portal_heartbeat_users_for_station($station['id']);
        $extra = ['portal_jwks' => $keys['jwks']];
        if ($users !== null) {
            $extra['users_sync'] = true;
            $extra['users'] = $users;
        }
        portal_api_ok($extra);
    }

    // ---- any authenticated portal role -----------------------------------

    if ($action === 'change_password') {
        if (!portal_test_auth_enabled()) {
            portal_api_fail(404, 'NexAPP SSO only');
        }
        $user = portal_require_any();
        $user = portal_current_user(true) ?? $user; // force-reload for a real password_hash
        $current = (string)($body['current_password'] ?? '');
        $next = (string)($body['new_password'] ?? '');
        if ($user['password_hash'] === '' || !password_verify($current, $user['password_hash'])) {
            portal_api_fail(401, 'invalid credentials');
        }
        if (strlen($next) < 8) {
            portal_api_fail(400, 'password must be at least 8 characters');
        }
        portal_user_update($user['id'], ['password' => $next, 'must_change_password' => false]);
        portal_api_ok(['user' => portal_me_payload()]);
    }

    if ($action === 'catalog_list') {
        $user = portal_require_any();
        portal_api_ok(['stations' => portal_catalog_list_for_user($user)]);
    }

    if ($action === 'viewer_jwt') {
        $user = portal_require_any();
        $stationId = (string)($body['station_id'] ?? '');
        $channelBase = strtolower(trim((string)($body['channel_base'] ?? '')));
        if ($stationId === '' || !preg_match('/^ch[0-7]$/', $channelBase)) {
            portal_api_fail(400, 'station_id and channel_base (ch0-ch7) required');
        }
        $station = portal_station_find_by_id($stationId);
        if ($station === null || $station['org_id'] !== $user['org_id'] || $station['status'] !== 'active') {
            portal_api_fail(404, 'station not found');
        }
        if (!portal_user_allows_channel($user, $stationId, $channelBase)) {
            portal_api_fail(403, 'channel not allowed');
        }
        $sub = 'portal:' . ($user['username'] ?? $user['id']);
        $jwt = portal_mint_viewer_jwt($sub, $channelBase);
        $whepUrl = rtrim((string)$station['edge_base_url'], '/') . ':' . (int)$station['edge_whep_port']
            . '/' . $channelBase . '/whep';
        $play = portal_station_sfu_play_url($station, $channelBase);
        portal_api_ok([
            'jwt' => $jwt,
            'expires_in' => NEXVUE_PORTAL_VIEWER_JWT_TTL_S,
            'whep_url' => $whepUrl,
            'path' => $channelBase,
            'ice_servers' => portal_station_ice_servers_for_viewer($station),
            'egress' => $play !== '' ? 'sfu' : 'local',
        ]);
    }

    if ($action === 'sfu_whep') {
        $user = portal_require_any();
        $stationId = (string)($body['station_id'] ?? '');
        $channelBase = strtolower(trim((string)($body['channel_base'] ?? '')));
        if ($stationId === '' || !preg_match('/^ch[0-7]$/', $channelBase)) {
            portal_api_fail(400, 'station_id and channel_base (ch0-ch7) required');
        }
        $station = portal_station_find_by_id($stationId);
        if ($station === null || $station['org_id'] !== $user['org_id'] || $station['status'] !== 'active') {
            portal_api_fail(404, 'station not found');
        }
        if (!portal_user_allows_channel($user, $stationId, $channelBase)) {
            portal_api_fail(403, 'channel not allowed');
        }
        $play = portal_station_sfu_play_url($station, $channelBase);
        if ($play === '') {
            portal_api_fail(409, 'Stream live input is not ready for this channel');
        }
        $sdp = (string)($body['sdp'] ?? '');
        try {
            $answer = portal_sfu_whep_exchange($play, $sdp);
        } catch (InvalidArgumentException $e) {
            portal_api_fail(400, $e->getMessage());
        } catch (RuntimeException $e) {
            portal_api_fail(502, $e->getMessage());
        }
        portal_api_ok(['sdp' => $answer, 'path' => $channelBase]);
    }

    // ---- org_admin only ---------------------------------------------------

    if ($action === 'users_list') {
        $user = portal_require_roles(['org_admin']);
        portal_api_ok(['users' => portal_users_list_for_org($user['org_id'])]);
    }

    if ($action === 'user_create') {
        if (!portal_test_auth_enabled()) {
            portal_api_fail(404, 'NexAPP issues portal users');
        }
        $user = portal_require_roles(['org_admin']);
        try {
            $row = portal_user_create([
                'org_id' => $user['org_id'],
                'username' => (string)($body['username'] ?? ''),
                'password' => (string)($body['password'] ?? ''),
                'role' => (string)($body['role'] ?? 'org_viewer'),
                'email' => $body['email'] ?? null,
                'must_change_password' => !empty($body['must_change_password']),
            ]);
        } catch (InvalidArgumentException $e) {
            portal_api_fail(400, $e->getMessage());
        } catch (RuntimeException $e) {
            portal_api_fail(400, $e->getMessage());
        }
        portal_api_ok(['user' => portal_user_row_public($row)]);
    }

    if ($action === 'user_update') {
        $user = portal_require_roles(['org_admin']);
        $id = (string)($body['id'] ?? '');
        if ($id === '') {
            portal_api_fail(400, 'id required');
        }
        try {
            $row = portal_user_update($id, $body, $user['org_id']);
        } catch (InvalidArgumentException $e) {
            portal_api_fail(400, $e->getMessage());
        }
        portal_api_ok(['user' => portal_user_row_public($row)]);
    }

    if ($action === 'stations_list') {
        $user = portal_require_roles(['org_admin', 'org_operator']);
        $stations = portal_stations_list_for_org($user['org_id']);
        foreach ($stations as &$s) {
            $s['channels'] = portal_station_channels_list($s['id']);
        }
        unset($s);
        portal_api_ok(['stations' => $stations]);
    }

    if ($action === 'enroll_token_create') {
        $user = portal_require_roles(['org_admin']);
        $name = (string)($body['name'] ?? '');
        try {
            $result = portal_enroll_token_create($user['org_id'], $name, $user['id']);
        } catch (InvalidArgumentException $e) {
            portal_api_fail(400, $e->getMessage());
        }
        portal_api_ok([
            'id' => $result['row']['id'],
            'token' => $result['token'],
            'expires_at' => $result['row']['expires_at'],
        ]);
    }

    if ($action === 'enroll_token_revoke') {
        $user = portal_require_roles(['org_admin']);
        $id = (string)($body['id'] ?? '');
        if ($id === '') {
            portal_api_fail(400, 'id required');
        }
        portal_enroll_token_revoke($id, $user['org_id']);
        portal_api_ok();
    }

    if ($action === 'nexapp_groups') {
        $user = portal_require_roles(['org_admin']);
        portal_nexapp_lib_load();
        $directory = function_exists('portal_nexapp_directory') ? portal_nexapp_directory() : null;
        portal_api_ok([
            'groups' => $directory ?? [],
            'directory_ok' => $directory !== null,
            'acl' => portal_group_acl_list($user['org_id']),
        ]);
    }

    if ($action === 'group_acl_put') {
        $user = portal_require_roles(['org_admin']);
        $groupId = (string)($body['nexapp_group_id'] ?? '');
        $groupName = (string)($body['nexapp_group_name'] ?? '');
        $stationId = (string)($body['station_id'] ?? '');
        $edgeRole = (string)($body['edge_role'] ?? 'viewer');
        $channels = $body['channels'] ?? null;
        if ($groupId === '' || $stationId === '') {
            portal_api_fail(400, 'nexapp_group_id and station_id required');
        }
        if ($channels !== null && !is_array($channels)) {
            portal_api_fail(400, 'channels must be an array or null');
        }
        try {
            portal_group_acl_put($user['org_id'], $groupId, $groupName, $stationId, $channels, $edgeRole);
        } catch (InvalidArgumentException $e) {
            portal_api_fail(400, $e->getMessage());
        }
        portal_api_ok();
    }

    if ($action === 'station_sso') {
        $user = portal_require_any();
        $stationId = (string)($body['station_id'] ?? '');
        if ($stationId === '') {
            portal_api_fail(400, 'station_id required');
        }
        $station = portal_station_find_by_id($stationId);
        if ($station === null || $station['org_id'] !== $user['org_id']) {
            portal_api_fail(404, 'station not found');
        }
        try {
            $jwt = portal_mint_sso_jwt($user, $stationId);
        } catch (RuntimeException $e) {
            portal_api_fail(403, $e->getMessage());
        } catch (InvalidArgumentException $e) {
            portal_api_fail(400, $e->getMessage());
        }
        portal_api_ok([
            'jwt' => $jwt,
            'expires_in' => NEXVUE_PORTAL_SSO_JWT_TTL_S,
            'login_url' => portal_station_login_url($station, $jwt),
        ]);
    }

    if ($action === 'health_summary') {
        $user = portal_require_any();
        portal_api_ok(portal_health_summary($user['org_id']));
    }

    if ($action === 'catalog_acl_put') {
        $user = portal_require_roles(['org_admin']);
        $portalUserId = (string)($body['portal_user_id'] ?? '');
        $stationId = (string)($body['station_id'] ?? '');
        $channels = $body['channels'] ?? null;
        if ($portalUserId === '' || $stationId === '') {
            portal_api_fail(400, 'portal_user_id and station_id required');
        }
        if ($channels !== null && !is_array($channels)) {
            portal_api_fail(400, 'channels must be an array or null');
        }
        try {
            portal_catalog_acl_put($user['org_id'], $portalUserId, $stationId, $channels);
        } catch (InvalidArgumentException $e) {
            portal_api_fail(400, $e->getMessage());
        }
        portal_api_ok();
    }

    portal_api_fail(400, 'unknown action');
} catch (RuntimeException $e) {
    $msg = $e->getMessage();
    if ($msg === 'unauthorized') {
        portal_api_fail(401, $msg);
    }
    if ($msg === 'forbidden') {
        portal_api_fail(403, $msg);
    }
    portal_api_fail(500, $msg);
} catch (Throwable $e) {
    portal_api_fail(500, 'internal error');
}
