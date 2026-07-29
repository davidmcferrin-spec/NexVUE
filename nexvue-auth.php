<?php
/**
 * nexvue-auth.php — JSON API for NexVUE local auth + share links.
 *
 * Public: login, logout, me, forgot, reset, share_redeem
 * Authed: whep_jwt, change_password
 * Admin: users_*, shares_*, user_reset_link, users_export/import, shares_export/import
 * Sync: Bearer NEXVUE_SYNC_KEY may call export/import without a browser session.
 *
 * CLI include: when PHP_SAPI is cli and NEXVUE_AUTH_HTTP is unset, returns
 * after loading the lib (unit tests).
 */

declare(strict_types=1);

require_once __DIR__ . '/nexvue-auth-lib.php';

function auth_api_fail(int $status, string $message): never {
    if (!headers_sent()) {
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
    }
    http_response_code($status);
    echo json_encode(['ok' => false, 'error' => $message], JSON_UNESCAPED_SLASHES);
    exit;
}

function auth_api_ok(array $extra = []): never {
    if (!headers_sent()) {
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
    }
    echo json_encode(array_merge(['ok' => true], $extra), JSON_UNESCAPED_SLASHES);
    exit;
}

function auth_api_body(): array {
    $raw = file_get_contents('php://input');
    if (!is_string($raw) || trim($raw) === '') {
        return [];
    }
    $j = json_decode($raw, true);
    return is_array($j) ? $j : [];
}

function auth_api_action(array $body): string {
    $a = $_GET['action'] ?? ($body['action'] ?? '');
    return is_string($a) ? trim($a) : '';
}

function auth_api_require_admin_or_sync(): void {
    if (auth_bearer_sync_ok()) {
        return;
    }
    try {
        auth_require_roles(['admin']);
    } catch (Throwable $e) {
        auth_api_fail(403, 'forbidden');
    }
}

if (PHP_SAPI === 'cli' && getenv('NEXVUE_AUTH_HTTP') === false) {
    return;
}

$body = auth_api_body();
$action = auth_api_action($body);
if ($action === '') {
    auth_api_fail(400, 'missing action');
}

// Hot paths (me / whep_jwt / logout): skip migrate — session cache + existing DB.
try {
    if (!in_array($action, ['me', 'whep_jwt', 'logout'], true)) {
        auth_migrate();
    }
} catch (Throwable $e) {
    auth_api_fail(500, 'auth store unavailable');
}

try {
    if ($action === 'login') {
        $username = (string)($body['username'] ?? '');
        $password = (string)($body['password'] ?? '');
        $row = auth_user_verify($username, $password);
        if ($row === null) {
            auth_api_fail(401, 'invalid credentials');
        }
        auth_login_user($row);
        auth_api_ok(['user' => auth_me_payload()]);
    }

    if ($action === 'logout') {
        auth_session_clear();
        auth_api_ok();
    }

    if ($action === 'me') {
        $me = auth_me_payload();
        if ($me === null) {
            auth_api_fail(401, 'unauthorized');
        }
        auth_api_ok(['user' => $me]);
    }

    if ($action === 'change_password') {
        $user = auth_current_user();
        if ($user === null) {
            auth_api_fail(401, 'unauthorized');
        }
        $current = (string)($body['current_password'] ?? '');
        $next = (string)($body['new_password'] ?? '');
        if (!password_verify($current, $user['password_hash'])) {
            auth_api_fail(401, 'invalid credentials');
        }
        if (strlen($next) < 8) {
            auth_api_fail(400, 'password must be at least 8 characters');
        }
        if ($next === 'password') {
            auth_api_fail(400, 'choose a password other than the default');
        }
        auth_user_update($user['id'], [
            'password' => $next,
            'must_change_password' => false,
        ]);
        auth_api_ok(['user' => auth_me_payload()]);
    }

    if ($action === 'forgot') {
        $identity = trim((string)($body['username_or_email'] ?? $body['identity'] ?? ''));
        $generic = ['message' => 'If an account matches, a reset was created. Contact an admin if you do not receive email.'];
        if ($identity === '') {
            auth_api_ok($generic);
        }
        $row = null;
        if (str_contains($identity, '@')) {
            $row = auth_user_find_by_email($identity);
        }
        if ($row === null) {
            $row = auth_user_find_by_username($identity);
        }
        if ($row !== null && empty($row['disabled_at'])) {
            $reset = auth_reset_create($row['id']);
            $scheme = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
            $host = $_SERVER['HTTP_HOST'] ?? 'localhost';
            $url = $scheme . '://' . $host . '/reset.html?token=' . rawurlencode($reset['token']);
            if (!empty($row['email'])) {
                auth_try_mail_reset((string)$row['email'], $url);
            }
        }
        auth_api_ok($generic);
    }

    if ($action === 'reset') {
        $token = (string)($body['token'] ?? '');
        $password = (string)($body['password'] ?? '');
        try {
            auth_reset_consume($token, $password);
        } catch (InvalidArgumentException $e) {
            auth_api_fail(400, $e->getMessage());
        }
        auth_api_ok(['message' => 'password updated']);
    }

    if ($action === 'share_redeem') {
        $token = (string)($body['token'] ?? $_GET['token'] ?? '');
        $row = auth_share_find_by_token($token);
        if (!auth_share_is_valid($row)) {
            auth_api_fail(401, 'invalid or expired share link');
        }
        auth_login_share($row);
        auth_api_ok(['user' => auth_me_payload()]);
    }

    if ($action === 'whep_jwt') {
        auth_require_any();
        $path = (string)($body['path'] ?? $_GET['path'] ?? '');
        $path = strtolower(trim($path));
        if (!preg_match('/^ch[0-7](lo)?$/', $path)) {
            auth_api_fail(400, 'invalid path');
        }
        $allowed = auth_allowed_channels_for_session();
        if ($allowed === null || !in_array($path, $allowed, true)) {
            auth_api_fail(403, 'channel not allowed');
        }
        $base = str_ends_with($path, 'lo') ? substr($path, 0, -2) : $path;
        $me = auth_me_payload();
        $sub = ($me['auth'] ?? '') === 'share'
            ? ('share:' . ($me['share_id'] ?? 'unknown'))
            : ('user:' . ($me['username'] ?? 'unknown'));
        // Unlock session before openssl JWT work so status polls are not blocked.
        auth_session_release();
        auth_ensure_keys();
        $jwt = auth_mint_viewer_jwt($sub, [$base]);
        auth_api_ok([
            'jwt' => $jwt,
            'expires_in' => NEXVUE_AUTH_JWT_TTL_S,
            'path' => $path,
        ]);
    }

    if ($action === 'users_list') {
        auth_require_roles(['admin']);
        auth_api_ok(['users' => auth_users_list()]);
    }

    if ($action === 'user_create') {
        auth_require_roles(['admin']);
        try {
            $row = auth_user_create([
                'username' => (string)($body['username'] ?? ''),
                'password' => (string)($body['password'] ?? ''),
                'email' => $body['email'] ?? null,
                'role' => (string)($body['role'] ?? 'viewer'),
                'must_change_password' => !empty($body['must_change_password']),
            ]);
        } catch (InvalidArgumentException $e) {
            auth_api_fail(400, $e->getMessage());
        } catch (Throwable $e) {
            auth_api_fail(400, 'could not create user');
        }
        auth_api_ok(['user' => auth_user_row_public($row)]);
    }

    if ($action === 'user_update') {
        auth_require_roles(['admin']);
        $id = (string)($body['id'] ?? '');
        if ($id === '') {
            auth_api_fail(400, 'missing id');
        }
        try {
            $row = auth_user_update($id, $body);
        } catch (InvalidArgumentException $e) {
            auth_api_fail(400, $e->getMessage());
        }
        auth_api_ok(['user' => auth_user_row_public($row)]);
    }

    if ($action === 'user_reset_link') {
        $admin = auth_require_roles(['admin']);
        $id = (string)($body['id'] ?? $_GET['id'] ?? '');
        $row = auth_user_find_by_id($id);
        if ($row === null) {
            auth_api_fail(404, 'user not found');
        }
        $reset = auth_reset_create($row['id']);
        $scheme = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
        $host = $_SERVER['HTTP_HOST'] ?? 'localhost';
        $url = $scheme . '://' . $host . '/reset.html?token=' . rawurlencode($reset['token']);
        auth_api_ok([
            'user_id' => $row['id'],
            'username' => $row['username'],
            'reset_url' => $url,
            'expires_at' => $reset['expires_at'],
            'created_by' => $admin['username'],
        ]);
    }

    if ($action === 'shares_list') {
        auth_require_roles(['admin']);
        auth_api_ok(['shares' => auth_shares_list()]);
    }

    if ($action === 'share_create') {
        $admin = auth_require_roles(['admin']);
        $name = (string)($body['name'] ?? '');
        $channels = $body['channels'] ?? [];
        if (!is_array($channels)) {
            auth_api_fail(400, 'channels must be an array');
        }
        $duration = isset($body['duration_s']) ? (int)$body['duration_s'] : null;
        $absolute = isset($body['expires_at']) ? (string)$body['expires_at'] : null;
        try {
            $expires = auth_parse_expires($absolute, $duration);
            $created = auth_share_create($name, $channels, $expires, $admin['id']);
        } catch (InvalidArgumentException $e) {
            auth_api_fail(400, $e->getMessage());
        }
        $pub = auth_share_row_public($created['row'], true, $created['token']);
        $scheme = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
        $host = $_SERVER['HTTP_HOST'] ?? 'localhost';
        $pub['url'] = $scheme . '://' . $host . '/index.html?t=' . rawurlencode($created['token']);
        auth_api_ok(['share' => $pub]);
    }

    if ($action === 'share_revoke') {
        auth_require_roles(['admin']);
        $id = (string)($body['id'] ?? '');
        if ($id === '') {
            auth_api_fail(400, 'missing id');
        }
        try {
            $row = auth_share_revoke($id);
        } catch (InvalidArgumentException $e) {
            auth_api_fail(404, $e->getMessage());
        }
        auth_api_ok(['share' => auth_share_row_public($row)]);
    }

    if ($action === 'users_export') {
        auth_api_require_admin_or_sync();
        $since = isset($_GET['since']) ? (string)$_GET['since'] : (isset($body['since']) ? (string)$body['since'] : null);
        auth_api_ok(['users' => auth_users_export($since)]);
    }

    if ($action === 'users_import') {
        auth_api_require_admin_or_sync();
        $users = $body['users'] ?? [];
        if (!is_array($users)) {
            auth_api_fail(400, 'users must be an array');
        }
        auth_api_ok(auth_users_import($users));
    }

    if ($action === 'shares_export') {
        auth_api_require_admin_or_sync();
        $since = isset($_GET['since']) ? (string)$_GET['since'] : (isset($body['since']) ? (string)$body['since'] : null);
        auth_api_ok(['shares' => auth_shares_export($since)]);
    }

    if ($action === 'shares_import') {
        auth_api_require_admin_or_sync();
        $shares = $body['shares'] ?? [];
        if (!is_array($shares)) {
            auth_api_fail(400, 'shares must be an array');
        }
        auth_api_ok(auth_shares_import($shares));
    }

    auth_api_fail(400, 'unknown action');
} catch (RuntimeException $e) {
    $msg = $e->getMessage();
    if ($msg === 'unauthorized') {
        auth_api_fail(401, $msg);
    }
    if ($msg === 'forbidden') {
        auth_api_fail(403, $msg);
    }
    auth_api_fail(500, $msg);
} catch (Throwable $e) {
    auth_api_fail(500, 'internal error');
}
